"""HTTP fixtures only; paid lifecycle tests never call a remote service."""

from decimal import Decimal
import json

import httpx
import pytest

from src.contracts import Assessment
from src.providers.openrouter import ProviderUnavailable
import src.providers.budgeted_paid as module
from src.providers.budgeted_paid import BudgetedPaidProvider

MODEL = "openai/gpt-4o-mini"


class LedgerFixture:
    def __init__(self):
        self.spent = Decimal(".0023492")
        self.reservations = {}
        self.settled = {}
        self.reason = None

    def snapshot(self):
        reserved = sum(self.reservations.values(), Decimal(0))
        return {
            "limit_usd": "0.25",
            "spent_usd": str(self.spent),
            "reserved_usd": str(reserved),
            "remaining_usd": str(
                max(Decimal(0), Decimal(".25") - self.spent - reserved)
            ),
            "blocked": self.reason is not None,
            "reason": self.reason,
        }

    def reserve(self, key, value):
        if self.reason or self.spent + sum(
            self.reservations.values(), Decimal(0)
        ) + value > Decimal(".25"):
            raise RuntimeError("fixture budget unavailable")
        self.reservations[key] = value
        return self.snapshot()

    def settle(self, key, value):
        estimate = self.reservations.pop(key)
        self.spent += value
        self.settled[key] = value
        if value > estimate:
            self.reason = "reservation_exceeded"
        return self.snapshot()

    def block(self, reason):
        self.reason = reason
        return self.snapshot()


def fixture(monkeypatch, post=None, ledger=None):
    ledger = ledger or LedgerFixture()
    posts = []
    gets = []
    monkeypatch.setattr(module, "_new_ledger", lambda _: ledger)
    prices = {"prompt": "0.00000015", "completion": "0.0000006"}

    def http(request):
        if request.method == "GET":
            gets.append(request)
            if request.url.path.endswith("/endpoints"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "endpoints": [
                                {"provider_name": "OpenAI", "pricing": prices}
                            ]
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": MODEL,
                            "pricing": prices,
                            "architecture": {
                                "input_modalities": ["text"],
                                "output_modalities": ["text"],
                            },
                        }
                    ]
                },
            )
        posts.append(request)
        if post:
            return post(request)
        return httpx.Response(200, json=response())

    original = BudgetedPaidProvider._make_inner

    def make(self):
        p = original(self)
        p.client.close()
        p.client = httpx.Client(
            base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(http)
        )
        p._pace = lambda: None
        return p

    monkeypatch.setattr(BudgetedPaidProvider, "_make_inner", make)
    p = BudgetedPaidProvider("fixture-private-key", MODEL, object())
    p.begin_record("record-one")
    return p, ledger, posts, gets


def response(**changes):
    return {
        "model": MODEL,
        "provider": "OpenAI",
        "usage": {"cost": ".00001", "prompt_tokens": 10, "completion_tokens": 10},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"decision":"clear","reasons":[]}'},
            }
        ],
    } | changes


def call(p):
    return p.complete("assess", {"text": "private fixture ticket"}, Assessment, 200)


def test_prior_cost_seed_and_global_snapshot_after_success(monkeypatch):
    p, ledger, posts, gets = fixture(monkeypatch)
    call(p)
    assert ledger.spent == Decimal(".0023592") and not ledger.reservations
    assert p.metadata["paid_budget"]["spent_usd"] == str(ledger.spent)
    assert p.metadata["cumulative_cost_usd"] == str(ledger.spent)
    assert p.metadata["usage"]["cost"] == "0.00001"
    assert len(posts) == 1 and len(gets) == 2
    assert "fixture-private-key" not in json.dumps(p.metadata)
    assert "private fixture ticket" not in json.dumps(p.metadata)


def test_three_call_record_limit_new_record_does_not_replenish_budget(monkeypatch):
    p, ledger, posts, gets = fixture(monkeypatch)
    for _ in range(3):
        call(p)
    p.begin_record("record-one")
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert len(posts) == 3
    p.begin_record("record-two")
    call(p)
    assert len(posts) == 4 and ledger.spent == Decimal(".0023892")
    assert len(gets) == 10  # preflight before every stage, including blocked call


def test_insufficient_durable_budget_denies_before_post(monkeypatch):
    p, ledger, posts, _ = fixture(monkeypatch)
    ledger.spent = Decimal(".2499999")
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert not posts and not ledger.reservations
    assert p.metadata["last_error"] == "paid_budget_reservation_rejected"


@pytest.mark.parametrize(
    "data",
    [
        response(provider="untrusted"),
        response(usage={}),
        response(usage={"cost": "NaN"}),
        {"private": "bad payload"},
    ],
)
def test_unknown_identity_or_cost_blocks_future_records_and_restarts(monkeypatch, data):
    p, ledger, posts, _ = fixture(
        monkeypatch, post=lambda _: httpx.Response(200, json=data)
    )
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert ledger.reason and ledger.reservations and len(posts) == 1
    p.begin_record("another-record")
    with pytest.raises(ProviderUnavailable):
        call(p)
    restarted = BudgetedPaidProvider("fixture-private-key", MODEL, object())
    restarted.begin_record("new-process-record")
    with pytest.raises(ProviderUnavailable):
        call(restarted)
    assert len(posts) == 1


