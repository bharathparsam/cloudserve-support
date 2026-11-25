"""Immutable reports from a consistent SQLite read snapshot; never run models."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from filelock import FileLock


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n", encoding="utf-8")


def _capture(store, run_id):
    # Read run state, inputs, outputs and events at the same database instant.
    with store.connection() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Run not found")
        run = dict(row)
        inputs = db.execute("SELECT original FROM input_records WHERE run_id=? ORDER BY input_index", (run_id,)).fetchall()
        outputs = db.execute("SELECT o.data FROM outcomes o JOIN input_records i USING(record_id) WHERE o.run_id=? ORDER BY i.input_index", (run_id,)).fetchall()
        events = db.execute("SELECT * FROM decision_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
    return {
        "run": run,
        "records": [json.loads(r[0]) for r in inputs],
        "outcomes": [json.loads(r[0]) for r in outputs],
        "events": [dict(r) | {"data": json.loads(r["data"])} for r in events],
    }


def save_run_report(settings, store, run_id):
    """Save/reuse a content-addressed ZIP and its files inside project storage."""
    run_id = str(uuid.UUID(run_id))
    root = Path(settings.storage) / "reports" / run_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(root / ".report.lock")).acquire(timeout=60):
        captured_at = datetime.now(timezone.utc).isoformat()
        data = _capture(store, run_id)
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        target = root / fingerprint[:20]
        if (target / "report.zip").is_file():
            return target / "report.zip"
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
        try:
            from .metrics import compute_metrics
            from evaluation.harness import enrich_metrics
            run, records, outcomes, events = (data[k] for k in ("run", "records", "outcomes", "events"))
            metrics = compute_metrics(outcomes, records, events=events)
            enrich_metrics(metrics, outcomes, records, output=pending, body_boundary=None)
            active = run["state"] in ("queued", "running")
            kind = "in_progress_snapshot" if active else "final" if run["state"] == "completed" and metrics["governance"]["reconciled"] else "incomplete"
            manifest = {
                "schema_version": 1, "run_id": run_id, "report_kind": kind,
                "run_state": run["state"], "captured_at": captured_at,
                "snapshot_sha256": fingerprint,
                "expected_records": run["expected_count"], "included_outcomes": len(outcomes),
                "pending_records": max(0, run["expected_count"] - len(outcomes)),
                "reconciled": metrics["governance"]["reconciled"],
                "export_mode": "Consistent persisted-data snapshot; no model calls or reprocessing",
                "metric_boundary": "Recomputed from this snapshot using current metric definitions; original exports remain unchanged. No historical short/long split assumed.",
                "processing_code_hashes": sorted({o.get("versions", {}).get("code_hash") for o in outcomes if o.get("versions", {}).get("code_hash")}),
                "source_started_at": run["started_at"], "source_completed_at": run["completed_at"],
            }
            for name, items in (("outcomes.jsonl", outcomes), ("decisions.jsonl", events)):
                (pending / name).write_text("".join(json.dumps(x, ensure_ascii=False, allow_nan=False, default=str) + "\n" for x in items), encoding="utf-8")
            _json(pending / "metrics.json", metrics)
            (pending / "metrics.md").write_text("# Measured report snapshot\n\n```json\n" + json.dumps(metrics, indent=2, ensure_ascii=False) + "\n```\n", encoding="utf-8")
            c = metrics["counts"]
            (pending / "report.md").write_text(
                "# CloudServe run report\n\n"
                + f"Report type: **{kind}**\n\nRun state at capture: **{run['state']}**\n\nCaptured: {captured_at}\n\n"
                + f"| Measure | Count |\n|---|---:|\n| Input tickets | {c['input']} |\n| Processed | {c['processed']} |\n| Answered | {c['answered']} |\n| Human handoffs | {c['escalated']} |\n| Blocked releases (included in handoffs) | {c['blocked']} |\n| Pending / unprocessed | {manifest['pending_records']} |\n\n"
                + "This report uses persisted decisions only. It does not make model calls, alter run state or overwrite earlier exports. An in-progress snapshot is not a completed-run report. Generated answers do not establish customer resolution or satisfaction.\n",
                encoding="utf-8",
            )
            manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(pending.iterdir()) if p.is_file()}
            _json(pending / "manifest.json", manifest)
            with zipfile.ZipFile(pending / "report.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for p in sorted(pending.iterdir()):
                    if p.is_file() and p.name != "report.zip":
                        archive.write(p, p.name)
            os.replace(pending, target)
        except BaseException:
            shutil.rmtree(pending, ignore_errors=True)
            raise
    return target / "report.zip"


def save_all_reports(settings, store):
    """Bundle every run visible when this request begins; failures are explicit."""
    root = Path(settings.storage) / "reports" / "downloads"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with store.connection() as db:
        ids = [r[0] for r in db.execute("SELECT run_id FROM runs ORDER BY created_at")]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / f"cloudserve-all-reports-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    pending = target.with_suffix(".partial")
    index = {"captured_at": datetime.now(timezone.utc).isoformat(), "run_count": len(ids), "reports": [], "errors": [], "note": "Each run has its own consistent capture time. Active runs are progress snapshots. Only runs persisted in this workspace database are included; no model calls."}
    try:
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for run_id in ids:
                try:
                    path = save_run_report(settings, store, run_id)
                    manifest = json.loads(path.with_name("manifest.json").read_text())
                    archive.write(path, f"{run_id}/report.zip")
                    index["reports"].append({"run_id": run_id, "report_kind": manifest["report_kind"], "captured_at": manifest["captured_at"]})
                except Exception as error:
                    index["errors"].append({"run_id": run_id, "error_type": type(error).__name__})
            index["exported_count"] = len(index["reports"])
            index["complete_export"] = not index["errors"]
            archive.writestr("index.json", json.dumps(index, indent=2))
        os.replace(pending, target)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return target
