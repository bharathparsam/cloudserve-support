"""Explicit paid API configuration and shared workflow; no hosted inference."""

from decimal import Decimal
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from test_integration import fixture_runtime, enable_fixture
from src.api import create_app
from src.operator_settings import (
    OperatorSettings,
    build_providers,
    persist_operator_settings,
    read_operator_settings,
)
from src.runtime import load_configuration

HEADERS = {"Authorization": "Bearer operator-test"}
PAID = {
    "generation_mode": "paid_test",
    "generation_model": "openai/gpt-4.1-mini",
    "decider_model": "perplexity/pplx-decider-flash-v1.3:free",
    "decider_mode": "disabled",
    "timeout_seconds": 30.0,
    "max_retries": 0,
}


@pytest.mark.parametrize(
    "change",
    [
        {"generation_mode": "free_only"},
        {"generation_model": "openai/gpt-5"},
        {"max_retries": 1},
        {"decider_model": "perplexity/paid"},
    ],
)
def test_paid_mode_never_implicitly_broadens_authority(change):
    with pytest.raises(ValidationError):
        OperatorSettings.model_validate(PAID | change)


def test_legacy_operator_payload_stays_free_by_default(fixture_runtime):
    s, store, pipeline = fixture_runtime
    legacy = {k: v for k, v in PAID.items() if k != "generation_mode"} | {
        "generation_model": "test/writer:free"
    }
    snapshot = persist_operator_settings(store, legacy)
    assert snapshot["generation_mode"] == "free_only"
    assert read_operator_settings(s, store)["generation_model"] == "test/writer:free"


def test_paid_provider_creation_is_local_and_budget_durable(fixture_runtime):
    s, store, pipeline = fixture_runtime
    provider, observer = build_providers(s, PAID)
    assert observer is None and provider.model == "openai/gpt-4.1-mini"
    assert provider.metadata["paid_budget"]["spent_usd"] == "0"
    provider.ledger.reserve("prior-fixture-paid", Decimal(".01"))
    provider.ledger.settle("prior-fixture-paid", Decimal(".003"))
    other, _ = build_providers(s, PAID)
    assert Decimal(other.metadata["paid_budget"]["spent_usd"]) == Decimal(".003")
    assert other.max_retries == 0


def test_paid_save_applies_to_same_api_pipeline_and_restart(
    fixture_runtime, monkeypatch
):
    from src.runtime import apply_operator_values

    s, store, pipeline = fixture_runtime

    def build(settings, value):
        return (
            SimpleNamespace(
                api_key="fixture",
                model=value.generation_model,
                metadata={"fixture": True, "cost_policy": "paid_test_only"},
                preflight=lambda: {"verified": True},
            ),
            None,
        )

    monkeypatch.setattr("src.api.build_providers", build)
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        r = c.put("/api/v1/settings", headers=HEADERS, json=PAID)
        assert r.status_code == 200
        assert (
            r.json()["generation_mode"] == "paid_test"
            and r.json()["paid_budget"]["limit_usd"] == "3.50"
        )
        status = c.get("/api/v1/status").json()
        assert (
            status["generation_mode"] == "paid_test"
            and not status["automation"]["enabled"]
        )
        assert pipeline.provider.model == "openai/gpt-4.1-mini"
    from src.config import Settings

    restarted = load_configuration(Settings(storage=s.storage))
    assert (
        restarted.generation_mode == "paid_test"
        and restarted.generation_cost_policy == "paid_test_only"
    )
    assert restarted.model == "openai/gpt-4.1-mini"


def test_budget_depletion_prevents_enable_before_any_provider_request(fixture_runtime):
    s, store, pipeline = fixture_runtime
    provider, _ = build_providers(s, PAID)
    provider.ledger.reserve("spent", Decimal("3.50"))
    provider.ledger.settle("spent", Decimal("3.50"))
    pipeline.provider = provider
    s.generation_mode = "paid_test"
    s.model = PAID["generation_model"]
    s.max_retries = 0

    def unexpected(*args, **kwargs):
        raise AssertionError("Exhausted budget must not contact provider")

    provider._inner.client.get = unexpected
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        r = c.put(
            "/api/v1/controls/automation",
            headers=HEADERS,
            json={"enabled": True, "reason": "fixture paid test"},
        )
        assert r.status_code == 409 and not store.control()["enabled"]
        assert (
            Decimal(
                c.get("/api/v1/settings", headers=HEADERS).json()["paid_budget"][
                    "remaining_usd"
                ]
            )
            == 0
        )


def test_pipeline_admits_a_new_record_without_changing_release_gates(fixture_runtime):
    store, pipeline = enable_fixture(fixture_runtime)
    admitted = []
    pipeline.provider.begin_record = admitted.append
    run = store.create_run([{"channel": "chat", "body": "How do I retry a request?"}])
    job = store.claim_job()
    result = pipeline.process(job["original"], run, job["record_id"], 0)
    assert admitted == [job["record_id"]]
    assert result.action == "auto_respond" and all(
        c.status == "pass" for c in result.checks
    )
