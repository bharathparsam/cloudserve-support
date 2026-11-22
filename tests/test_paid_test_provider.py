"""Mock HTTP tests only; no live inference or paid API requests."""

from decimal import Decimal
import json

import httpx
import pytest

from src.contracts import Assessment
from src.providers.openrouter import (
    OpenRouterProvider,
    ProviderUnavailable,
    FreePolicyError,
)
from src.providers.paid_test import PaidTestProvider

MODEL = "openai/gpt-4o-mini"
CANONICAL = "openai/gpt-4o-mini-2024-07-18"


def prices():
    return {
        "prompt": "0.00000015",
        "completion": "0.0000006",
        "input_cache_read": "0.000000075",
    }


def catalog():
    return {
        "id": MODEL,
        "canonical_slug": CANONICAL,
        "pricing": prices(),
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["response_format", "temperature"],
    }


def output(cost=".00001", **changes):
    data = {
        "id": "test-id",
        "model": CANONICAL,
        "provider": "OpenAI",
        "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 10},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"decision":"clear","reasons":[]}'},
            }
        ],
    }
    data.update(changes)
    return data


def provider(post=None, model_data=None, endpoint_prices=None, **kwargs):
    p = PaidTestProvider("not-a-real-key", model=MODEL, **kwargs)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "endpoints": [
                            {
                                "provider_name": "OpenAI",
                                "model_id": CANONICAL,
                                "pricing": endpoint_prices or prices(),
                            }
                        ]
                    }
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json={"data": [model_data or catalog()]})
        return post(request) if post else httpx.Response(200, json=output())

    p.client.close()
    p.client = httpx.Client(
        base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handle)
    )
    p._pace = lambda: None
    return p, calls


def call(p):
    return p.complete("assess", {"ticket": "development fixture"}, Assessment, 200)


@pytest.mark.parametrize("budget", [0, -1, True, "NaN", "Infinity", "3.500001"])
def test_budget_strictly_bounded(budget):
    with pytest.raises(ValueError):
        PaidTestProvider("fixture", budget_usd=budget)


@pytest.mark.parametrize(
    "kwargs", [{"model": "openai/gpt-4o"}, {"max_retries": 1}, {"max_retries": True}]
)
def test_explicit_model_and_no_retries(kwargs):
    with pytest.raises(ValueError):
        PaidTestProvider("fixture", **kwargs)


def test_preflight_paid_limits_and_positive_cost_success():
    p, calls = provider()
    call(p)
    request = json.loads(calls[-1].content)
    assert request["provider"] == {
        "only": ["OpenAI"],
        "allow_fallbacks": False,
        "max_price": {"prompt": 0.15, "completion": 0.6},
    }
    assert not {"tools", "plugins", "models", "web_search_options"} & request.keys()
    assert p.metadata["usage"]["cost"] == ".00001" or Decimal(
        p.metadata["usage"]["cost"]
    ) == Decimal(".00001")
    assert Decimal(p.metadata["cumulative_cost_usd"]) == Decimal(".00001")
    assert (
        p.metadata["reserved_unreported_cost_usd"] == "0.00000000"
        or Decimal(p.metadata["reserved_unreported_cost_usd"]) == 0
    )
    assert (
        p.metadata["cost_policy"] == "paid_test_only"
        and p.metadata["successful_calls"] == 1
    )
    assert "not-a-real-key" not in json.dumps(p.metadata)
    assert "development fixture" not in json.dumps(p.metadata)


@pytest.mark.parametrize(
    "pricing",
    [
        {"prompt": "1", "completion": "0"},
        {"prompt": "NaN", "completion": "0"},
        {"prompt": "0", "completion": "0", "request": ".1"},
        {"prompt": "0", "completion": "0", "input_cache_read": ".1"},
        {"prompt": "0"},
    ],
)
def test_unknown_expensive_or_hidden_fees_prevent_posts(pricing):
    model = catalog()
    model["pricing"] = pricing
    p, calls = provider(model_data=model)
    with pytest.raises(FreePolicyError):
        call(p)
    assert all(r.method == "GET" for r in calls)


def test_tiny_remaining_budget_prevents_inference():
    p, calls = provider(budget_usd=Decimal(".000001"))
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "paid_test_budget_insufficient"
    assert all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("cost", [None, True, -1, "NaN", ".3"])
def test_unknown_invalid_or_excess_cost_latches(cost):
    p, calls = provider(post=lambda _: httpx.Response(200, json=output(cost)))
    with pytest.raises(FreePolicyError):
        call(p)
    assert p._cost_violation
    with pytest.raises(FreePolicyError):
        call(p)
    assert sum(r.method == "POST" for r in calls) == 1


