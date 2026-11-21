"""One durable USD 0.25 paid-development allowance across tickets and restarts.

Only paid provider metadata is read from historic events. Amounts are decimal
strings in SQLite; no customer input, credentials or account balances are stored.
"""

from __future__ import annotations
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
import json, os, re

LIMIT = Decimal("0.25")


class BudgetUnavailable(RuntimeError):
    """Paid use is blocked or its durable accounting cannot be established."""


class BudgetExceeded(BudgetUnavailable):
    """No reservation was made because the remaining allowance is insufficient."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _amount(value):
    if isinstance(value, bool):
        raise ValueError("Invalid decimal amount")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid decimal amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid decimal amount")
    return amount


class BudgetLedger:
    def __init__(self, store):
        self.store = store
        self.barrier = Path(str(store.path) + ".paid-budget-blocked")
        self._storage_failed = self.barrier.exists()

    def _failure(self):
        self._storage_failed = True
        try:
            self.store._latch_closed()
        except Exception:
            pass
        try:
            with self.barrier.open("w") as stream:
                stream.write("Paid budget audit unavailable. No automatic reset.")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass

    def _schema(self, db):
        # Do not use executescript: sqlite3 executescript implicitly commits an
        # outstanding transaction and would destroy reservation atomicity.
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_budget(id INTEGER PRIMARY KEY CHECK(id=1),limit_usd TEXT NOT NULL,spent_usd TEXT NOT NULL,blocked INTEGER NOT NULL,reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_reservations(request_id TEXT PRIMARY KEY,estimate_usd TEXT NOT NULL,actual_usd TEXT,state TEXT NOT NULL CHECK(state IN ('reserved','settled')),created_at TEXT NOT NULL,settled_at TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_seed_calls(call_id TEXT PRIMARY KEY,cost_usd TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO paid_ui_budget VALUES(1,?,?,0,NULL,?,?)",
            (str(LIMIT), "0", _now(), _now()),
        )
        try:
            row = db.execute("SELECT * FROM paid_ui_budget WHERE id=1").fetchone()
            if _amount(row["limit_usd"]) != LIMIT:
                raise ValueError()
            _amount(row["spent_usd"])
        except (ValueError, TypeError):
            self._block(db, "budget_state_invalid")

    def _block(self, db, reason):
        # Reasons are finite codes, not provider error text or ticket content.
        reason = (
            reason
            if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason)
            else "paid_budget_policy_failure"
        )
        db.execute(
            "UPDATE paid_ui_budget SET blocked=1,reason=COALESCE(reason,?),updated_at=? WHERE id=1",
            (reason, _now()),
        )

    def _spent(self, db):
        return _amount(
            db.execute("SELECT spent_usd FROM paid_ui_budget WHERE id=1").fetchone()[0]
        )

    def _charge(self, db, request_id, cost):
        prior = db.execute(
            "SELECT cost_usd FROM paid_ui_seed_calls WHERE call_id=?", (request_id,)
        ).fetchone()
        if prior:
            if _amount(prior[0]) != cost:
                self._block(db, "paid_cost_conflict")
            return False
        db.execute(
            "INSERT INTO paid_ui_seed_calls VALUES(?,?,?)",
            (request_id, str(cost), _now()),
        )
        db.execute(
            "UPDATE paid_ui_budget SET spent_usd=?,updated_at=? WHERE id=1",
            (str(self._spent(db) + cost), _now()),
        )
        return True

    def _provably_unbilled(self, db, metadata):
        """Only an explicit pre-POST refusal can omit a charge record safely.

        request_attempts is a monotonically counted HTTP-POST admission counter;
        missing/coerced/positive values cannot establish that no request was sent.
        An existing pending reservation is retained as an unknown liability even
        if conflicting event metadata claims zero attempts.
        """
        attempts = metadata.get("request_attempts")
        prerequest_codes = {
            "paid_budget_exhausted",
            "paid_budget_blocked",
            "paid_budget_reservation_rejected",
            "paid_budget_unavailable",
            "preflight_unavailable",
            "paid_test_policy_rejected",
            "paid_test_record_required",
            "paid_test_request_limit",
            "paid_test_configuration_rejected",
            "paid_test_request_too_large",
            "paid_test_budget_insufficient",
            "configuration_rejected",
            "missing_key",
            "free_policy_rejected",
        }
        if type(attempts) is not int or attempts != 0:
            return False
        if (
            metadata.get("last_call_status") != "failed"
            or metadata.get("last_error") not in prerequest_codes
        ):
            return False
        if any(
            metadata.get(key) is not None
            for key in (
                "last_http_status",
                "response_id",
                "actual_model",
                "actual_provider",
            )
        ):
            return False
        usage = metadata.get("usage")
        if usage is not None:
            # A placeholder unknown cost is not contradictory, but an actual
            # cost/token response cannot be reconciled with a no-POST assertion.
            if not isinstance(usage, dict) or any(
                value is not None for value in usage.values()
            ):
                return False
        call = metadata.get("last_call_id")
        if (
            isinstance(call, str)
            and db.execute(
                "SELECT 1 FROM paid_ui_reservations WHERE request_id=? AND state='reserved'",
                (call,),
            ).fetchone()
        ):
            return False
        return True

    def _seed(self, db):
        # Explicit JSON projections ensure protected original records are never read.
        for stage, key in (
            ("sensitivity", "provider"),
            ("generation", "model"),
            ("generation", "provider"),
            ("validation", "provider"),
        ):
            query = (
                "SELECT json_extract(data,'$."
                + key
                + "') AS paid_metadata FROM decision_events WHERE stage=? AND json_extract(data,'$."
                + key
                + ".cost_policy')='paid_test_only'"
            )
            for event in db.execute(query, (stage,)).fetchall():
                try:
                    metadata = json.loads(event[0], parse_float=Decimal)
                    if self._provably_unbilled(db, metadata):
                        continue
                    call = metadata.get("last_call_id")
                    if not isinstance(call, str) or not re.fullmatch(
                        r"[A-Za-z0-9_.:-]{1,128}", call
                    ):
                        raise ValueError()
                    usage = metadata.get("usage")
                    if not isinstance(usage, dict) or "cost" not in usage:
                        raise ValueError()
                    cost = _amount(usage["cost"])
                    self._charge(db, call, cost)
                except (ValueError, TypeError, InvalidOperation):
                    self._block(db, "historical_paid_cost_unverified")
        if self._spent(db) > LIMIT:
            self._block(db, "paid_budget_exceeded")

    def _snapshot(self, db):
        row = db.execute("SELECT * FROM paid_ui_budget WHERE id=1").fetchone()
        try:
            spent = _amount(row["spent_usd"])
            reserved = sum(
                (
                    _amount(r[0])
                    for r in db.execute(
                        "SELECT estimate_usd FROM paid_ui_reservations WHERE state='reserved'"
                    )
                ),
                Decimal(0),
            )
        except (ValueError, TypeError, InvalidOperation):
            raise ValueError("Paid accounting state cannot be reconstructed") from None
        failed = self._storage_failed or self.barrier.exists()
        return {
            "limit_usd": str(LIMIT),
            "spent_usd": str(spent),
            "reserved_usd": str(reserved),
            "remaining_usd": str(max(Decimal(0), LIMIT - spent - reserved)),
            "blocked": bool(row["blocked"]) or failed,
            "reason": row["reason"]
            or ("paid_budget_audit_unavailable" if failed else None),
        }

    def _run(self, operation):
        try:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._schema(db)
                if self._storage_failed or self.barrier.exists():
                    self._block(db, "paid_budget_audit_unavailable")
                self._seed(db)
                result, error = operation(db)
            if error:
                raise error
            return result
        except BudgetUnavailable:
            raise
        except Exception:
            self._failure()
            raise BudgetUnavailable("Paid budget storage unavailable") from None

    def snapshot(self):
        return self._run(lambda db: (self._snapshot(db), None))

    def block(self, reason):
        def operation(db):
            self._block(db, reason)
            return self._snapshot(db), None

        return self._run(operation)

    def reserve(self, request_id, estimate):
        def operation(db):
            state = self._snapshot(db)
            if state["blocked"]:
                return state, BudgetUnavailable("Paid budget is blocked")
            try:
                if not isinstance(request_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,128}", request_id
                ):
                    raise ValueError()
                amount = _amount(estimate)
                if amount <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                self._block(db, "reservation_invalid")
                return self._snapshot(db), BudgetUnavailable("Paid reservation invalid")
            prior = db.execute(
                "SELECT * FROM paid_ui_reservations WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if _amount(prior["estimate_usd"]) != amount:
                    self._block(db, "reservation_conflict")
                    return self._snapshot(db), BudgetUnavailable(
                        "Paid reservation conflict"
                    )
                if prior["state"] == "settled":
                    return state, BudgetUnavailable("Paid request already settled")
                return state, None
            if db.execute(
                "SELECT 1 FROM paid_ui_seed_calls WHERE call_id=?", (request_id,)
            ).fetchone():
                self._block(db, "paid_request_identity_reused")
                return self._snapshot(db), BudgetUnavailable(
                    "Paid request identity reused"
                )
            if amount > _amount(state["remaining_usd"]):
                return state, BudgetExceeded("Paid budget insufficient")
            db.execute(
                "INSERT INTO paid_ui_reservations VALUES(?,?,NULL,'reserved',?,NULL)",
                (request_id, str(amount), _now()),
            )
            return self._snapshot(db), None

        return self._run(operation)

    def settle(self, request_id, actualcost):
        def operation(db):
            try:
                actual = _amount(actualcost)
            except (ValueError, TypeError):
                self._block(db, "paid_actual_cost_unverified")
                return self._snapshot(db), BudgetUnavailable(
                    "Paid actual cost unverified"
                )
            prior = db.execute(
                "SELECT * FROM paid_ui_reservations WHERE request_id=?", (request_id,)
            ).fetchone()
            if not prior:
                # A known charge without an admission reservation is still a real
                # liability. Record once, then forbid further requests.
                if isinstance(request_id, str) and re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,128}", request_id
                ):
                    self._charge(db, request_id, actual)
                self._block(db, "paid_reservation_missing")
                return self._snapshot(db), BudgetUnavailable(
                    "Paid reservation unavailable"
                )
            if prior["state"] == "settled":
                if _amount(prior["actual_usd"]) == actual:
                    return self._snapshot(db), None
                # Never erase the first actual charge; a conflicting report is unknown.
                self._block(db, "paid_settlement_conflict")
                return self._snapshot(db), BudgetUnavailable("Paid settlement conflict")
            self._charge(db, request_id, actual)
            db.execute(
                "UPDATE paid_ui_reservations SET actual_usd=?,state='settled',settled_at=? WHERE request_id=?",
                (str(actual), _now(), request_id),
            )
            if actual > _amount(prior["estimate_usd"]) or self._spent(db) > LIMIT:
                self._block(db, "paid_actual_exceeds_reservation")
            state = self._snapshot(db)
            return state, (
                BudgetUnavailable("Paid budget is blocked")
                if state["blocked"]
                else None
            )

        return self._run(operation)
