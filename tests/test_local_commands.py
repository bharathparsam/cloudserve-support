"""Local commands exercise persisted outcomes; providers are synthetic fixtures."""

import json
import sys
import pytest
from evaluation.harness import main, run
from scripts.configure import configure
from scripts.control import enable
from src.operator_settings import OperatorSettingsUnavailable, read_operator_settings
from test_integration import fixture_runtime, enable_fixture


def test_cli_accepts_arbitrary_file_and_saves_reconciled_reports(
    fixture_runtime, monkeypatch, tmp_path
):
    store, pipeline = enable_fixture(fixture_runtime)
    monkeypatch.setattr("src.runtime.create_runtime", lambda **_: fixture_runtime)
    source = tmp_path / "renamed-input.json"
    records = [
        {"channel": ch, "body": "How should I retry HTTP 429?"}
        for ch in ("email", "chat", "docs_comment", "forum")
    ] + [None]
    source.write_text(json.dumps(records))
    output = tmp_path / "saved-report"
    monkeypatch.setattr(
        sys, "argv", ["harness", "--input", str(source), "--output", str(output)]
    )
    main()
    manifest = json.loads((output / "manifest.json").read_text())
    outcomes = [
        json.loads(line)
        for line in (output / "outcomes.jsonl").read_text().splitlines()
    ]
    assert manifest["state"] == "completed" and manifest["kind"] == "batch"
    assert [o["input_index"] for o in outcomes] == list(range(5))
    assert sum(o["action"] == "auto_respond" for o in outcomes) == 4
    assert outcomes[-1]["action"] == "escalate"
    assert (output / "metrics.md").is_file() and (output / "metrics.json").is_file()
    assert (output / "decisions.jsonl").is_file()


def test_generic_batch_does_not_bypass_paid_gate(
    fixture_runtime, monkeypatch, tmp_path
):
    settings, store, pipeline = fixture_runtime
    settings.generation_cost_policy = "paid_test_only"
    calls = []

    def denied(*args):
        calls.append(True)
        raise RuntimeError("Explicit paid gate denied")

    monkeypatch.setattr("evaluation.harness.paid_development_gate", denied)
    with pytest.raises(RuntimeError, match="paid gate denied"):
        run(
            tmp_path / "input-not-opened.json",
            tmp_path / "report",
            runtime=fixture_runtime,
            kind="batch",
        )
    assert calls == [True] and not (tmp_path / "report").exists()


def test_configure_preserves_legacy_observer_setting_in_audit(fixture_runtime):
    settings, store, pipeline = fixture_runtime
    configure(settings, "fixture/generator:free")
    with store.connection() as db:
        before = json.loads(
            db.execute("SELECT data FROM operator_settings WHERE id=1").fetchone()[0]
        )
        before["decider_mode"] = "shadow"
        db.execute(
            "UPDATE operator_settings SET data=? WHERE id=1", (json.dumps(before),)
        )
    with pytest.raises(OperatorSettingsUnavailable):
        read_operator_settings(settings, store)
    result = configure(settings, "fixture/new-generator:free")
    assert (
        result["generation_mode"] == "free_only"
        and result["decider_mode"] == "disabled"
    )
    with store.connection() as db:
        event = json.loads(
            db.execute(
                "SELECT data FROM operator_settings_events ORDER BY revision DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert event["before"]["decider_mode"] == "shadow"
    assert event["after"]["decider_mode"] == "disabled"
    assert not store.control()["enabled"]


def test_failed_enable_keeps_automation_stopped(fixture_runtime):
    settings, store, pipeline = fixture_runtime
    pipeline.policy.update(calibration_ready=True, retrieval_ready=True)
    pipeline.provider.preflight = lambda: {"verified": False}
    with pytest.raises(RuntimeError, match="verified"):
        enable(fixture_runtime, "Fixture enable")
    assert not store.control()["enabled"]
