"""One durable USD 3.50 paid-development allowance across tickets and restarts.

Only paid provider metadata is read from historic events. Amounts are decimal
strings in SQLite; no customer input, credentials or account balances are stored.
"""

from __future__ import annotations
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
import json, os, re

LIMIT = Decimal("3.50")
LEGACY_LIMIT = Decimal("0.25")
AUTHORIZATION_ID = "paid-development-validation-cap-3.50-v1"
AUTHORIZATION_SCOPE = "development_and_supplied_validation"
TIMEOUT_POLICY_VERSION = "hold-bounded-network-timeouts-v1"


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
            "CREATE TABLE IF NOT EXISTS paid_ui_request_bounds(request_id TEXT PRIMARY KEY REFERENCES paid_ui_reservations(request_id),proof TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_timeout_policy(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,version TEXT NOT NULL,revision INTEGER NOT NULL,reason TEXT NOT NULL,acknowledged_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_timeout_holds(request_id TEXT PRIMARY KEY REFERENCES paid_ui_reservations(request_id),reserved_usd TEXT NOT NULL,evidence TEXT NOT NULL,held_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_timeout_policy_events(event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
        inserted = db.execute(
            "INSERT OR IGNORE INTO paid_ui_budget VALUES(1,?,?,0,NULL,?,?)",
            (str(LIMIT), "0", _now(), _now()),
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS paid_ui_budget_authorizations(version TEXT PRIMARY KEY,previous_limit_usd TEXT NOT NULL,new_limit_usd TEXT NOT NULL,reason TEXT NOT NULL,applied_at TEXT NOT NULL)"
        )
        try:
            row = db.execute("SELECT * FROM paid_ui_budget WHERE id=1").fetchone()
            if _amount(row["limit_usd"]) == LEGACY_LIMIT:
                # Explicit expanded development authorization. Preserve every
                # liability and block; this is never a reset or a fresh balance.
                db.execute(
                    "INSERT OR IGNORE INTO paid_ui_budget_authorizations VALUES(?,?,?,?,?)",
                    (
                        AUTHORIZATION_ID,
                        str(LEGACY_LIMIT),
                        str(LIMIT),
                        "explicit_paid_development_and_validation_with_account_margin",
                        _now(),
                    ),
                )
                db.execute(
                    "UPDATE paid_ui_budget SET limit_usd=?,updated_at=? WHERE id=1",
                    (str(LIMIT), _now()),
                )
            elif _amount(row["limit_usd"]) != LIMIT:
                raise ValueError()
            _amount(row["spent_usd"])
            db.execute(
                "INSERT OR IGNORE INTO paid_ui_budget_authorizations VALUES(?,?,?,?,?)",
                (
                    AUTHORIZATION_ID,
                    "0" if inserted.rowcount else str(LIMIT),
                    str(LIMIT),
                    "explicit_paid_development_and_validation_with_account_margin",
                    _now(),
                ),
            )
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

        Legacy events require zero cumulative POST attempts. New events may
        prove zero attempts for this call while retaining a valid cumulative
        counter from earlier calls. Missing/coerced counters prove nothing.
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
        if type(attempts) is not int or attempts < 0:
            return False
        if "call_request_attempts" in metadata:
            current = metadata["call_request_attempts"]
            if type(current) is not int or current != 0 or current > attempts:
                return False
        elif attempts != 0:
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

    @staticmethod
    def _local_assessment_unbilled(metadata, decision, reasons):
        """Legacy local sensitivity exits copied idle provider metadata.

        Only the two deterministic local assessor branches are evidence. Generic
        idle/unknown metadata is never treated as free and no liability is reset.
        """
        if (
            type(metadata.get("request_attempts")) is not int
            or metadata["request_attempts"] != 0
        ):
            return False
        if decision != "escalate" or not isinstance(reasons, list) or len(reasons) != 1:
            return False
        if reasons[0] not in ("sensitive_authority_request", "instruction_injection"):
            return False
        return all(
            metadata.get(key) is None
            for key in (
                "last_call_id",
                "last_call_status",
                "last_error",
                "last_http_status",
                "response_id",
                "actual_model",
                "actual_provider",
                "usage",
            )
        )

    def _valid_bound(self, proof, estimate):
        if (
            not isinstance(proof, dict)
            or proof.get("version") != "bounded-paid-request-v1"
        ):
            return False
        from .providers.paid_test import PaidTestProvider

        models = PaidTestProvider.allowed_models
        if proof.get("model") not in models:
            return False
        for key in (
            "request_bytes",
            "max_tokens",
            "framing_token_allowance",
            "max_retries",
        ):
            if type(proof.get(key)) is not int:
                return False
        if (
            not 0 < proof["request_bytes"] <= 262144
            or not 0 < proof["max_tokens"] <= 32768
            or proof["framing_token_allowance"] != 4096
            or proof["max_retries"] != 0
        ):
            return False
        if (
            proof.get("optional_capabilities_disabled") is not True
            or proof.get("pricing_verified") is not True
        ):
            return False
        if not isinstance(proof.get("catalog_hash"), str) or not re.fullmatch(
            "[a-f0-9]{64}", proof["catalog_hash"]
        ):
            return False
        try:
            prompt = _amount(proof["prompt_price"])
            output = _amount(proof["completion_price"])
            if prompt > models[proof["model"]][0] or output > models[proof["model"]][1]:
                return False
            return (proof["request_bytes"] + 4096) * prompt + proof[
                "max_tokens"
            ] * output == _amount(estimate)
        except (KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def _timeout_metadata(metadata, request_id, estimate):
        if (
            not isinstance(metadata, dict)
            or metadata.get("cost_policy") != "paid_test_only"
            or metadata.get("last_call_id") != request_id
        ):
            return False
        if (
            metadata.get("last_call_status") != "failed"
            or metadata.get("last_error") != "network_timeout"
        ):
            return False
        if (
            type(metadata.get("request_attempts")) is not int
            or metadata["request_attempts"] < 1
            or type(metadata.get("last_retry_count")) is not int
            or metadata["last_retry_count"] != 0
        ):
            return False
        if "call_request_attempts" in metadata:
            current = metadata["call_request_attempts"]
            if (
                type(current) is not int
                or current < 1
                or current > metadata["request_attempts"]
            ):
                return False
        if any(
            metadata.get(key) is not None
            for key in (
                "last_http_status",
                "response_id",
                "actual_model",
                "actual_provider",
                "usage",
            )
        ):
            return False
        try:
            return _amount(metadata.get("last_reserved_cost_usd")) == _amount(estimate)
        except ValueError:
            return False

    def _policy(self, db):
        row = db.execute(
            "SELECT * FROM paid_ui_timeout_policy WHERE id=1 AND enabled=1"
        ).fetchone()
        return (
            {
                "version": row["version"],
                "scope": AUTHORIZATION_SCOPE,
                "revision": row["revision"],
                "acknowledged_at": row["acknowledged_at"],
            }
            if row
            else None
        )

    def _held_timeout(self, db, metadata):
        if not self._policy(db):
            return False
        request_id = metadata.get("last_call_id")
        row = db.execute(
            "SELECT h.*,r.estimate_usd,b.proof FROM paid_ui_timeout_holds h JOIN paid_ui_reservations r USING(request_id) JOIN paid_ui_request_bounds b USING(request_id) WHERE h.request_id=?",
            (request_id,),
        ).fetchone()
        return bool(
            row
            and _amount(row["reserved_usd"]) == _amount(row["estimate_usd"])
            and self._valid_bound(json.loads(row["proof"]), row["estimate_usd"])
            and self._timeout_metadata(metadata, request_id, row["estimate_usd"])
        )

    def _timeout_events(self, db, request_id):
        result = []
        for stage, key in (
            ("sensitivity", "provider"),
            ("generation", "model"),
            ("generation", "provider"),
            ("validation", "provider"),
        ):
            query = (
                "SELECT json_extract(e.data,'$."
                + key
                + "'),json_extract(o.data,'$.versions.code_hash') FROM decision_events e LEFT JOIN outcomes o USING(record_id) WHERE e.stage=? AND json_extract(e.data,'$."
                + key
                + ".last_call_id')=?"
            )
            for row in db.execute(query, (stage, request_id)):
                result.append((json.loads(row[0], parse_float=Decimal), row[1]))
        return result

    def _legacy_bound(
        self, metadata, estimate, recorded_hash, acknowledged_hash, legacy_freeze=None
    ):
        if (
            not acknowledged_hash
            or recorded_hash != acknowledged_hash
            or not re.fullmatch("[a-f0-9]{64}", str(acknowledged_hash))
        ):
            return None
        try:
            if not isinstance(legacy_freeze, dict):
                return None
            from .ingest import digest

            core = {k: v for k, v in legacy_freeze.items() if k != "protocol_hash"}
            if legacy_freeze.get("code_hash") != acknowledged_hash or legacy_freeze.get(
                "protocol_hash"
            ) != digest(core):
                return None
            fingerprint = legacy_freeze["configured_artifacts"]
            details = fingerprint["details"]
            if fingerprint.get("sha256") != digest(details):
                return None
            params = details["parameters"]
            if legacy_freeze.get("configured_model") != metadata.get(
                "configured_model"
            ) or params.get("configured_model") != metadata.get("configured_model"):
                return None
            if (
                params.get("generation_cost_policy") != "paid_test_only"
                or type(params.get("max_retries")) is not int
                or params["max_retries"] != 0
            ):
                return None
            generation = params["generation"]
            caps = {
                task: generation[field]
                for task, field in (
                    ("assess", "assessment_max_tokens"),
                    ("draft", "draft_max_tokens"),
                    ("validate", "validation_max_tokens"),
                )
            }
            if any(
                type(value) is not int or not 0 < value <= 32768
                for value in caps.values()
            ):
                return None
            if metadata.get("catalog_verified") is not True:
                return None
            endpoints = metadata["verified_endpoints"]
            if not endpoints or any(
                e.get("provider") not in ("OpenAI", "Azure") for e in endpoints
            ):
                return None
            prompt = max(_amount(e["pricing"]["prompt"]) for e in endpoints)
            output = max(_amount(e["pricing"]["completion"]) for e in endpoints)
            routing = metadata.get("routing_price_limits", {})
            if _amount(routing.get("prompt")) != prompt * Decimal(1000000) or _amount(
                routing.get("completion")
            ) != output * Decimal(1000000):
                return None
            cap = caps[metadata["last_task"]]
            if prompt <= 0:
                return None
            size = (_amount(estimate) - cap * output) / prompt - 4096
            if size != size.to_integral_value():
                return None
            proof = {
                "version": "bounded-paid-request-v1",
                "model": metadata["configured_model"],
                "request_bytes": int(size),
                "max_tokens": cap,
                "framing_token_allowance": 4096,
                "max_retries": 0,
                "prompt_price": str(prompt),
                "completion_price": str(output),
                "pricing_verified": True,
                "optional_capabilities_disabled": True,
                "catalog_hash": metadata["catalog_hash"],
                "legacy_freeze_protocol_hash": legacy_freeze["protocol_hash"],
                "legacy_freeze_code_hash": legacy_freeze["code_hash"],
            }
            return proof if self._valid_bound(proof, estimate) else None
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return None

    def try_hold_timeout(self, request_id, metadata):
        def operation(db):
            if not self._policy(db) or self._snapshot(db)["blocked"]:
                return False, None
            row = db.execute(
                "SELECT * FROM paid_ui_reservations WHERE request_id=? AND state='reserved'",
                (request_id,),
            ).fetchone()
            bound = db.execute(
                "SELECT proof FROM paid_ui_request_bounds WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                not row
                or not bound
                or not self._timeout_metadata(metadata, request_id, row["estimate_usd"])
                or not self._valid_bound(json.loads(bound[0]), row["estimate_usd"])
            ):
                return False, None
            db.execute(
                "INSERT OR IGNORE INTO paid_ui_timeout_holds VALUES(?,?,?,?)",
                (
                    request_id,
                    row["estimate_usd"],
                    json.dumps(
                        {
                            "proof_method": "pre_request_persisted_bound",
                            "actual_cost": "unknown",
                        }
                    ),
                    _now(),
                ),
            )
            return True, None

        return self._run(operation)

    def acknowledge_timeout_policy(
        self, request_ids, reason, legacy_code_hash=None, legacy_freeze=None
    ):
        """Called only by explicit stopped/idle operator CLI acknowledgement."""
        if (
            not request_ids
            or not isinstance(reason, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,:;()/_-]{2,499}", reason)
        ):
            raise BudgetUnavailable(
                "Explicit timeout liability acknowledgement required"
            )
        try:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._schema(db)
                if (
                    self._storage_failed
                    or self.barrier.exists()
                    or self.store.barrier.exists()
                    or getattr(self.store, "_fail_closed", False)
                ):
                    raise BudgetUnavailable(
                        "Audit barrier forbids timeout policy activation"
                    )
                control = db.execute(
                    "SELECT enabled FROM operator_controls WHERE id=1"
                ).fetchone()
                if (
                    control["enabled"]
                    or db.execute(
                        "SELECT 1 FROM runs WHERE state IN ('queued','running') LIMIT 1"
                    ).fetchone()
                ):
                    raise BudgetUnavailable("Stop automation and finish all runs first")
                budget = db.execute(
                    "SELECT * FROM paid_ui_budget WHERE id=1"
                ).fetchone()
                if (
                    budget["blocked"] and budget["reason"] != "paid_usage_unresolved"
                ) or budget["reason"] not in (None, "paid_usage_unresolved"):
                    raise BudgetUnavailable(
                        "Unrelated paid budget block cannot be cleared"
                    )
                pending = db.execute(
                    "SELECT * FROM paid_ui_reservations WHERE state='reserved'"
                ).fetchall()
                if (
                    self._spent(db)
                    + sum((_amount(r["estimate_usd"]) for r in pending), Decimal(0))
                    > LIMIT
                ):
                    raise BudgetUnavailable("Existing liabilities exceed the fixed cap")
                requested = set(request_ids)
                for row in pending:
                    held = db.execute(
                        "SELECT 1 FROM paid_ui_timeout_holds WHERE request_id=?",
                        (row["request_id"],),
                    ).fetchone()
                    if held:
                        continue
                    if row["request_id"] not in requested:
                        raise BudgetUnavailable(
                            "Every unknown pending liability needs explicit acknowledgement"
                        )
                    evidence = self._timeout_events(db, row["request_id"])
                    if not evidence or any(
                        not self._timeout_metadata(
                            m, row["request_id"], row["estimate_usd"]
                        )
                        for m, _ in evidence
                    ):
                        raise BudgetUnavailable(
                            "Strict network-timeout evidence unavailable"
                        )
                    bound = db.execute(
                        "SELECT proof FROM paid_ui_request_bounds WHERE request_id=?",
                        (row["request_id"],),
                    ).fetchone()
                    proof = (
                        json.loads(bound[0])
                        if bound
                        else self._legacy_bound(
                            evidence[0][0],
                            row["estimate_usd"],
                            evidence[0][1],
                            legacy_code_hash,
                            legacy_freeze,
                        )
                    )
                    if not proof or not self._valid_bound(proof, row["estimate_usd"]):
                        raise BudgetUnavailable(
                            "Verified timeout price and request bounds unavailable"
                        )
                    if not bound:
                        db.execute(
                            "INSERT INTO paid_ui_request_bounds VALUES(?,?)",
                            (row["request_id"], json.dumps(proof)),
                        )
                    db.execute(
                        "INSERT INTO paid_ui_timeout_holds VALUES(?,?,?,?)",
                        (
                            row["request_id"],
                            row["estimate_usd"],
                            json.dumps(
                                {
                                    "proof_method": (
                                        "pre_request_persisted_bound"
                                        if bound
                                        else "explicit_frozen_legacy_code_review"
                                    ),
                                    "code_hash": (
                                        legacy_code_hash if not bound else None
                                    ),
                                    "actual_cost": "unknown",
                                }
                            ),
                            _now(),
                        ),
                    )
                if not requested <= {r["request_id"] for r in pending}:
                    raise BudgetUnavailable("Acknowledged request must remain reserved")
                stamp = _now()
                if not self._policy(db):
                    db.execute(
                        "INSERT OR REPLACE INTO paid_ui_timeout_policy VALUES(1,1,?,1,?,?)",
                        (TIMEOUT_POLICY_VERSION, reason, stamp),
                    )
                db.execute("UPDATE paid_ui_budget SET blocked=0,reason=NULL WHERE id=1")
                self._seed(db)
                if self._snapshot(db)["blocked"]:
                    raise BudgetUnavailable(
                        "Strict historical-cost scan still found unresolved evidence"
                    )
                db.execute(
                    "INSERT INTO paid_ui_timeout_policy_events VALUES(?,?,?,?)",
                    (
                        __import__("uuid").uuid4().hex,
                        "explicit_operator_acknowledgement",
                        json.dumps(
                            {
                                "request_ids": sorted(requested),
                                "reason": reason,
                                "policy": self._policy(db),
                                "reserved_unknown_cost": "retained_in_full",
                                "actual_cost": "unknown",
                                "total_cap_usd": str(LIMIT),
                            }
                        ),
                        stamp,
                    ),
                )
                result = self._snapshot(db)
            return result
        except BudgetUnavailable:
            raise
        except Exception:
            self._failure()
            raise BudgetUnavailable("Timeout policy audit unavailable") from None

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
                + "') AS paid_metadata,json_extract(data,'$.assessment.decision'),json_extract(data,'$.assessment.reasons') FROM decision_events WHERE stage=? AND json_extract(data,'$."
                + key
                + ".cost_policy')='paid_test_only'"
            )
            for event in db.execute(query, (stage,)).fetchall():
                try:
                    metadata = json.loads(event[0], parse_float=Decimal)
                    if stage == "sensitivity":
                        reasons = json.loads(event[2]) if event[2] is not None else None
                        if self._local_assessment_unbilled(metadata, event[1], reasons):
                            continue
                    if self._held_timeout(db, metadata):
                        continue
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
        result = {
            "limit_usd": str(LIMIT),
            "spent_usd": str(spent),
            "reserved_usd": str(reserved),
            "remaining_usd": str(max(Decimal(0), LIMIT - spent - reserved)),
            "blocked": bool(row["blocked"]) or failed,
            "reason": row["reason"]
            or ("paid_budget_audit_unavailable" if failed else None),
        }
        if self._policy(db):
            held = db.execute(
                "SELECT h.reserved_usd FROM paid_ui_timeout_holds h JOIN paid_ui_reservations r USING(request_id) WHERE r.state='reserved'"
            ).fetchall()
            result.update(
                timeout_accounting_policy_enabled=True,
                held_unknown_count=len(held),
                held_unknown_usd=str(sum((_amount(r[0]) for r in held), Decimal(0))),
                spent_is_known_lower_bound=bool(held),
            )
        return result

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

    def authorization(self):
        """Stable authorization evidence; excludes fluctuating spend/balances.

        This explicit paid exception does not satisfy the original free-only
        assessment criterion. It authorizes this fixed total, never a refill.
        """

        def operation(db):
            row = db.execute(
                "SELECT * FROM paid_ui_budget_authorizations WHERE version=?",
                (AUTHORIZATION_ID,),
            ).fetchone()
            if row is None or _amount(row["new_limit_usd"]) != LIMIT:
                return None, BudgetUnavailable("Paid authorization unavailable")
            result = {
                "authorization_id": AUTHORIZATION_ID,
                "authorized_limit_usd": str(LIMIT),
                "authorization_scope": AUTHORIZATION_SCOPE,
                "authorization_created_at": row["applied_at"],
            }
            if self._policy(db):
                result["timeout_accounting_policy"] = self._policy(db)
            return result, None

        return self._run(operation)

    def snapshot(self):
        return self._run(lambda db: (self._snapshot(db), None))

    def block(self, reason):
        def operation(db):
            self._block(db, reason)
            return self._snapshot(db), None

        return self._run(operation)

    def reserve(self, request_id, estimate, bound_proof=None):
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
            if bound_proof is not None and not self._valid_bound(bound_proof, amount):
                self._block(db, "reservation_bound_invalid")
                return self._snapshot(db), BudgetUnavailable(
                    "Paid request bound invalid"
                )
            db.execute(
                "INSERT INTO paid_ui_reservations VALUES(?,?,NULL,'reserved',?,NULL)",
                (request_id, str(amount), _now()),
            )
            if bound_proof is not None:
                db.execute(
                    "INSERT INTO paid_ui_request_bounds VALUES(?,?)",
                    (request_id, json.dumps(bound_proof)),
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
