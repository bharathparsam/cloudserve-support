"""Optional held-UNKNOWN policy: isolated ledgers/HTTP fixtures, never live calls."""

from decimal import Decimal as D
from pathlib import Path
import json, uuid
import pytest
from filelock import FileLock
from src.logging_store import Store
from src.paid_budget import BudgetLedger, BudgetUnavailable
from src.config import Settings
from scripts.paid_timeout_policy import acknowledge


def proof(size=1000, tokens=500):
    return {
        "version": "bounded-paid-request-v1",
        "model": "openai/gpt-4o-mini",
        "request_bytes": size,
        "max_tokens": tokens,
        "framing_token_allowance": 4096,
        "max_retries": 0,
        "prompt_price": ".00000015",
        "completion_price": ".0000006",
        "pricing_verified": True,
        "optional_capabilities_disabled": True,
        "catalog_hash": "a" * 64,
    }


def estimate(bound):
    return (bound["request_bytes"] + 4096) * D(bound["prompt_price"]) + bound[
        "max_tokens"
    ] * D(bound["completion_price"])


def timeout(call, value):
    return {
        "cost_policy": "paid_test_only",
        "configured_model": "openai/gpt-4o-mini",
        "last_call_id": call,
        "last_call_status": "failed",
        "last_error": "network_timeout",
        "request_attempts": 1,
        "last_retry_count": 0,
        "last_reserved_cost_usd": str(value),
        "usage": None,
        "last_http_status": None,
        "response_id": None,
        "actual_model": None,
        "actual_provider": None,
        "catalog_verified": True,
        "catalog_hash": "a" * 64,
        "verified_endpoints": [
            {
                "provider": "OpenAI",
                "pricing": {"prompt": ".00000015", "completion": ".0000006"},
            }
        ],
        "routing_price_limits": {"prompt": ".15", "completion": ".6"},
        "last_task": "validate",
    }


def event(store, metadata, code_hash=None):
    record_id = None
    run_id = None
    if code_hash:
        run_id = store.create_run(
            [{"channel": "chat", "body": "synthetic timeout proof"}], kind="development"
        )
        with store.connection() as db:
            record_id = db.execute(
                "SELECT record_id FROM input_records WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO outcomes VALUES(?,?,'escalate',1,?)",
                (record_id, run_id, json.dumps({"versions": {"code_hash": code_hash}})),
            )
            db.execute(
                "UPDATE runs SET state='completed',processed_count=1 WHERE run_id=?",
                (run_id,),
            )
            db.execute("UPDATE jobs SET state='done' WHERE run_id=?", (run_id,))
    store.event(record_id, run_id, "validation", {"provider": metadata})


def pending(tmp_path, bound=True):
    store = Store(tmp_path / "audit.db")
    ledger = BudgetLedger(store)
    p = proof()
    value = estimate(p)
    ledger.reserve("first-timeout", value, bound_proof=p if bound else None)
    ledger.block("paid_usage_unresolved")
    event(store, timeout("first-timeout", value))
    return store, ledger, value


def test_default_off_unknown_timeout_still_blocks_and_keeps_full_reservation(tmp_path):
    store, ledger, value = pending(tmp_path)
    assert not ledger.try_hold_timeout("first-timeout", timeout("first-timeout", value))
    state = ledger.snapshot()
    assert (
        state["blocked"]
        and D(state["reserved_usd"]) == value
        and D(state["spent_usd"]) == 0
    )
    assert "timeout_accounting_policy" not in ledger.authorization()


def test_explicit_ack_retains_unknown_liability_and_allows_only_remaining_cap(tmp_path):
    store, ledger, value = pending(tmp_path)
    state = ledger.acknowledge_timeout_policy(
        ["first-timeout"], "User approved full reservation liability 2026-11-22"
    )
    assert not state["blocked"] and state["timeout_accounting_policy_enabled"]
    assert state["held_unknown_count"] == 1 and D(state["held_unknown_usd"]) == value
    assert (
        D(state["spent_usd"]) == 0
        and D(state["reserved_usd"]) == value
        and D(state["remaining_usd"]) == D("3.50") - value
    )
    assert state["spent_is_known_lower_bound"]
    before = ledger.authorization()
    assert (
        before["timeout_accounting_policy"]["scope"]
        == "development_and_supplied_validation"
    )
    ledger.reserve("next", D(".01"))
    ledger.settle("next", D(".001"))
    assert ledger.authorization() == before
    restarted = BudgetLedger(Store(store.path))
    assert restarted.snapshot()["held_unknown_count"] == 1
    with pytest.raises(BudgetUnavailable):
        restarted.reserve("over-cap", D("3.50"))