def test_timeout_conservatively_blocks_with_retained_reservation(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("private error", request=request)

    p, ledger, posts, _ = fixture(monkeypatch, post=timeout)
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert ledger.reason == "paid_usage_unresolved" and ledger.reservations
    assert p.metadata["last_error"] == "network_timeout"
    assert len(posts) == 1 and "private error" not in json.dumps(p.metadata)


def test_schema_failure_still_settles_known_actual_cost(monkeypatch):
    p, ledger, posts, _ = fixture(
        monkeypatch, post=lambda _: httpx.Response(200, json=response(choices=[]))
    )
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert (
        ledger.spent == Decimal(".0023592")
        and not ledger.reservations
        and not ledger.reason
    )
    assert p.metadata["last_error"] == "response_schema_invalid"


def test_known_excess_charge_recorded_and_blocks(monkeypatch):
    p, ledger, posts, _ = fixture(
        monkeypatch,
        post=lambda _: httpx.Response(
            200,
            json=response(
                usage={"cost": ".2", "prompt_tokens": 1, "completion_tokens": 1}
            ),
        ),
    )
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert (
        ledger.spent == Decimal(".2023492")
        and ledger.reason
        and not ledger.reservations
    )


def test_record_admission_required_and_retries_forbidden(monkeypatch):
    fixture(monkeypatch)
    p = BudgetedPaidProvider("fixture-private-key", MODEL, object())
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "paid_test_record_required"
    with pytest.raises(ValueError):
        BudgetedPaidProvider("fixture", MODEL, object(), max_retries=1)


def test_settlement_store_failure_returns_no_answer(monkeypatch):
    p, ledger, posts, _ = fixture(monkeypatch)

    def fail(*_):
        raise RuntimeError("private database error")

    ledger.settle = fail
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert ledger.reason and ledger.reservations
    assert p.metadata["last_error"] == "paid_budget_settlement_failed"
    assert "private database error" not in json.dumps(p.metadata)


def test_real_sqlite_ledger_survives_new_provider_and_store(monkeypatch, tmp_path):
    from src.logging_store import Store
    from src.paid_budget import BudgetLedger

    fixture(monkeypatch)
    monkeypatch.setattr(module, "_new_ledger", BudgetLedger)
    path = tmp_path / "budget.sqlite"
    p = BudgetedPaidProvider("fixture", MODEL, Store(path))
    p.begin_record("one")
    call(p)
    restarted = BudgetedPaidProvider("fixture", MODEL, Store(path))
    restarted.begin_record("two")
    call(restarted)
    assert Decimal(restarted.metadata["paid_budget"]["spent_usd"]) == Decimal(".00002")
    assert Decimal(restarted.metadata["paid_budget"]["reserved_usd"]) == 0
    assert not restarted.metadata["paid_budget"]["blocked"]


def test_real_sqlite_unresolved_charge_blocks_new_store(monkeypatch, tmp_path):
    from src.logging_store import Store
    from src.paid_budget import BudgetLedger

    fixture(
        monkeypatch,
        post=lambda _: httpx.Response(200, json=response(provider="unknown")),
    )
    monkeypatch.setattr(module, "_new_ledger", BudgetLedger)
    path = tmp_path / "budget.sqlite"
    p = BudgetedPaidProvider("fixture", MODEL, Store(path))
    p.begin_record("one")
    with pytest.raises(ProviderUnavailable):
        call(p)
    restarted = BudgetedPaidProvider("fixture", MODEL, Store(path))
    restarted.begin_record("two")
    assert restarted.metadata["paid_budget"]["blocked"]
    assert Decimal(restarted.metadata["paid_budget"]["reserved_usd"]) > 0
    with pytest.raises(ProviderUnavailable):
        call(restarted)
    assert restarted.metadata["last_error"] == "paid_budget_blocked"


@pytest.mark.parametrize(
    "state,expected",
    [("blocked", "paid_budget_blocked"), ("exhausted", "paid_budget_exhausted")],
)
def test_real_ledger_budget_failures_survive_all_mirrors_without_http(
    monkeypatch, tmp_path, state, expected
):
    from src.logging_store import Store
    from src.paid_budget import BudgetLedger

    _, _, posts, gets = fixture(monkeypatch)
    monkeypatch.setattr(module, "_new_ledger", BudgetLedger)
    store = Store(tmp_path / "budget.sqlite")
    ledger = BudgetLedger(store)
    if state == "blocked":
        ledger.block("test_budget_blocked")
    else:
        ledger.reserve("exhaustion-test", Decimal(".25"))
        ledger.settle("exhaustion-test", Decimal(".25"))
    p = BudgetedPaidProvider("fixture", MODEL, store)
    p.begin_record("record")
    for action in (p.preflight, lambda: call(p)):
        with pytest.raises(ProviderUnavailable):
            action()
        assert p.metadata["last_error"] == expected
        assert p.metadata["status"] == expected
        assert p.metadata["last_call_status"] == "failed"
        assert p._inner.metadata["last_error"] == expected
        assert not p.metadata["verified"]
        p._mirror()
        assert p.metadata["last_error"] == expected
    assert not posts and not gets


def test_missing_record_failure_survives_mirror_and_has_failed_status(monkeypatch):
    fixture(monkeypatch)
    p = BudgetedPaidProvider("fixture", MODEL, object())
    with pytest.raises(ProviderUnavailable):
        call(p)
    p._mirror()
    assert p.metadata["last_error"] == "paid_test_record_required"
    assert p.metadata["last_call_status"] == "failed"


def test_snapshot_failure_survives_subsequent_metadata_copy(monkeypatch):
    p, ledger, posts, gets = fixture(monkeypatch)

    def fail():
        raise RuntimeError("private database details")

    ledger.snapshot = fail
    with pytest.raises(ProviderUnavailable):
        call(p)
    assert p.metadata["last_error"] == "paid_budget_unavailable"
    assert p.metadata["last_call_status"] == "failed"
    assert p._inner.metadata["last_error"] == "paid_budget_unavailable"
    assert not posts and not gets
    assert "private database details" not in json.dumps(p.metadata)
