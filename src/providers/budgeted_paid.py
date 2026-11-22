"""Explicit paid development mode backed by a durable, shared budget ledger."""

from decimal import Decimal, InvalidOperation
import threading

from .openrouter import ProviderUnavailable, FreePolicyError
from .paid_test import PaidTestProvider


def _new_ledger(store):
    from src.paid_budget import BudgetLedger

    return BudgetLedger(store)


class _DurablePaidProvider(PaidTestProvider):
    def __init__(self, ledger, *args, **kwargs):
        self.ledger = ledger
        self._reservation_id = None
        super().__init__(*args, **kwargs)

    def _before_request(self, request):
        # Parent validates capabilities, size, per-record call cap, and estimate.
        super()._before_request(request)
        call_id = self.metadata["last_call_id"]
        try:
            if callable(getattr(self.ledger, "try_hold_timeout", None)):
                self.ledger.reserve(
                    call_id,
                    self._current_reservation,
                    bound_proof=self.metadata.get("reservation_bound"),
                )
            else:
                self.ledger.reserve(call_id, self._current_reservation)
        except RuntimeError:
            self._failure("paid_budget_reservation_rejected")
            raise ProviderUnavailable("Durable paid test budget unavailable") from None
        self._reservation_id = call_id

    def _paid_cost_failure(self, code):
        self.ledger.block(code)
        super()._paid_cost_failure(code)

    def _capture_usage(self, data):
        try:
            super()._capture_usage(data)
        finally:
            # Base identity validation has already passed. Settle even a
            # schema/count/reservation error if the actual cost is known.
            safe = self.metadata.get("usage") or {}
            try:
                value = safe.get("cost")
                actual = Decimal(str(value))
                valid = value is not None and actual.is_finite() and actual >= 0
            except (InvalidOperation, ValueError, TypeError):
                valid = False
            if valid and self._reservation_id is not None:
                try:
                    self.ledger.settle(self._reservation_id, actual)
                except RuntimeError:
                    self._failure("paid_budget_settlement_failed")
                    raise ProviderUnavailable(
                        "Durable paid test settlement unavailable"
                    ) from None
                self._reservation_id = None


class BudgetedPaidProvider:
    """Legacy paid adapter; every request reserves the same persistent total cap.

    ``begin_record`` starts only the three-call record allowance. It never
    releases reservations or changes the global budget.
    """

    reasoning_policy = PaidTestProvider.reasoning_policy

    def __init__(
        self,
        api_key,
        model,
        store,
        timeout=30,
        max_retries=0,
        budget_usd=Decimal("3.50"),
    ):
        self.api_key, self.model = api_key, model
        self.timeout, self.max_retries = timeout, max_retries
        self._record_budget_usd = budget_usd
        self.ledger = _new_ledger(store)
        self._lock = threading.RLock()
        self._record_id = None
        self._inner = self._make_inner()
        self.metadata = dict(self._inner.metadata)
        self._mirror()

    def _make_inner(self):
        return _DurablePaidProvider(
            self.ledger,
            api_key=self.api_key,
            budget_usd=self._record_budget_usd,
            model=self.model,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def _mirror(self):
        self.metadata = dict(self._inner.metadata)
        self.metadata["cost_policy"] = "paid_test_only"
        try:
            snapshot = self.ledger.snapshot()
        except RuntimeError:
            self._inner._failure("paid_budget_unavailable")
            self._inner._verified = False
            self._inner.metadata["verified"] = False
            self.metadata = dict(self._inner.metadata)
            raise ProviderUnavailable("Durable paid test budget unavailable") from None
        self.metadata["paid_budget"] = snapshot
        self.metadata.update(
            budget_usd=snapshot["limit_usd"],
            cumulative_cost_usd=snapshot["spent_usd"],
            reserved_unreported_cost_usd=snapshot["reserved_usd"],
            budget_remaining_usd=snapshot["remaining_usd"],
        )
        return snapshot

    def _check_budget(self):
        snapshot = self._mirror()
        if snapshot["blocked"]:
            self._inner._failure("paid_budget_blocked")
            self._inner._verified = False
            self._inner.metadata["verified"] = False
            self._mirror()
            raise ProviderUnavailable("Durable paid test budget blocked")
        if Decimal(snapshot["remaining_usd"]) <= 0:
            self._inner._failure("paid_budget_exhausted")
            self._inner._verified = False
            self._inner.metadata["verified"] = False
            self._mirror()
            raise ProviderUnavailable("Durable paid test budget exhausted")

    def verify_credit(self, required_usd):
        with self._lock:
            result = self._inner.verify_credit(required_usd)
            self._mirror()
            return result

    def begin_record(self, record_id):
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("Record identifier required")
        with self._lock:
            if record_id == self._record_id:
                return
            self._inner.client.close()
            self._inner = self._make_inner()
            self._record_id = record_id
            self._mirror()

    def preflight(self):
        with self._lock:
            try:
                self._check_budget()
                self._inner.preflight()
            except Exception:
                self._mirror()
                raise
            self._mirror()
            return dict(self.metadata)

    def complete(self, task, payload, schema, max_tokens):
        with self._lock:
            if self._record_id is None:
                self._inner._reset_call(task)
                self._inner._failure("paid_test_record_required")
                self._mirror()
                raise ProviderUnavailable("Paid test record admission required")
            self._inner._reset_call(task)
            try:
                self._check_budget()
                # Catalog/endpoint prices are reverified before every stage.
                self._inner.preflight()
                return self._inner.complete(task, payload, schema, max_tokens)
            except Exception:
                if self._inner.metadata.get("last_error") is None:
                    self._inner._failure(
                        "paid_test_policy_rejected"
                        if self._inner.metadata.get("status")
                        == "paid_test_policy_rejected"
                        else "preflight_unavailable"
                    )
                if self._inner._reservation_id is not None:
                    # Transport, unverified identity, malformed HTTP, or unknown
                    # usage may have charged: retain reservation and block.
                    try:
                        hold = getattr(self.ledger, "try_hold_timeout", None)
                        held = bool(
                            hold
                            and hold(
                                self._inner._reservation_id, dict(self._inner.metadata)
                            )
                        )
                        if held:
                            self._inner.metadata["paid_timeout_cost_status"] = (
                                "unknown_full_reservation_held"
                            )
                        else:
                            self.ledger.block("paid_usage_unresolved")
                    except RuntimeError:
                        self._inner._failure("paid_budget_unavailable")
                    self._inner._verified = False
                    self._inner.metadata["verified"] = False
                raise
            finally:
                self._mirror()