def test_future_bounded_timeout_is_held_only_after_persistent_optin(tmp_path):
    store, ledger, value = pending(tmp_path)
    ledger.acknowledge_timeout_policy(
        ["first-timeout"], "Explicit operator acknowledgement"
    )
    p = proof(size=1100)
    second = estimate(p)
    ledger.reserve("next-timeout", second, bound_proof=p)
    assert ledger.try_hold_timeout("next-timeout", timeout("next-timeout", second))
    event(store, timeout("next-timeout", second))
    state = ledger.snapshot()
    assert (
        not state["blocked"]
        and state["held_unknown_count"] == 2
        and D(state["reserved_usd"]) == value + second
    )


@pytest.mark.parametrize(
    "change",
    [
        {"last_error": "response_identity_mismatch"},
        {"usage": {"cost": None}},
        {"last_http_status": 200},
        {"actual_model": "unknown"},
        {"request_attempts": 0},
        {"last_retry_count": 1},
        {"response_id": "response"},
    ],
)
def test_malformed_identity_usage_or_non_timeout_never_becomes_hold(tmp_path, change):
    store, ledger, value = pending(tmp_path)
    ledger.acknowledge_timeout_policy(
        ["first-timeout"], "Explicit operator acknowledgement"
    )
    p = proof()
    ledger.reserve("bad-attempt", value, bound_proof=p)
    m = timeout("bad-attempt", value)
    m.update(change)
    assert not ledger.try_hold_timeout("bad-attempt", m)
    ledger.block("paid_usage_unresolved")
    assert ledger.snapshot()["blocked"]


def test_missing_bound_proof_cannot_be_acknowledged_or_silently_invented(tmp_path):
    store, ledger, value = pending(tmp_path, bound=False)
    with pytest.raises(BudgetUnavailable, match="bounds"):
        ledger.acknowledge_timeout_policy(["first-timeout"], "Explicit acknowledgement")
    assert (
        ledger.snapshot()["blocked"] and D(ledger.snapshot()["reserved_usd"]) == value
    )


def legacy_freeze(code_hash="b" * 64):
    from src.ingest import digest

    details = {
        "parameters": {
            "configured_model": "openai/gpt-4.1-mini",
            "generation_cost_policy": "paid_test_only",
            "max_retries": 0,
            "generation": {
                "assessment_max_tokens": 500,
                "draft_max_tokens": 1400,
                "validation_max_tokens": 1400,
            },
        }
    }
    frozen = {
        "code_hash": code_hash,
        "configured_model": "openai/gpt-4.1-mini",
        "configured_artifacts": {"details": details, "sha256": digest(details)},
    }
    frozen["protocol_hash"] = digest(frozen)
    return frozen


def legacy_metadata(call, value):
    result = timeout(call, value)
    result.update(
        configured_model="openai/gpt-4.1-mini",
        last_task="draft",
        verified_endpoints=[
            {
                "provider": "OpenAI",
                "pricing": {"prompt": ".0000004", "completion": ".0000016"},
            }
        ],
        routing_price_limits={"prompt": ".4", "completion": "1.6"},
    )
    return result


def legacy_pending(tmp_path):
    store = Store(tmp_path / "audit.db")
    ledger = BudgetLedger(store)
    value = D(".0057888")
    ledger.reserve("legacy-timeout", value)
    ledger.block("paid_usage_unresolved")
    event(store, legacy_metadata("legacy-timeout", value), code_hash="b" * 64)
    return store, ledger, value


