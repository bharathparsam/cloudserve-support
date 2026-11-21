"""Durable accounting tests use isolated SQLite and never send inference requests."""

from decimal import Decimal as D
import json, sqlite3, threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from src.logging_store import Store
from src.paid_budget import BudgetLedger, BudgetExceeded, BudgetUnavailable


def store(tmp_path):
    return Store(tmp_path / "decisions.db")


def paid_event(s, stage, key, call, cost):
    # Seed only provider metadata; no original input records are needed or read.
    data = {
        key: {
            "cost_policy": "paid_test_only",
            "last_call_id": call,
            "usage": {"cost": cost},
        }
    }
    with s.connection() as db:
        db.execute(
            "INSERT INTO decision_events VALUES(?,?,?,?,?,?)",
            (
                str(__import__("uuid").uuid4()),
                "2026-11-20T00:00:00+00:00",
                None,
                None,
                stage,
                json.dumps(data),
            ),
        )


def test_three_historical_metadata_patterns_seed_exact_actual_once(tmp_path):
    s = store(tmp_path)
    paid_event(s, "sensitivity", "provider", "call-assess", "0.0001788")
    paid_event(s, "generation", "model", "call-draft", "0.0012644")
    paid_event(s, "validation", "provider", "call-check", "0.000906")
    paid_event(s, "validation", "provider", "call-check", "0.000906")
    ledger = BudgetLedger(s)
    state = ledger.snapshot()
    assert D(state["spent_usd"]) == D("0.0023492")
    assert D(state["remaining_usd"]) == D("0.2476508") and not state["blocked"]
    assert BudgetLedger(Store(s.path)).snapshot()["spent_usd"] == state["spent_usd"]
    assert ledger.snapshot()["spent_usd"] == state["spent_usd"]


def test_decimal_reservation_settlement_idempotency_and_restart(tmp_path):
    s = store(tmp_path)
    ledger = BudgetLedger(s)
    assert D(ledger.reserve("request-1", D(".10"))["reserved_usd"]) == D(".10")
    assert D(ledger.reserve("request-1", D(".10"))["reserved_usd"]) == D(".10")
    assert D(BudgetLedger(Store(s.path)).snapshot()["reserved_usd"]) == D(".10")
    result = ledger.settle("request-1", D(".00001234"))
    assert D(result["spent_usd"]) == D(".00001234") and D(result["reserved_usd"]) == 0
    assert ledger.settle("request-1", D(".00001234")) == result
    paid_event(s, "generation", "model", "request-1", ".00001234")
    assert D(ledger.snapshot()["spent_usd"]) == D(".00001234")
    with pytest.raises(BudgetUnavailable):
        ledger.reserve("request-1", D(".10"))
    assert not ledger.snapshot()["blocked"]


def test_different_records_and_instances_share_fixed_cap(tmp_path):
    s = store(tmp_path)
    a = BudgetLedger(s)
    b = BudgetLedger(Store(s.path))
    a.reserve("record1-call", D(".15"))
    b.reserve("record2-call", D(".10"))
    assert D(a.snapshot()["remaining_usd"]) == 0
    with pytest.raises(BudgetExceeded):
        b.reserve("record3-call", D(".000001"))
    assert not b.snapshot()["blocked"]
    a.settle("record1-call", D(".05"))
    assert D(b.snapshot()["remaining_usd"]) == D(".10")


def test_atomic_concurrent_reservations_never_overspend(tmp_path):
    s = store(tmp_path)
    BudgetLedger(s).snapshot()
    barrier = threading.Barrier(6)

    def reserve(i):
        ledger = BudgetLedger(Store(s.path))
        barrier.wait()
        try:
            ledger.reserve("parallel-" + str(i), D(".05"))
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))
    assert sum(results) == 5 and D(BudgetLedger(s).snapshot()["reserved_usd"]) == D(
        ".25"
    )


@pytest.mark.parametrize("cost", [None, True, "NaN", "Infinity", "-.01", "invalid"])
def test_unknown_actual_blocks_and_retains_crash_liability(tmp_path, cost):
    s = store(tmp_path)
    ledger = BudgetLedger(s)
    ledger.reserve("call", D(".05"))
    with pytest.raises(BudgetUnavailable):
        ledger.settle("call", cost)
    snapshot = BudgetLedger(Store(s.path)).snapshot()
    assert (
        snapshot["blocked"]
        and D(snapshot["reserved_usd"]) == D(".05")
        and D(snapshot["spent_usd"]) == 0
    )
    with pytest.raises(BudgetUnavailable):
        ledger.reserve("another", D(".01"))


