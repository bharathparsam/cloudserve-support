"""Synthetic HTTP fixtures verify adapter gates; these are not live inference evidence."""

import copy
import json
import math

import httpx
import pytest

from src.providers.decider import DeciderProvider, DeciderUnavailable, validate_answers
from src.providers.openrouter import FreePolicyError

MODEL = "perplexity/pplx-decider-flash-v1.3:free"
QUESTIONS = {"safe_input": {"type": "noul", "instructions": "Is this request safe?"}}


def catalog(pricing=None, architecture=None):
    return {
        "data": [
            {
                "id": MODEL,
                "canonical_slug": "perplexity/pplx-decider-flash-v1.3-20261112",
                "pricing": (
                    pricing
                    if pricing is not None
                    else {"prompt": "0", "completion": "0", "request": "0"}
                ),
                "architecture": (
                    architecture
                    if architecture is not None
                    else {
                        "input_modalities": ["text"],
                        "output_modalities": ["decisions"],
                    }
                ),
            }
        ]
    }


def valid(answers=None, cost=0):
    return {
        "id": "gen-synthetic",
        "model": MODEL,
        "provider": "Perplexity",
        "answers": (
            answers
            if answers is not None
            else {"safe_input": {"type": "noul", "noul": 0.96}}
        ),
        "usage": {"input_tokens": 123, "output_tokens": 0, "cost": cost},
    }


def provider(handler):
    p = DeciderProvider(api_key="synthetic-not-a-key")
    p.client = httpx.Client(
        base_url="https://openrouter.ai", transport=httpx.MockTransport(handler)
    )
    p._pace = lambda: None
    return p


def ready(handler):
    p = provider(handler)
    p._verified = True
    p.providers = ["Perplexity"]
    return p


def test_preflight_uses_decisions_catalog_and_zero_priced_endpoint():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/v1/models":
            assert request.url.params["output_modalities"] == "decisions"
            return httpx.Response(200, json=catalog())
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "endpoints": [
                            {
                                "provider_name": "Perplexity",
                                "pricing": {"prompt": "0", "completion": "0"},
                            },
                            {
                                "provider_name": "Paid",
                                "pricing": {"prompt": ".1", "completion": "0"},
                            },
                        ]
                    }
                },
            )
        return httpx.Response(
            200, json={"data": {"limit_remaining": 1000, "is_free_tier": False}}
        )

    p = provider(handler)
    result = p.preflight()
    assert result["verified"] and p.providers == ["Perplexity"]
    assert result["quota"]["remaining_free_requests"] is None
    assert result["quota"]["free_request_allowance_verified"] is False
    assert len(calls) == 3


@pytest.mark.parametrize(
    "pricing",
    [
        None,
        {"prompt": "0"},
        {"prompt": ".01", "completion": "0"},
        {"prompt": "0", "completion": "0", "request": ".01"},
        {"prompt": "0", "completion": "NaN"},
        {"prompt": "0", "completion": "-1"},
    ],
)
def test_nonfree_unknown_or_negative_pricing_rejected_before_decision(pricing):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        value = catalog()
        value["data"][0]["pricing"] = pricing
        return httpx.Response(200, json=value)

    p = provider(handler)
    with pytest.raises(FreePolicyError):
        p.preflight()
    assert calls == ["/api/v1/models"]


def test_missing_key_never_calls_network():
    p = DeciderProvider()
    p.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("network forbidden"))
    )
    with pytest.raises(DeciderUnavailable):
        p.decide({"body": "question"}, QUESTIONS)


def test_text_model_cannot_be_treated_as_decisions_model():
    p = provider(
        lambda _: httpx.Response(
            200,
            json=catalog(
                architecture={
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                }
            ),
        )
    )
    with pytest.raises(FreePolicyError):
        p.preflight()


def test_dedicated_endpoint_payload_and_safe_usage_metadata():
    def handler(request):
        assert request.url.path == "/api/alpha/decisions"
        payload = json.loads(request.content)
        assert payload["state"] == {"ticket": {"body": "How do retries work?"}}
        assert payload["questions"] == QUESTIONS
        assert payload["provider"] == {
            "only": ["Perplexity"],
            "allow_fallbacks": False,
            "max_price": {"prompt": 0, "completion": 0},
        }
        assert set(payload) == {"state", "questions", "model", "provider"}
        return httpx.Response(200, json=valid())

    p = ready(handler)
    result = p.decide({"ticket": {"body": "How do retries work?"}}, QUESTIONS)
    assert result["answers"]["safe_input"]["noul"] == 0.96
    assert (
        p.metadata["usage"]["cost"] == 0
        and p.metadata["actual_provider"] == "Perplexity"
    )
    assert p.api_key not in json.dumps(p.metadata)


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"safe_input": {"type": "refusal"}},
        {"safe_input": {"type": "noul", "noul": "0.9"}},
        {"safe_input": {"type": "noul", "noul": True}},
        {"safe_input": {"type": "noul", "noul": 1.1}},
        {"safe_input": {"type": "noul", "noul": float("nan")}},
        {
            "safe_input": {
                "type": "noul",
                "noul": 0.9,
                "explanation": "private untrusted text",
            }
        },
        {"safe_input": {"type": "choice", "choice": "safe"}},
    ],
)
def test_unusable_answers_are_never_converted_to_safe(answers):
    # Validate separately for NaN because httpx's JSON serializer rejects nonfinite data too.
    with pytest.raises(ValueError):
        validate_answers(answers, QUESTIONS)