def test_legacy_exact_frozen_formula_ack_has_explicit_provenance(tmp_path):
    store, ledger, value = legacy_pending(tmp_path)
    frozen = legacy_freeze()
    with pytest.raises(BudgetUnavailable):
        ledger.acknowledge_timeout_policy(
            ["legacy-timeout"],
            "Explicit unknown reservation acknowledgement",
            legacy_code_hash="c" * 64,
            legacy_freeze=frozen,
        )
    state = ledger.acknowledge_timeout_policy(
        ["legacy-timeout"],
        "Explicit unknown reservation acknowledgement",
        legacy_code_hash="b" * 64,
        legacy_freeze=frozen,
    )
    assert (
        not state["blocked"]
        and D(state["held_unknown_usd"]) == value
        and D(state["spent_usd"]) == 0
    )
    with store.connection() as db:
        evidence = json.loads(
            db.execute("SELECT evidence FROM paid_ui_timeout_holds").fetchone()[0]
        )
        assert (
            evidence["proof_method"] == "explicit_frozen_legacy_code_review"
            and evidence["actual_cost"] == "unknown"
        )
        bound = json.loads(
            db.execute("SELECT proof FROM paid_ui_request_bounds").fetchone()[0]
        )
        assert bound["request_bytes"] == 4776 and bound["max_tokens"] == 1400
        assert bound["legacy_freeze_protocol_hash"] == frozen["protocol_hash"]


def test_code_hash_alone_cannot_invent_legacy_token_limits(tmp_path):
    store, ledger, value = legacy_pending(tmp_path)
    with pytest.raises(BudgetUnavailable, match="bounds"):
        ledger.acknowledge_timeout_policy(
            ["legacy-timeout"], "Explicit acknowledgement", legacy_code_hash="b" * 64
        )
    assert (
        ledger.snapshot()["blocked"] and D(ledger.snapshot()["reserved_usd"]) == value
    )


@pytest.mark.parametrize(
    "change",
    [
        "code_hash",
        "protocol_hash",
        "missing_caps",
        "wrong_model",
        "fingerprint",
        "invalid_cap",
    ],
)
def test_legacy_freeze_mismatches_refuse_without_accounting_changes(tmp_path, change):
    from src.ingest import digest

    store, ledger, value = legacy_pending(tmp_path)
    frozen = legacy_freeze()
    if change == "code_hash":
        frozen["code_hash"] = "c" * 64
    elif change == "protocol_hash":
        frozen["protocol_hash"] = "0" * 64
    elif change == "missing_caps":
        del frozen["configured_artifacts"]["details"]["parameters"]["generation"][
            "draft_max_tokens"
        ]
    elif change == "wrong_model":
        frozen["configured_model"] = "openai/gpt-4o-mini"
    elif change == "fingerprint":
        frozen["configured_artifacts"]["sha256"] = "0" * 64
    elif change == "invalid_cap":
        frozen["configured_artifacts"]["details"]["parameters"]["generation"][
            "draft_max_tokens"
        ] = True
    if change in ("missing_caps", "invalid_cap"):
        frozen["configured_artifacts"]["sha256"] = digest(
            frozen["configured_artifacts"]["details"]
        )
    # Even a structurally rehashed but inconsistent protocol is rejected.
    if change not in ("protocol_hash",):
        frozen["protocol_hash"] = digest(
            {k: v for k, v in frozen.items() if k != "protocol_hash"}
        )
    with pytest.raises(BudgetUnavailable):
        ledger.acknowledge_timeout_policy(
            ["legacy-timeout"],
            "Explicit acknowledgement",
            legacy_code_hash="b" * 64,
            legacy_freeze=frozen,
        )
    state = ledger.snapshot()
    assert (
        state["blocked"]
        and D(state["reserved_usd"]) == value
        and D(state["spent_usd"]) == 0
    )


def test_cli_legacy_freeze_reads_exact_protocol_without_any_token_defaults(tmp_path):
    from scripts.paid_timeout_policy import read_legacy_freeze

    frozen = legacy_freeze()
    path = tmp_path / "pilot-freeze.json"
    path.write_text(json.dumps(frozen))
    assert read_legacy_freeze(path, "b" * 64) == frozen
    with pytest.raises(BudgetUnavailable):
        read_legacy_freeze(path, "c" * 64)
    frozen["configured_artifacts"]["details"]["parameters"]["generation"][
        "draft_max_tokens"
    ] = 700
    path.write_text(json.dumps(frozen))
    with pytest.raises(BudgetUnavailable):
        read_legacy_freeze(path, "b" * 64)