def test_actual_over_estimate_records_cost_before_blocking(tmp_path):
    s = store(tmp_path)
    ledger = BudgetLedger(s)
    ledger.reserve("call", D(".01"))
    with pytest.raises(BudgetUnavailable):
        ledger.settle("call", D(".02"))
    state = BudgetLedger(Store(s.path)).snapshot()
    assert (
        state["blocked"]
        and D(state["spent_usd"]) == D(".02")
        and D(state["reserved_usd"]) == 0
    )
    assert ledger.settle("call", D(".02")) == state


def test_conflicting_settlement_cannot_erase_charge(tmp_path):
    ledger = BudgetLedger(store(tmp_path))
    ledger.reserve("call", D(".05"))
    ledger.settle("call", D(".01"))
    with pytest.raises(BudgetUnavailable):
        ledger.settle("call", D(".02"))
    assert ledger.snapshot()["blocked"] and D(ledger.snapshot()["spent_usd"]) == D(
        ".01"
    )


@pytest.mark.parametrize("cost", [None, "NaN", "-.1", True])
def test_unknown_historical_paid_cost_latches_persistently(tmp_path, cost):
    s = store(tmp_path)
    paid_event(s, "sensitivity", "provider", "legacy", cost)
    ledger = BudgetLedger(s)
    assert ledger.snapshot()["blocked"]
    with pytest.raises(BudgetUnavailable):
        ledger.reserve("new", D(".01"))
    assert BudgetLedger(Store(s.path)).snapshot()["blocked"]


def test_conflicting_historical_cost_and_cap_excess_block(tmp_path):
    s = store(tmp_path)
    paid_event(s, "generation", "model", "legacy", ".20")
    paid_event(s, "validation", "provider", "legacy", ".21")
    assert BudgetLedger(s).snapshot()["blocked"]
    assert D(BudgetLedger(s).snapshot()["spent_usd"]) == D(".20")


def test_settlement_write_failure_rolls_back_and_latches_across_restart(tmp_path):
    s = store(tmp_path)
    ledger = BudgetLedger(s)
    ledger.reserve("call", D(".05"))
    with s.connection() as db:
        db.execute(
            "CREATE TRIGGER deny_settlement BEFORE UPDATE ON paid_ui_reservations BEGIN SELECT RAISE(ABORT,'simulated full device'); END"
        )
    with pytest.raises(BudgetUnavailable, match="storage unavailable"):
        ledger.settle("call", D(".01"))
    with s.connection() as db:
        assert db.execute("SELECT spent_usd FROM paid_ui_budget").fetchone()[0] == "0"
        assert (
            db.execute("SELECT state FROM paid_ui_reservations").fetchone()[0]
            == "reserved"
        )
        assert db.execute("SELECT COUNT(*) FROM paid_ui_seed_calls").fetchone()[0] == 0
        db.execute("DROP TRIGGER deny_settlement")
    assert s._fail_closed and ledger.barrier.exists()
    restarted = BudgetLedger(Store(s.path))
    state = restarted.snapshot()
    assert state["blocked"] and D(state["reserved_usd"]) == D(".05")
    with pytest.raises(BudgetUnavailable):
        restarted.reserve("next", D(".01"))


def test_reservation_write_failure_does_not_create_authorization(tmp_path):
    s = store(tmp_path)
    ledger = BudgetLedger(s)
    ledger.snapshot()
    with s.connection() as db:
        db.execute(
            "CREATE TRIGGER deny_reserve BEFORE INSERT ON paid_ui_reservations BEGIN SELECT RAISE(ABORT,'simulated failure'); END"
        )
    with pytest.raises(BudgetUnavailable):
        ledger.reserve("call", D(".10"))
    with s.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM paid_ui_reservations").fetchone()[0] == 0
        )
    assert ledger.snapshot()["blocked"]


def test_no_original_inputs_are_read_for_seeding(tmp_path):
    s = store(tmp_path)
    paid_event(s, "sensitivity", "provider", "legacy", ".001")
    # SQLite authorizer rejects any read of originals; budget metadata must work.
    original_connection = s.connection
    from contextlib import contextmanager

    @contextmanager
    def guarded():
        with original_connection() as db:
            db.set_authorizer(
                lambda action, a, b, c, d: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and a == "input_records"
                    else sqlite3.SQLITE_OK
                )
            )
            yield db

    s.connection = guarded
    assert D(BudgetLedger(s).snapshot()["spent_usd"]) == D(".001")


def test_block_reasons_are_generic_and_no_reset_or_topup_api(tmp_path):
    ledger = BudgetLedger(store(tmp_path))
    state = ledger.block("raw private error alice@example.com")
    assert state["reason"] == "paid_budget_policy_failure" and state["blocked"]
    assert not hasattr(ledger, "reset") and not hasattr(ledger, "top_up")