def test_missing_cost_latches():
    data = output()
    del data["usage"]["cost"]
    p, _ = provider(post=lambda _: httpx.Response(200, json=data))
    with pytest.raises(FreePolicyError):
        call(p)
    assert p.metadata["last_error"] == "usage_cost_missing"


def test_timeout_retains_reservation_and_never_retries():
    def timeout(request):
        raise httpx.ReadTimeout("private error", request=request)

    p, calls = provider(post=timeout)
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert sum(r.method == "POST" for r in calls) == 1
    assert Decimal(p.metadata["reserved_unreported_cost_usd"]) > 0
    assert p.metadata["reported_cost_usd"] == "0"
    assert "private error" not in json.dumps(p.metadata)


def test_maximum_three_requests_even_across_preflight():
    p, calls = provider()
    for _ in range(3):
        call(p)
    p.preflight()
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "paid_test_request_limit"
    assert sum(r.method == "POST" for r in calls) == 3


def test_invalid_identity_retains_reservation_and_no_success():
    p, _ = provider(post=lambda _: httpx.Response(200, json=output(provider="unknown")))
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "response_identity_unverified"
    assert Decimal(p.metadata["reserved_unreported_cost_usd"]) > 0
    assert p.metadata["successful_calls"] == 0


def test_production_provider_still_rejects_paid():
    p = OpenRouterProvider("fixture", model=MODEL, max_retries=0)
    with pytest.raises(FreePolicyError):
        p.preflight()


def test_optional_websearch_catalog_fee_does_not_enable_feature():
    model = catalog()
    model["pricing"]["web_search"] = ".01"
    p, calls = provider(model_data=model)
    call(p)
    assert "plugins" not in json.loads(calls[-1].content)


def test_oversize_request_and_optional_capabilities_blocked():
    p, calls = provider()
    p.preflight()
    with pytest.raises(FreePolicyError):
        p._before_request({"model": MODEL, "tools": []})
    with pytest.raises(ProviderUnavailable):
        p._before_request({"model": MODEL, "messages": ["x" * 262145], "max_tokens": 1})
    assert all(r.method == "GET" for r in calls)


def test_paid_pacing_never_sleeps_or_changes_free_tier_clock(monkeypatch):
    import src.providers.openrouter as base

    marker = base.OpenRouterProvider._last_attempt

    def forbidden(_):
        raise AssertionError("Paid adapter must not use free-tier sleep")

    monkeypatch.setattr(base.time, "sleep", forbidden)
    p = PaidTestProvider("fixture", budget_usd=Decimal("3.50"))
    p._pace()
    p._pace()
    assert base.OpenRouterProvider._last_attempt == marker
    assert p.metadata["pacing_policy"] == "serialized_paid_no_free_tier_sleep_v1"
    assert p.budget_usd == Decimal("3.50")


@pytest.mark.parametrize(
    "credit,key,required,verified,sufficient",
    [
        ({"total_credits": 4, "total_usage": 0.1}, {"limit": None}, "3.5", True, True),
        (
            {"total_credits": 4, "total_usage": 0.1},
            {"limit": 1, "limit_remaining": ".2"},
            ".3",
            True,
            False,
        ),
        (
            {"total_credits": 4, "total_usage": 0.1},
            {"limit": 1, "limit_remaining": ".3"},
            ".3",
            True,
            True,
        ),
        ({"total_credits": 4, "total_usage": 0.1}, {}, ".3", False, False),
        (
            {"total_credits": "NaN", "total_usage": 0},
            {"limit": None},
            ".3",
            False,
            False,
        ),
        ({"total_credits": 4, "total_usage": 0.1}, {"limit": 1}, ".3", False, False),
        ({"total_credits": 4, "total_usage": 0.1}, {"limit": None}, True, False, False),
    ],
)
def test_account_credit_check_never_reports_balances_or_keys(
    credit, key, required, verified, sufficient
):
    p = PaidTestProvider("private-fixture-key")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"data": credit if request.url.path.endswith("/credits") else key}
        )

    p.client.close()
    p.client = httpx.Client(
        base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler)
    )
    result = p.verify_credit(required)
    assert result["verified"] == verified and result["sufficient"] == sufficient
    assert set(result) == {"verified", "sufficient", "checked_at", "reason"}
    assert all(r.method == "GET" for r in requests)
    assert "private-fixture-key" not in json.dumps(p.metadata)
    assert not {"total_credits", "total_usage", "limit_remaining"} & result.keys()


def test_credit_network_failure_is_unknown_with_no_raw_error():
    p = PaidTestProvider("fixture")

    def failure(request):
        raise httpx.ConnectError("private error", request=request)

    p.client.close()
    p.client = httpx.Client(
        base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(failure)
    )
    result = p.verify_credit(Decimal(".1"))
    assert not result["verified"] and not result["sufficient"]
    assert "private error" not in json.dumps(p.metadata)
