"""Generation HTTP fixtures test evidence correctness, never live model quality."""

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from src.contracts import Assessment
from src.providers.openrouter import (
    OpenRouterProvider,
    ProviderUnavailable,
    FreePolicyError,
)

MODEL = "google/gemma-4-26b-a4b-it:free"
CANONICAL = "google/gemma-4-26b-a4b-it-20260403"


def response(**changes):
    data = {
        "id": "gen-synthetic",
        "model": MODEL,
        "provider": "Fixture",
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
            "cost": 0,
        },
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"decision":"clear","reasons":[]}',
                },
            }
        ],
    }
    data.update(changes)
    return data


def ready(handler):
    p = OpenRouterProvider(api_key="synthetic-not-real", max_retries=0)
    p._verified = True
    p.providers = ["Fixture"]
    p._pace = lambda: None
    p.metadata.update(verified=True, catalog_verified=True, status="verified")
    p.client = httpx.Client(
        base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler)
    )
    return p


def call(p):
    return p.complete(
        "assess", {"ticket": {"body": "How do retries work?"}}, Assessment, 500
    )


def test_success_then_429_cannot_reuse_prior_result_metadata():
    statuses = iter(
        [
            httpx.Response(200, json=response()),
            httpx.Response(429, json={"error": {"message": "DO NOT EXPOSE"}}),
        ]
    )
    p = ready(lambda _: next(statuses))
    assert call(p).decision == "clear"
    success_id = p.metadata["last_call_id"]
    assert p.metadata["actual_model"] == MODEL and p.metadata["successful_calls"] == 1
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_call_id"] != success_id
    assert (
        not {
            "actual_model",
            "actual_provider",
            "usage",
            "response_id",
            "last_call_at",
            "last_finish_reason",
        }
        & p.metadata.keys()
    )
    assert (
        p.metadata["last_error"] == "quota_exhausted"
        and p.metadata["status"] == "quota_exhausted"
    )
    assert p.metadata["last_call_status"] == "failed" and p.metadata["catalog_verified"]
    assert (
        p.metadata["request_attempts"] == 2
        and p.metadata["successful_http_responses"] == 1
    )
    assert p.metadata["successful_calls"] == p.metadata["calls"] == 1
    assert "DO NOT EXPOSE" not in json.dumps(p.metadata)


def test_zero_cost_usage_and_schema_are_required_for_success_counter():
    invalid = response()
    invalid["choices"][0]["message"][
        "content"
    ] = '{"decision":"clear","reasons":[],"invented":"field"}'
    p = ready(lambda _: httpx.Response(200, json=invalid))
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "response_schema_invalid"
    assert (
        p.metadata["successful_http_responses"] == 1
        and p.metadata["successful_calls"] == 0
    )
    assert p.metadata["usage"]["cost"] == 0 and p.metadata["actual_model"] == MODEL
    assert "invented" not in json.dumps(p.metadata)


@pytest.mark.parametrize(
    "cost,code",
    [
        (0.01, "unexpected_nonzero_cost"),
        (None, "usage_cost_invalid"),
        ("NaN", "usage_cost_invalid"),
        (False, "usage_cost_invalid"),
        (-1, "usage_cost_invalid"),
    ],
)
def test_nonzero_or_invalid_cost_latches_provider_without_more_requests(cost, code):
    calls = []
    data = response()
    data["usage"]["cost"] = cost

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=data)

    p = ready(handler)
    with pytest.raises(FreePolicyError):
        call(p)
    assert (
        p.metadata["last_error"] == code
        and not p._verified
        and not p.metadata["verified"]
    )
    with pytest.raises(FreePolicyError):
        call(p)
    assert len(calls) == 1 and p.metadata["last_error"] == "cost_policy_latched"
    assert "usage" not in p.metadata and "actual_model" not in p.metadata
    assert p.metadata["cost_violation_evidence"]["actual_model"] == MODEL
    with pytest.raises(FreePolicyError):
        p.preflight()
    assert len(calls) == 1


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 1, "completion_tokens": 1}])
def test_missing_cost_never_becomes_zero(usage):
    p = ready(lambda _: httpx.Response(200, json=response(usage=usage)))
    with pytest.raises(FreePolicyError):
        call(p)
    assert p.metadata["last_error"] == "usage_cost_missing"
    assert p.metadata["usage"] is None or p.metadata["usage"]["cost"] is None


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", None])
def test_incomplete_or_filtered_output_cannot_pass_even_with_valid_json(finish):
    data = response()
    data["choices"][0]["finish_reason"] = finish
    p = ready(lambda _: httpx.Response(200, json=data))
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == (
        "provider_refusal" if finish == "content_filter" else "incomplete_output"
    )
    assert p.metadata["successful_calls"] == 0


@pytest.mark.parametrize(
    "model,provider", [("other/model:free", "Fixture"), (MODEL, "UnverifiedProvider")]
)
def test_unknown_returned_identity_rejected(model, provider):
    p = ready(
        lambda _: httpx.Response(200, json=response(model=model, provider=provider))
    )
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "response_identity_unverified"
    assert "actual_model" not in p.metadata and "usage" not in p.metadata