def test_full_gpt41_bound_uses_shared_approved_model_ceiling_without_overwriting_models(
    tmp_path,
):
    from src.providers.paid_test import PaidTestProvider

    before = dict(PaidTestProvider.allowed_models)
    store = Store(tmp_path / "audit.db")
    ledger = BudgetLedger(store)
    bound = proof()
    bound.update(
        model="openai/gpt-4.1", prompt_price=".000002", completion_price=".000008"
    )
    ledger.reserve("full-model", estimate(bound), bound_proof=bound)
    assert PaidTestProvider.allowed_models == before and "openai/gpt-4.1" in before


def test_other_block_reason_or_audit_barrier_cannot_be_cleared(tmp_path):
    store, ledger, value = pending(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE paid_ui_budget SET reason='paid_actual_cost_unverified'")
    with pytest.raises(BudgetUnavailable, match="Unrelated"):
        ledger.acknowledge_timeout_policy(["first-timeout"], "Explicit acknowledgement")
    with store.connection() as db:
        db.execute("UPDATE paid_ui_budget SET reason='paid_usage_unresolved'")
    ledger.barrier.write_text("Unknown audit state")
    with pytest.raises(BudgetUnavailable, match="barrier"):
        ledger.acknowledge_timeout_policy(["first-timeout"], "Explicit acknowledgement")


def test_all_pending_unknown_liabilities_must_be_proven_and_acknowledged(tmp_path):
    store, ledger, value = pending(tmp_path)
    # Simulate an abandoned second reservation without timeout proof.
    with store.connection() as db:
        db.execute(
            "INSERT INTO paid_ui_reservations VALUES('another', '.01', NULL,'reserved','now',NULL)"
        )
    with pytest.raises(BudgetUnavailable, match="Every"):
        ledger.acknowledge_timeout_policy(["first-timeout"], "Explicit acknowledgement")
    assert ledger.snapshot()["blocked"]
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM paid_ui_timeout_policy").fetchone()[0] == 0
        )


def test_policy_cli_requires_ack_stopped_idle_lock_and_no_inference(tmp_path):
    settings = Settings(storage=tmp_path)
    store = Store(settings.db_path)
    ledger = BudgetLedger(store)
    p = proof()
    v = estimate(p)
    ledger.reserve("timeout", v, bound_proof=p)
    ledger.block("paid_usage_unresolved")
    event(store, timeout("timeout", v))
    with pytest.raises(BudgetUnavailable, match="Explicit"):
        acknowledge(settings, ["timeout"], "Explicit acknowledgement")
    store.set_control(True, "Synthetic enabled")
    with pytest.raises(BudgetUnavailable, match="Stop"):
        acknowledge(settings, ["timeout"], "Explicit acknowledgement", True)
    store.set_control(False, "Synthetic stopped")
    with FileLock(str(tmp_path / "pipeline.lock")):
        with pytest.raises(BudgetUnavailable, match="busy"):
            acknowledge(settings, ["timeout"], "Explicit acknowledgement", True)
    result = acknowledge(
        settings,
        ["timeout"],
        "User approved full reservation liability 2026-11-22",
        True,
    )
    assert "UNKNOWN" in result["notice"] and result["budget"]["held_unknown_count"] == 1


def test_verified_actual_settlement_resolves_hold_without_inventing_the_estimate_as_cost(
    tmp_path,
):
    store, ledger, value = pending(tmp_path)
    ledger.acknowledge_timeout_policy(
        ["first-timeout"], "Explicit full reservation acknowledgement"
    )
    # Simulates a subsequently VERIFIED provider actual-cost report, not a timeout estimate.
    ledger.settle("first-timeout", D(".0004"))
    state = ledger.snapshot()
    assert D(state["spent_usd"]) == D(".0004") and D(state["reserved_usd"]) == 0
    assert state["held_unknown_count"] == 0 and not state["spent_is_known_lower_bound"]


def test_policy_audit_failure_rolls_back_and_latches_instead_of_releasing_funds(
    tmp_path,
):
    store, ledger, value = pending(tmp_path)
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER reject_policy_audit BEFORE INSERT ON paid_ui_timeout_policy_events BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END"
        )
    with pytest.raises(BudgetUnavailable, match="audit"):
        ledger.acknowledge_timeout_policy(
            ["first-timeout"], "Explicit full reservation acknowledgement"
        )
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM paid_ui_timeout_policy").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM paid_ui_timeout_holds").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT state FROM paid_ui_reservations").fetchone()[0]
            == "reserved"
        )
    assert ledger.barrier.exists() and store.barrier.exists()


