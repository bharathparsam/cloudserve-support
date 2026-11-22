"""Paid evaluation gates tested with isolated ledgers and no network/inference."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json, uuid
import pytest
from src.config import Settings
from src.logging_store import Store
from src.paid_budget import BudgetLedger
from src.ingest import now, digest
from evaluation.harness import (
    run,
    final_gate,
    paid_development_gate,
    configured_fingerprint,
    tree_hash,
    paid_authorization,
    frozen_body_boundary,
    credit_reserve_with_holds,
)
from scripts.preflight import empirical_smoke, paid_reserve_estimate, build_preflight
from scripts.freeze import freeze


class PaidFixture:
    model = "openai/gpt-4.1-mini"
    api_key = "fixture"
    max_retries = 0

    def __init__(self, store):
        self.ledger = BudgetLedger(store)
        self.metadata = {
            "fixture": True,
            "configured_model": self.model,
            "cost_policy": "paid_test_only",
        }
        self.credit = True

    def preflight(self):
        return {
            **self.metadata,
            "verified": True,
            "model_pricing": {"prompt": "0.0000004", "completion": "0.0000016"},
        }

    def verify_credit(self, required):
        return {
            "verified": True,
            "sufficient": self.credit,
            "checked_at": now(),
            "reason": None if self.credit else "account_credit_insufficient",
        }


def setup(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "requirements.txt").write_text("fixture==1\n")
    settings = Settings(root=root, storage=tmp_path / "storage")
    settings.model = PaidFixture.model
    settings.generation_cost_policy = "paid_test_only"
    settings.generation_mode = "paid_test"
    settings.max_retries = 0
    settings.paid_test_budget_usd = "3.50"
    store = Store(settings.db_path)
    provider = PaidFixture(store)
    pipeline = SimpleNamespace(
        provider=provider,
        classifier=SimpleNamespace(ready=True),
        retriever=SimpleNamespace(ready=True),
        policy={"calibration_ready": True, "retrieval_ready": True},
        evaluator=None,
        decider_mode="disabled",
    )
    store.set_control(True, "Synthetic gate enable")
    return settings, store, pipeline


def smoke(store, kind="manual"):
    rid = store.create_run(
        [{"channel": "chat", "body": "synthetic development smoke"}], kind=kind
    )
    with store.connection() as db:
        record = db.execute(
            "SELECT record_id FROM input_records WHERE run_id=?", (rid,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO outcomes VALUES(?,?,'auto_respond',0,'{}')", (record, rid)
        )
        db.execute("UPDATE jobs SET state='done' WHERE run_id=?", (rid,))
        db.execute(
            "UPDATE runs SET state='completed',processed_count=1,started_at=?,completed_at=? WHERE run_id=?",
            (now(), now(), rid),
        )
    for stage, key, cost in [
        ("sensitivity", "provider", ".0002"),
        ("generation", "model", ".001"),
        ("validation", "provider", ".0008"),
    ]:
        store.event(
            record,
            rid,
            stage,
            {
                key: {
                    "configured_model": PaidFixture.model,
                    "actual_model": PaidFixture.model,
                    "cost_policy": "paid_test_only",
                    "last_call_id": str(uuid.uuid4()),
                    "last_call_status": "success",
                    "usage": {"cost": cost},
                }
            },
        )
    return rid


def test_default_paid_final_refuses_before_any_input_read(tmp_path, monkeypatch):
    runtime = setup(tmp_path)
    monkeypatch.setattr(
        "evaluation.harness.load_records",
        lambda _: pytest.fail("Input must remain unopened"),
    )
    with pytest.raises(RuntimeError, match="only to development"):
        run(
            tmp_path / "unread.json",
            tmp_path / "output",
            runtime=runtime,
            kind="supplied_validation",
        )


def test_paid_development_needs_explicit_flag_and_smoke_preflight(tmp_path):
    settings, store, p = setup(tmp_path)
    with pytest.raises(RuntimeError, match="explicit"):
        paid_development_gate(settings, store, p)
    with pytest.raises(RuntimeError, match="smoke"):
        paid_development_gate(settings, store, p, True)
    with pytest.raises(RuntimeError, match="smoke"):
        build_preflight(settings, store, p, 500, authorize_paid_evaluation=True)


def test_smoke_metadata_deduplicates_and_never_accepts_validation_cohort(tmp_path):
    settings, store, p = setup(tmp_path)
    rid = smoke(store)
    result = empirical_smoke(store, [rid, rid], p.provider.model)
    assert result["observed_calls"] == 3 and Decimal(
        result["observed_total_cost_usd"]
    ) == Decimal(".002")
    # Kind is rejected before touching any event metadata from that cohort.
    forbidden = smoke(store, kind="supplied_validation")
    with pytest.raises(RuntimeError, match="forbidden"):
        empirical_smoke(store, [forbidden], p.provider.model)


def test_empirical_estimate_is_not_guarantee_and_preserves_validation_reserve():
    result = paid_reserve_estimate(
        {"maximum_observed_answer_record_cost_usd": ".002"}, 500, ".50"
    )
    assert (
        Decimal(result["estimated_reserve_usd"]) == Decimal("2.50")
        and result["estimate_is_guarantee"] is False
    )


def test_paid_development_preflight_credit_budget_and_authorization_checks(tmp_path):
    settings, store, p = setup(tmp_path)
    rid = smoke(store)
    checked = build_preflight(
        settings,
        store,
        p,
        500,
        authorize_paid_evaluation=True,
        smoke_run_ids=[rid],
        validation_reserve_usd=".50",
    )
    assert checked["paid_budget_sufficient"] and not checked["quota_sufficient"]
    assert (
        "NFR-07" in checked["specification_deviation"]
        and checked["credit_check"]["verified"]
    )
    (settings.storage / "preflight.json").write_text(json.dumps(checked))
    assert paid_development_gate(settings, store, p, True)["record_budget"] == 500
    p.provider.credit = False
    with pytest.raises(RuntimeError, match="credit"):
        paid_development_gate(settings, store, p, True)


def test_paid_final_exact_protocol_stable_auth_and_current_budget(tmp_path):
    settings, store, p = setup(tmp_path)
    rid = smoke(store)
    frozen = freeze(
        settings,
        settings.storage / "freeze.json",
        authorize_paid_evaluation=True,
        store=store,
    )
    checked = build_preflight(
        settings,
        store,
        p,
        80,
        scope="supplied_validation",
        authorize_paid_evaluation=True,
        smoke_run_ids=[rid],
    )
    (settings.storage / "preflight.json").write_text(json.dumps(checked))
    assert final_gate(settings, store, p, True)["paid_evaluation_authorized"]
    original_auth = paid_authorization(store, p.provider)["sha256"]
    p.provider.ledger.reserve("simulated-later-spend", Decimal(".10"))
    p.provider.ledger.settle("simulated-later-spend", Decimal(".05"))
    assert paid_authorization(store, p.provider)["sha256"] == original_auth
    assert final_gate(settings, store, p, True)["record_budget"] == 80
    frozen["human_review_sampling"] = "changed after preflight"
    frozen["protocol_hash"] = digest(
        {k: v for k, v in frozen.items() if k != "protocol_hash"}
    )
    (settings.storage / "freeze.json").write_text(json.dumps(frozen))
    with pytest.raises(RuntimeError, match="exact frozen protocol"):
        final_gate(settings, store, p, True)


def test_paid_final_declines_insufficient_remaining_even_if_old_preflight_passed(
    tmp_path,
):
    settings, store, p = setup(tmp_path)
    rid = smoke(store)
    freeze(
        settings,
        settings.storage / "freeze.json",
        authorize_paid_evaluation=True,
        store=store,
    )
    checked = build_preflight(
        settings,
        store,
        p,
        80,
        scope="supplied_validation",
        authorize_paid_evaluation=True,
        smoke_run_ids=[rid],
    )
    (settings.storage / "preflight.json").write_text(json.dumps(checked))
    p.provider.ledger.reserve("another-operation", Decimal("3.2"))
    with pytest.raises(RuntimeError, match="current|Current|reserve"):
        final_gate(settings, store, p, True)


def test_free_preflight_never_treats_paid_credit_as_free_quota(tmp_path):
    settings, store, p = setup(tmp_path)
    settings.generation_cost_policy = "free_only"
    result = build_preflight(settings, store, p, 80)
    assert not result["quota_sufficient"] and not result["paid_evaluation_authorized"]


def test_frozen_boundary_is_shared_and_invalid_values_fail(tmp_path):
    settings, store, p = setup(tmp_path)
    assert frozen_body_boundary(settings) is None
    (settings.storage / "freeze.json").write_text(
        '{"short_long_body_character_boundary":123.5}'
    )
    assert frozen_body_boundary(settings) == 123.5
    (settings.storage / "freeze.json").write_text(
        '{"short_long_body_character_boundary":true}'
    )
    with pytest.raises(RuntimeError):
        frozen_body_boundary(settings)


@pytest.mark.parametrize("held", ["NaN", "Infinity", "-0.01", None])
def test_credit_reserve_refuses_invalid_unknown_holds(held):
    with pytest.raises(RuntimeError, match="held-unknown"):
        credit_reserve_with_holds(Decimal("2"), {"held_unknown_usd": held})


def test_credit_reserve_keeps_full_unknown_hold():
    assert credit_reserve_with_holds(
        Decimal("2"), {"held_unknown_usd": ".0057888"}
    ) == Decimal("2.0057888")
