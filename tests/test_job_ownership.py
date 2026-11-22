"""An API worker must never silently take over an unattended CLI evaluation."""

import threading, time
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.logging_store import Store
from src.jobs import JobRunner, API_RUN_KINDS


@pytest.mark.parametrize(
    "kind", ["development", "supplied_validation", "paid_development_test"]
)
def test_api_claim_cannot_steal_cli_kind_even_after_abandoned_record(tmp_path, kind):
    store = Store(tmp_path / "audit.db")
    rid = store.create_run(
        [{"channel": "chat", "body": "one"}, {"channel": "chat", "body": "two"}],
        kind=kind,
    )
    first = store.claim_job(run_id=rid)
    assert first["input_index"] == 0 and first["run_kind"] == kind
    # Simulates native process loss: first record remains running and next queued.
    assert store.claim_job(allowed_kinds=API_RUN_KINDS) is None
    with store.connection() as db:
        assert [r[0] for r in db.execute("SELECT state FROM jobs ORDER BY rowid")] == [
            "running",
            "queued",
        ]
        assert db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
    # An explicit CLI claim is still possible; the API does not own it.
    assert store.claim_job(run_id=rid)["input_index"] == 1


@pytest.mark.parametrize("kind", sorted(API_RUN_KINDS))
def test_api_kinds_remain_claimable(tmp_path, kind):
    store = Store(tmp_path / "audit.db")
    rid = store.create_run([{"channel": "chat", "body": "request"}], kind=kind)
    job = store.claim_job(allowed_kinds=API_RUN_KINDS)
    assert job["run_id"] == rid and job["run_kind"] == kind


@pytest.mark.parametrize(
    "kind", ["development", "supplied_validation", "paid_development_test"]
)
def test_actual_worker_loop_does_not_process_cli_queue(tmp_path, kind):
    store = Store(tmp_path / "audit.db")
    rid = store.create_run([{"channel": "chat", "body": "do not take over"}], kind=kind)
    calls = []
    pipeline = SimpleNamespace(
        storage_path=tmp_path, process=lambda *args: calls.append(args)
    )
    runner = JobRunner(store, pipeline)
    # Avoid startup recovery intentionally: this tests the claim loop after a CLI crash.
    runner.recovered = True
    worker = threading.Thread(target=runner.run)
    worker.start()
    try:
        time.sleep(0.38)
        assert calls == []
        with store.connection() as db:
            assert (
                db.execute("SELECT state FROM jobs WHERE run_id=?", (rid,)).fetchone()[
                    0
                ]
                == "queued"
            )
    finally:
        runner.stopping.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize("kind", sorted(API_RUN_KINDS))
def test_actual_worker_loop_processes_api_jobs(tmp_path, kind):
    store = Store(tmp_path / "audit.db")
    rid = store.create_run([{"channel": "chat", "body": "API ticket"}], kind=kind)
    processed = threading.Event()
    calls = []

    def process(*args):
        calls.append(args)
        processed.set()

    pipeline = SimpleNamespace(storage_path=tmp_path, process=process)
    runner = JobRunner(store, pipeline)
    runner.recovered = True
    worker = threading.Thread(target=runner.run)
    worker.start()
    try:
        assert processed.wait(2)
        assert len(calls) == 1 and calls[0][1] == rid
    finally:
        runner.stopping.set()
        worker.join(2)
    assert not worker.is_alive()


def test_kind_filter_composes_with_explicit_run_and_empty_means_no_claim(tmp_path):
    store = Store(tmp_path / "audit.db")
    rid = store.create_run([{}], kind="development")
    assert store.claim_job(run_id=rid, allowed_kinds=API_RUN_KINDS) is None
    assert store.claim_job(allowed_kinds=[]) is None
    with pytest.raises(ValueError):
        store.claim_job(allowed_kinds="development")
    assert store.claim_job(run_id=rid)["run_id"] == rid


def test_terminal_run_is_not_resumed_by_unrestricted_or_ui_claim(tmp_path):
    store = Store(tmp_path / "audit.db")
    rid = store.create_run([{}], kind="manual")
    with store.connection() as db:
        db.execute("UPDATE runs SET state='interrupted' WHERE run_id=?", (rid,))
    assert (
        store.claim_job() is None
        and store.claim_job(allowed_kinds=API_RUN_KINDS) is None
    )