def test_refusal_response_keeps_actual_usage_but_fails_closed():
    p = ready(
        lambda _: httpx.Response(200, json=valid({"safe_input": {"type": "refusal"}}))
    )
    with pytest.raises(DeciderUnavailable):
        p.decide({}, QUESTIONS)
    assert p.metadata["usage"]["input_tokens"] == 123
    assert p.metadata["last_error"] == "invalid_response"


@pytest.mark.parametrize("status,expected", [(401, 1), (402, 1), (429, 3), (503, 3)])
def test_provider_errors_bound_retries_and_do_not_leak_body(
    monkeypatch, status, expected
):
    monkeypatch.setattr("src.providers.decider.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            status,
            headers={"retry-after": "0"},
            json={"error": {"message": "SENSITIVE PROVIDER BODY"}},
        )

    p = ready(handler)
    with pytest.raises(DeciderUnavailable) as error:
        p.decide({}, QUESTIONS)
    assert len(calls) == expected and "SENSITIVE" not in str(error.value)


def test_long_retry_after_does_not_retry_early(monkeypatch):
    delays = []
    monkeypatch.setattr("src.providers.decider.time.sleep", delays.append)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"retry-after": "3600"})

    p = ready(handler)
    with pytest.raises(DeciderUnavailable):
        p.decide({}, QUESTIONS)
    assert (
        len(calls) == 1
        and delays == []
        and p.metadata["quota_wait_required_seconds"] == 3600
    )


def test_nonzero_usage_trips_cost_gate_without_retry():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=valid(cost=0.0001))

    p = ready(handler)
    with pytest.raises(DeciderUnavailable):
        p.decide({}, QUESTIONS)
    assert len(calls) == 1 and p.metadata["usage"]["cost"] == 0.0001
    assert not p.metadata["verified"] and not p._verified
    with pytest.raises(FreePolicyError):
        p.decide({}, QUESTIONS)
    with pytest.raises(FreePolicyError):
        p.preflight()
    assert len(calls) == 1


def test_unverified_returned_identity_rejected():
    data = valid()
    data["provider"] = "PaidUnknown"
    p = ready(lambda _: httpx.Response(200, json=data))
    with pytest.raises(DeciderUnavailable):
        p.decide({}, QUESTIONS)


def test_choice_and_score_distributions_are_strict():
    questions = {
        "owner": {
            "type": "choice",
            "instructions": "Choose owner",
            "criteria": {"support": None, "security": None},
        },
        "quality": {
            "type": "score",
            "instructions": "Grade",
            "criteria": ["none", "partial", "full"],
        },
    }
    answers = {
        "owner": {
            "type": "choice",
            "choice": "support",
            "probabilities": {"support": 0.8, "security": 0.2},
        },
        "quality": {
            "type": "score",
            "score": 1.8,
            "probabilities": {"0": 0, "1": 0.2, "2": 0.8},
        },
    }
    assert validate_answers(answers, questions)["quality"]["score"] == 1.8
    broken = copy.deepcopy(answers)
    broken["owner"]["probabilities"]["security"] = 0.3
    with pytest.raises(ValueError):
        validate_answers(broken, questions)
    broken = copy.deepcopy(answers)
    broken["quality"]["score"] = 2
    with pytest.raises(ValueError):
        validate_answers(broken, questions)


def test_invalid_local_payload_rejected_before_inference():
    p = ready(lambda _: pytest.fail("No inference should occur"))
    with pytest.raises(ValueError):
        p.decide({}, {"bad key": {"type": "noul", "instructions": "test"}})
    with pytest.raises(ValueError):
        p.decide({"value": float("nan")}, QUESTIONS)


def test_decider_attempts_are_paced_across_instances(monkeypatch):
    from types import SimpleNamespace

    ticks = iter([100.0, 101.0, 103.1])
    delays = []
    monkeypatch.setattr(
        "src.providers.decider.time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=delays.append),
    )
    monkeypatch.setattr(DeciderProvider, "_last_attempt", None)
    first = DeciderProvider()
    second = DeciderProvider()
    first._pace()
    second._pace()
    assert delays == pytest.approx([2.1])
