"""Isolated settings transactions and metadata-only discovery; no live inference."""

from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import threading
import time

import httpx
import pytest
from pydantic import ValidationError

from src.logging_store import Store
import src.operator_settings as module
from src.operator_settings import (
    OperatorSettings,
    OperatorSettingsConflict,
    OperatorSettingsUnavailable,
    build_providers,
    persist_operator_settings,
    read_operator_settings,
)


def values(**changes):
    data = {
        "generation_model": "google/gemma-4-26b-a4b-it:free",
        "decider_model": "perplexity/pplx-decider-flash-v1.3:free",
        "decider_mode": "disabled",
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "generation_mode": "free_only",
    }
    return data | changes


def settings(tmp_path):
    return SimpleNamespace(
        model=values()["generation_model"],
        decider_model=values()["decider_model"],
        decider_mode="disabled",
        api_key="synthetic-private-key",
        decider_policy_path=tmp_path / "decider.json",
    )


def test_defaults_are_environment_values_without_credentials_or_persistence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    s = settings(tmp_path)
    store = Store(tmp_path / "db")
    result = read_operator_settings(s, store)
    assert result == values(timeout_seconds=45.0, max_retries=1) | {
        "revision": 0,
        "updated_at": None,
        "source": "environment",
    }
    assert s.api_key not in json.dumps(result)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM operator_settings").fetchone()[0] == 0


def test_persistent_revision_and_safe_atomic_audit_survive_restart(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    s = settings(tmp_path)
    first = persist_operator_settings(store, values())
    second = persist_operator_settings(
        store, values(timeout_seconds=60.0, max_retries=0)
    )
    restarted = Store(path)
    result = read_operator_settings(s, restarted)
    assert first["revision"] == 1 and second["revision"] == 2 and result == second
    assert result["source"] == "operator" and result["updated_at"]
    with restarted.connection() as db:
        events = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM operator_settings_events ORDER BY revision"
            )
        ]
        assert len(events) == 2
        assert json.loads(events[1]["data"])["before"]["timeout_seconds"] == 30
        assert json.loads(events[1]["data"])["after"]["timeout_seconds"] == 60
        assert s.api_key not in json.dumps(events)
    assert not restarted.control()["enabled"]


@pytest.mark.parametrize(
    "change",
    [
        {"generation_model": "google/gemma-paid"},
        {"generation_model": "https://example.com/model:free"},
        {"generation_model": "google/model:free?api_key=secret"},
        {"generation_model": "google/../model:free"},
        {"decider_model": "~perplexity/pplx-decider-flash-latest"},
        {"decider_model": "openrouter/free"},
        {"timeout_seconds": 4},
        {"timeout_seconds": 121},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
        {"timeout_seconds": "30"},
        {"max_retries": -1},
        {"max_retries": 3},
        {"max_retries": True},
        {"max_retries": 1.0},
        {"decider_mode": "automatic"},
        {"api_key": "must-not-be-persisted"},
        {"revision": 9},
    ],
)
def test_invalid_or_unknown_mutation_fields_rejected(change, tmp_path):
    store = Store(tmp_path / "db")
    with pytest.raises(ValidationError):
        persist_operator_settings(store, values(**change))


def test_enabled_automation_prevents_mutation_without_stopping_or_latching(tmp_path):
    store = Store(tmp_path / "db")
    s = settings(tmp_path)
    read_operator_settings(s, store)
    control = store.set_control(True, "Synthetic start")
    with pytest.raises(OperatorSettingsConflict):
        persist_operator_settings(store, values())
    assert store.control() == control
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM operator_settings").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM operator_settings_events").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("claimed", [False, True])
def test_queued_or_running_batch_prevents_mutation(tmp_path, claimed):
    store = Store(tmp_path / "db")
    run = store.create_run([{"channel": "chat", "body": "request"}])
    if claimed:
        store.claim_job()
    with pytest.raises(OperatorSettingsConflict):
        persist_operator_settings(store, values())
    assert store.get_run(run)["state"] == ("running" if claimed else "queued")


def test_orphan_active_job_is_not_ignored(tmp_path):
    store = Store(tmp_path / "db")
    run = store.create_run([{}])
    with store.connection() as db:
        db.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run,))
    with pytest.raises(OperatorSettingsConflict):
        persist_operator_settings(store, values())


def test_audit_failure_rolls_back_new_settings_and_latches_releases(tmp_path):
    store = Store(tmp_path / "db")
    s = settings(tmp_path)
    original = persist_operator_settings(store, values())
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_settings_audit BEFORE INSERT ON operator_settings_events BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )
    with pytest.raises(OperatorSettingsUnavailable):
        persist_operator_settings(store, values(timeout_seconds=60.0))
    assert read_operator_settings(s, store) == original
    assert store.barrier.exists()
    with store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM operator_settings_events").fetchone()[0]
            == 1
        )


def test_concurrent_enable_committed_before_settings_transaction_blocks_save(tmp_path):
    store = Store(tmp_path / "db")
    errors = []
    started = threading.Event()

    def save():
        started.set()
        try:
            persist_operator_settings(store, values())
        except Exception as error:
            errors.append(error)

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=save)
        thread.start()
        assert started.wait(2)
        db.execute("UPDATE operator_controls SET enabled=1 WHERE id=1")
    thread.join(3)
    assert (
        not thread.is_alive()
        and len(errors) == 1
        and isinstance(errors[0], OperatorSettingsConflict)
    )
    assert store.control()["enabled"]


def test_invalid_stored_payload_is_not_replaced_silently_by_defaults(tmp_path):
    store = Store(tmp_path / "db")
    persist_operator_settings(store, values())
    with store.connection() as db:
        db.execute("UPDATE operator_settings SET data='{}' WHERE id=1")
    with pytest.raises(OperatorSettingsUnavailable):
        read_operator_settings(settings(tmp_path), store)


def test_disabled_decider_is_not_constructed_or_contacted(tmp_path, monkeypatch):
    captured = []

    def generation(**kwargs):
        captured.append(kwargs)
        return "generation"

    monkeypatch.setattr(module, "OpenRouterProvider", generation)
    provider, evaluator = build_providers(
        settings(tmp_path), values(decider_mode="disabled")
    )
    assert provider == "generation" and evaluator is None
    assert (
        captured[0]["free_only"] is True
        and captured[0]["timeout"] == 30
        and captured[0]["max_retries"] == 2
    )


def test_provider_build_uses_persisted_snapshot_and_private_local_key(
    tmp_path, monkeypatch
):
    captured = []
    monkeypatch.setattr(
        module,
        "OpenRouterProvider",
        lambda **kw: captured.append(("generation", kw)) or object(),
    )
    s = settings(tmp_path)
    snapshot = values(timeout_seconds=90.0, max_retries=1) | {
        "revision": 3,
        "source": "operator",
        "updated_at": "timestamp",
    }
    provider, evaluator = build_providers(s, snapshot)
    assert [x[0] for x in captured] == ["generation"]
    assert all(
        x[1]["api_key"] == s.api_key
        and x[1]["timeout"] == 90
        and x[1]["max_retries"] == 1
        for x in captured
    )
    assert evaluator is None
