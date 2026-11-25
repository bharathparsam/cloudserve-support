"""Small local dashboard: static assets and authenticated workflow adapters."""
from pathlib import Path
import json
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from .operator_settings import OperatorSettings

UI = Path(__file__).resolve().parents[1] / "ui"
MODELS = (
    {"id": "google/gemma-4-26b-a4b-it:free", "name": "Gemma 4 · 26B", "mode": "free_only"},
    {"id": "meta-llama/llama-3.1-8b-instruct:free", "name": "Llama 3.1 · 8B", "mode": "free_only"},
    {"id": "openai/gpt-4.1", "name": "GPT-4.1", "mode": "paid_test"},
    {"id": "openai/gpt-4.1-mini", "name": "GPT-4.1 Mini", "mode": "paid_test"},
)
STAGES = ("ingest", "classification", "retrieval", "sensitivity", "routing", "generation", "validation", "final")


def register_console(app, auth, save_settings):
    app.mount("/ui", StaticFiles(directory=UI), name="ui")

    @app.get("/api/v1/console/models", dependencies=[Depends(auth)])
    def models():
        return {"items": MODELS, "availability": "Candidates; provider metadata is checked when saving. A listing is not proof of quota or successful inference."}

    @app.put("/api/v1/console/settings", dependencies=[Depends(auth)])
    def settings(value: OperatorSettings, request: Request):
        if not any(m["id"] == value.generation_model and m["mode"] == value.generation_mode for m in MODELS):
            raise HTTPException(422, "Choose one of the dashboard's supported models")
        return save_settings(value, request)

    @app.post("/api/v1/console/runs", status_code=202, dependencies=[Depends(auth)])
    async def upload(request: Request, file: UploadFile = File(...), authorize_paid: bool = Form(False), expected_model: str = Form(...)):
        raw = await file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(413, "Choose a JSON file smaller than 10 MiB")
        try:
            records = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Choose a valid JSON file") from None
        if not isinstance(records, list) or not 1 <= len(records) <= 10000:
            raise HTTPException(422, "Use a JSON array containing 1 to 10000 tickets")
        s, store = request.app.state.settings, request.app.state.store

        def admit():
            try:
                with FileLock(str(s.storage / "pipeline.lock")).acquire(timeout=1):
                    if not store.control()["enabled"]:
                        raise HTTPException(409, "Enable automatic answers before starting a run")
                    if s.model != expected_model:
                        raise HTTPException(409, "Model settings changed; refresh before starting this run")
                    mode = getattr(s, "generation_mode", "free_only")
                    if mode == "paid_test" and not authorize_paid:
                        raise HTTPException(409, "Confirm paid usage for this run before starting")
                    if mode == "paid_test":
                        from .paid_budget import BudgetLedger
                        from decimal import Decimal
                        budget = BudgetLedger(store).snapshot()
                        if budget["blocked"] or Decimal(budget["remaining_usd"]) <= 0:
                            raise HTTPException(409, "Paid budget is blocked or exhausted; choose a free model")
                    return store.create_run(records, "development_upload", admission={
                        "generation_mode": mode, "generation_model": s.model,
                        "paid_usage_authorized": mode == "paid_test" and authorize_paid,
                        "actor": "local_operator", "surface": "dashboard",
                    })
            except Timeout:
                raise HTTPException(409, "A ticket is processing; retry when it finishes") from None
            except RuntimeError:
                raise HTTPException(409, "Another run is active; wait for it to finish") from None
        import asyncio
        rid = await asyncio.to_thread(admit)
        return {"run_id": rid, "expected_count": len(records)}

    @app.get("/api/v1/console/reports/archive", dependencies=[Depends(auth)])
    def all_reports(request: Request):
        from .reports import save_all_reports
        path = save_all_reports(request.app.state.settings, request.app.state.store)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.get("/api/v1/console/runs/{run_id}/report", dependencies=[Depends(auth)])
    def report(run_id: str, request: Request):
        from .reports import save_run_report
        if request.app.state.store.get_run(run_id) is None:
            raise HTTPException(404, "Run not found")
        path = save_run_report(request.app.state.settings, request.app.state.store, run_id)
        return FileResponse(path, media_type="application/zip", filename=f"cloudserve-report-{run_id}.zip")

    @app.get("/api/v1/console/runs/{run_id}/outcomes", dependencies=[Depends(auth)])
    def download_outcomes(run_id: str, request: Request):
        store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        if run["state"] in ("queued", "running"):
            raise HTTPException(409, "Wait for the run to finish before downloading")
        with store.connection() as db:
            rows = db.execute(
                "SELECT o.data FROM outcomes o JOIN input_records i USING(record_id) "
                "WHERE o.run_id=? ORDER BY i.input_index", (run_id,),
            ).fetchall()
        return Response(
            "".join(row[0] + "\n" for row in rows),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="cloudserve-outcomes.jsonl"'},
        )

    @app.get("/api/v1/console/runs/{run_id}/summary", dependencies=[Depends(auth)])
    def summary(run_id: str, request: Request):
        store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        with store.connection() as db:
            rows = db.execute("SELECT data FROM outcomes WHERE run_id=?", (run_id,)).fetchall()
            events = db.execute("SELECT record_id,stage,data FROM decision_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        outcomes = [json.loads(r[0]) for r in rows]
        counts = {"input": run["expected_count"], "processed": len(outcomes),
                  "answered": sum(o["action"] == "auto_respond" for o in outcomes),
                  "escalated": sum(o["action"] == "escalate" for o in outcomes),
                  "blocked": sum(bool(o.get("blocked")) for o in outcomes),
                  "invalid": sum("invalid_input" in o.get("route", {}).get("reason_codes", []) for o in outcomes)}
        seen = {(e["record_id"], e["stage"]): json.loads(e["data"]) for e in events if e["stage"] in STAGES}
        stages = {stage: {"recorded": sum(key[1] == stage for key in seen),
                          "skipped": sum(key[1] == stage and value.get("status") in ("skipped", "not_called") for key, value in seen.items())} for stage in STAGES}
        return {"run": run, "counts": counts, "stages": stages}