def test_historical_actual_above_total_cap_is_preserved_and_blocks(tmp_path):
    s = store(tmp_path)
    paid_event(s, "generation", "model", "legacy", ".30001")
    snapshot = BudgetLedger(s).snapshot()
    assert (
        snapshot["blocked"]
        and D(snapshot["spent_usd"]) == D(".30001")
        and D(snapshot["remaining_usd"]) == 0
    )


def test_reusing_reservation_id_with_changed_estimate_blocks_without_mutation(tmp_path):
    ledger = BudgetLedger(store(tmp_path))
    ledger.reserve("call", D(".10"))
    with pytest.raises(BudgetUnavailable):
        ledger.reserve("call", D(".11"))
    state = ledger.snapshot()
    assert state["blocked"] and D(state["reserved_usd"]) == D(".10")


def test_paid_metadata_missing_stable_call_identity_is_unknown_not_free(tmp_path):
    s = store(tmp_path)
    paid_event(s, "validation", "provider", None, ".001")
    state = BudgetLedger(s).snapshot()
    assert state["blocked"] and state["reason"] == "historical_paid_cost_unverified"
    assert D(state["spent_usd"]) == 0


def refusal_event(s, **changes):
    metadata = {
        "cost_policy": "paid_test_only",
        "last_call_id": "refused-call",
        "request_attempts": 0,
        "paid_request_count": 1,
        "last_call_status": "failed",
        "last_error": "paid_budget_reservation_rejected",
    }
    metadata.update(changes)
    with s.connection() as db:
        db.execute(
            "INSERT INTO decision_events VALUES(?,?,?,?,?,?)",
            (
                str(__import__("uuid").uuid4()),
                "2026-11-20T00:00:00+00:00",
                None,
                None,
                "sensitivity",
                json.dumps({"provider": metadata}),
            ),
        )


@pytest.mark.parametrize(
    "error",
    [
        "paid_budget_reservation_rejected",
        "paid_budget_exhausted",
        "paid_budget_blocked",
        "preflight_unavailable",
        "paid_test_policy_rejected",
    ],
)
def test_known_zero_post_refusal_is_not_an_unknown_paid_charge(tmp_path, error):
    s = store(tmp_path)
    paid_event(s, "generation", "model", "legacy-paid", ".247")
    refusal_event(s, last_error=error)
    ledger = BudgetLedger(s)
    state = ledger.snapshot()
    assert (
        not state["blocked"]
        and D(state["spent_usd"]) == D(".247")
        and D(state["remaining_usd"]) == D(".003")
    )
    # A less expensive allowed model's estimate can still be admitted.
    ledger.reserve("cheaper-call", D(".002"))
    ledger.settle("cheaper-call", D(".001"))
    assert not BudgetLedger(Store(s.path)).snapshot()["blocked"]
    assert D(ledger.snapshot()["spent_usd"]) == D(".248")


@pytest.mark.parametrize("attempts", [None, 1, False, "0", 0.0, -1])
def test_unproven_or_positive_attempt_refusal_still_blocks(tmp_path, attempts):
    s = store(tmp_path)
    refusal_event(s, request_attempts=attempts)
    assert BudgetLedger(s).snapshot()["blocked"]


@pytest.mark.parametrize(
    "changes",
    [
        {"last_call_status": "pending"},
        {"last_error": "network_timeout"},
        {"last_http_status": 500},
        {"usage": {"cost": "0.001"}},
        {"response_id": "provider-response"},
    ],
)
def test_contradictory_zero_attempt_refusal_is_not_skipped(tmp_path, changes):
    s = store(tmp_path)
    refusal_event(s, **changes)
    state = BudgetLedger(s).snapshot()
    if "usage" in changes:
        # An explicit known charge is still charged, never silently skipped.
        assert D(state["spent_usd"]) == D(".001")
    else:
        assert state["blocked"]


def test_zero_attempt_claim_cannot_discard_an_outstanding_reservation(tmp_path):
    s = store(tmp_path)
    ledger = BudgetLedger(s)
    ledger.reserve("refused-call", D(".05"))
    refusal_event(s)
    state = ledger.snapshot()
    assert (
        state["blocked"]
        and D(state["reserved_usd"]) == D(".05")
        and D(state["spent_usd"]) == 0
    )
    with pytest.raises(BudgetUnavailable):
        ledger.reserve("another-call", D(".01"))


def test_zero_post_catalog_refusal_without_call_id_is_provably_unbilled(tmp_path):
    s = store(tmp_path)
    refusal_event(
        s, last_call_id=None, last_error="preflight_unavailable", paid_request_count=0
    )
    assert not BudgetLedger(s).snapshot()["blocked"]