def test_provider_future_timeout_is_still_error_but_durable_full_hold_can_continue(
    monkeypatch, tmp_path
):
    import httpx
    from test_budgeted_paid_provider import fixture, call, MODEL
    from src.providers.budgeted_paid import BudgetedPaidProvider
    from src.providers.openrouter import ProviderUnavailable
    import src.providers.budgeted_paid as module

    def offline_timeout(request):
        raise httpx.ReadTimeout("fixture timeout")

    _, _, posts, _ = fixture(monkeypatch, post=offline_timeout)
    monkeypatch.setattr(module, "_new_ledger", BudgetLedger)
    store, ledger, value = pending(tmp_path)
    ledger.acknowledge_timeout_policy(
        ["first-timeout"], "User approved full reservation liability"
    )
    provider = BudgetedPaidProvider("fixture", MODEL, store)
    provider.begin_record("future-record")
    with pytest.raises(ProviderUnavailable):
        call(provider)
    state = ledger.snapshot()
    assert not state["blocked"] and state["held_unknown_count"] == 2 and len(posts) == 1
    assert (
        provider.metadata["last_call_status"] == "failed"
        and provider.metadata["last_error"] == "network_timeout"
    )
    assert (
        provider.metadata["paid_timeout_cost_status"] == "unknown_full_reservation_held"
    )
    assert provider.metadata.get("usage") is None
    assert state["spent_is_known_lower_bound"] and D(state["spent_usd"]) == 0
    provider.begin_record("next-record")
    with pytest.raises(ProviderUnavailable):
        call(provider)
    assert (
        not ledger.snapshot()["blocked"]
        and ledger.snapshot()["held_unknown_count"] == 3
    )


def test_enabled_policy_does_not_hold_malformed_provider_usage(monkeypatch, tmp_path):
    import httpx
    from test_budgeted_paid_provider import fixture, call, response, MODEL
    from src.providers.budgeted_paid import BudgetedPaidProvider
    from src.providers.openrouter import ProviderUnavailable
    import src.providers.budgeted_paid as module

    fixture(
        monkeypatch,
        post=lambda _: httpx.Response(200, json=response(usage={"cost": "NaN"})),
    )
    monkeypatch.setattr(module, "_new_ledger", BudgetLedger)
    store, ledger, value = pending(tmp_path)
    ledger.acknowledge_timeout_policy(
        ["first-timeout"], "User approved full reservation liability"
    )
    provider = BudgetedPaidProvider("fixture", MODEL, store)
    provider.begin_record("malformed-record")
    with pytest.raises(ProviderUnavailable):
        call(provider)
    assert ledger.snapshot()["blocked"] and ledger.snapshot()["held_unknown_count"] == 1


def test_cli_legacy_freeze_path_uses_explicit_historical_1400_cap(tmp_path):
    settings = Settings(storage=tmp_path)
    store = Store(settings.db_path)
    ledger = BudgetLedger(store)
    value = D(".0057888")
    ledger.reserve("legacy-timeout", value)
    ledger.block("paid_usage_unresolved")
    event(store, legacy_metadata("legacy-timeout", value), code_hash="b" * 64)
    path = tmp_path / "historical-pilot-freeze.json"
    path.write_text(json.dumps(legacy_freeze()))
    result = acknowledge(
        settings,
        ["legacy-timeout"],
        "User approved full reservation liability 2026-11-22",
        True,
        "b" * 64,
        path,
    )
    assert (
        not result["budget"]["blocked"]
        and D(result["budget"]["held_unknown_usd"]) == value
    )
    with store.connection() as db:
        bound = json.loads(
            db.execute("SELECT proof FROM paid_ui_request_bounds").fetchone()[0]
        )
        assert bound["max_tokens"] == 1400 and bound["request_bytes"] == 4776


@pytest.mark.parametrize("current", [0, False, "1", None, 2])
def test_timeout_optional_per_call_counter_must_be_valid_positive_and_within_total(
    current,
):
    m = timeout("call", D(".01"))
    m["call_request_attempts"] = current
    assert not BudgetLedger._timeout_metadata(m, "call", D(".01"))


def test_timeout_optional_single_post_counter_is_accepted():
    m = timeout("call", D(".01"))
    m["call_request_attempts"] = 1
    assert BudgetLedger._timeout_metadata(m, "call", D(".01"))