def test_catalog_advertised_dated_canonical_alias_and_free_base_alias_allowed():
    p = OpenRouterProvider(api_key="synthetic", max_retries=0)
    p._pace = lambda: None
    posts = []

    def handler(request):
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": MODEL,
                            "canonical_slug": CANONICAL,
                            "pricing": {"prompt": "0", "completion": "0"},
                            "architecture": {
                                "input_modalities": ["text"],
                                "output_modalities": ["text"],
                            },
                            "supported_parameters": ["temperature", "response_format"],
                        }
                    ]
                },
            )
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "endpoints": [
                            {
                                "provider_name": "Fixture",
                                "model_id": MODEL,
                                "name": "Fixture | " + CANONICAL + ":free",
                                "pricing": {"prompt": "0", "completion": "0"},
                                "supported_parameters": ["response_format"],
                            }
                        ]
                    }
                },
            )
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        posts.append(json.loads(request.content))
        return httpx.Response(200, json=response(model=CANONICAL))

    p.client = httpx.Client(
        base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler)
    )
    assert call(p).decision == "clear"
    assert (
        p.metadata["actual_model"] == CANONICAL
        and MODEL[:-5] in p.metadata["verified_model_ids"]
    )
    assert posts[0]["provider"] == {
        "only": ["Fixture"],
        "allow_fallbacks": False,
        "max_price": {"prompt": 0, "completion": 0},
    }
    assert posts[0]["model"] == MODEL


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "configuration_or_authorization_rejected"),
        (402, "configuration_or_authorization_rejected"),
        (429, "quota_exhausted"),
        (503, "provider_unavailable"),
    ],
)
def test_http_error_codes_are_actionable_and_sanitized(status, code):
    p = ready(
        lambda _: httpx.Response(status, json={"error": {"message": "SECRET BODY"}})
    )
    with pytest.raises(ProviderUnavailable) as error:
        call(p)
    assert p.metadata["last_error"] == code and p.metadata["status"] == code
    assert "SECRET" not in str(error.value) and "SECRET" not in json.dumps(p.metadata)


def test_network_failure_clears_previous_success_evidence():
    first = [True]

    def handler(request):
        if first[0]:
            first[0] = False
            return httpx.Response(200, json=response())
        raise httpx.ReadTimeout("SECRET NETWORK MESSAGE", request=request)

    p = ready(handler)
    call(p)
    with pytest.raises(ProviderUnavailable) as error:
        call(p)
    assert p.metadata["last_error"] == "network_timeout" and "usage" not in p.metadata
    assert "SECRET" not in str(error.value)


def test_unknown_retry_delay_is_not_retried_and_http_date_long_wait_stops(monkeypatch):
    for header in ("NaN", "unparseable", "Fri, 01 Jan 2100 00:00:00 GMT"):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"retry-after": header})

        p = ready(handler)
        p.max_retries = 2
        with pytest.raises(ProviderUnavailable):
            call(p)
        assert len(calls) == 1 and p.metadata["last_error"] in (
            "quota_retry_delay_unavailable",
            "quota_wait_required",
        )


def test_attempt_pacing_is_process_shared_and_tests_need_not_sleep(monkeypatch):
    ticks = iter([100.0, 101.0, 103.1])
    delays = []
    monkeypatch.setattr(
        "src.providers.openrouter.time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=delays.append),
    )
    monkeypatch.setattr(OpenRouterProvider, "_last_attempt", None)
    a = OpenRouterProvider()
    b = OpenRouterProvider()
    a._pace()
    b._pace()
    assert delays == pytest.approx([2.1])


def test_tiny_nonzero_cost_is_not_rounded_to_zero():
    p = ready(
        lambda _: httpx.Response(
            200,
            json=response(
                usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": "1e-999"}
            ),
        )
    )
    with pytest.raises(FreePolicyError):
        call(p)
    assert p.metadata["usage"]["cost"] == "1E-999"
    assert p.metadata["last_error"] == "unexpected_nonzero_cost"


@pytest.mark.parametrize(
    "reasoning,parameters,expected",
    [
        ({"mandatory": False}, ["reasoning"], {"enabled": False}),
        (
            {"mandatory": False, "default_enabled": False},
            ["reasoning"],
            {"enabled": False},
        ),
        ({"mandatory": True}, ["reasoning"], None),
        ({"mandatory": False}, [], None),
        ({"default_enabled": False}, ["reasoning"], None),
        ({"mandatory": 0}, ["reasoning"], None),
        (None, ["reasoning"], None),
    ],
)
def test_reasoning_is_disabled_only_when_catalog_explicitly_allows(
    reasoning, parameters, expected
):
    p = OpenRouterProvider(api_key="synthetic", max_retries=0)
    p._pace = lambda: None
    posted = []

    def handler(request):
        if request.url.path == "/api/v1/models":
            model = {
                "id": MODEL,
                "pricing": {"prompt": "0", "completion": "0"},
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": parameters,
            }
            if reasoning is not None:
                model["reasoning"] = reasoning
            return httpx.Response(200, json={"data": [model]})
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "endpoints": [
                            {
                                "provider_name": "Fixture",
                                "model_id": MODEL,
                                "pricing": {"prompt": "0", "completion": "0"},
                                "supported_parameters": parameters,
                            }
                        ]
                    }
                },
            )
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        posted.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    p.client = httpx.Client(
        base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler)
    )
    preflight = p.preflight()
    assert preflight["reasoning_policy"] == "disable_if_catalog_explicitly_optional_v1"
    assert preflight["reasoning_request_config"] == expected
    assert call(p).decision == "clear"
    assert posted[0]["max_tokens"] == 500
    if expected is None:
        assert "reasoning" not in posted[0]
    else:
        assert posted[0]["reasoning"] == expected
    assert p.metadata["last_request_reasoning"] == expected
