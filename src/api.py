from __future__ import annotations
import asyncio, json, secrets, uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.exceptions import RequestValidationError
from .contracts import TicketInput, ControlInput, ReviewInput
from .runtime import create_runtime
from .jobs import JobRunner
from filelock import FileLock, Timeout
from .operator_settings import (
    OperatorSettings,
    OperatorSettingsConflict,
    OperatorSettingsUnavailable,
    read_operator_settings,
    persist_operator_settings,
    build_providers,
)
from .runtime import apply_operator_values


def create_app(runtime=None, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        settings, store, pipeline = runtime or await asyncio.to_thread(create_runtime)
        app.state.settings, app.state.store, app.state.pipeline = (
            settings,
            store,
            pipeline,
        )
        app.state.runner = JobRunner(store, pipeline)
        if start_worker:
            app.state.runner.start()
        yield
        app.state.runner.stop()

    app = FastAPI(title="CloudServe Support", version="1.0.0", lifespan=lifespan)

    def auth(request: Request):
        token = request.app.state.settings.operator_token
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or not secrets.compare_digest(token, supplied):
            raise HTTPException(401, "Operator token required")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origin rejected")

    @app.exception_handler(HTTPException)
    async def http_error(request, e):
        return JSONResponse(
            status_code=e.status_code,
            content={
                "error": {
                    "code": str(e.status_code),
                    "message": str(e.detail),
                    "request_id": str(uuid.uuid4()),
                    "details": None,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request, e):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "Invalid request fields",
                    "request_id": str(uuid.uuid4()),
                    "details": [
                        {"field": list(x["loc"]), "type": x["type"]} for x in e.errors()
                    ],
                }
            },
        )

    @app.exception_handler(Exception)
    async def internal(request, e):
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Operation could not be durably completed",
                    "request_id": str(uuid.uuid4()),
                    "details": None,
                }
            },
        )

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready(request: Request):
        p = request.app.state.pipeline
        request.app.state.store.control()
        assets = bool(p.classifier.ready and p.retriever.ready)
        worker = not start_worker or request.app.state.runner.thread.is_alive()
        return JSONResponse(
            status_code=200 if assets and worker else 503,
            content={
                "ready": assets and worker,
                "assets": assets,
                "worker": worker,
                "provider": "configured" if p.provider.api_key else "missing_key",
            },
        )

    def budget_status(request):
        from .paid_budget import BudgetLedger, LIMIT

        try:
            if not hasattr(request.app.state, "budget_ledger"):
                request.app.state.budget_ledger = BudgetLedger(request.app.state.store)
            return request.app.state.budget_ledger.snapshot()
        except RuntimeError:
            return {
                "limit_usd": str(LIMIT),
                "spent_usd": None,
                "reserved_usd": None,
                "remaining_usd": None,
                "blocked": True,
                "reason": "budget_unavailable",
            }

    @app.get("/api/v1/status")
    def status(request: Request):
        p = request.app.state.pipeline
        s = request.app.state.store
        mode = getattr(request.app.state.settings, "generation_mode", "free_only")
        budget = budget_status(request)
        provider_error = (
            p.provider.metadata.get("last_call_status") == "failed"
            or p.provider.metadata.get("last_http_status", 0)
            in (400, 401, 402, 403, 404, 422, 429, 500, 502, 503, 504)
            or p.provider.metadata.get("last_error") == "network_timeout"
        )
        if mode == "paid_test":
            from decimal import Decimal

            provider_error = (
                provider_error
                or budget["blocked"]
                or budget["remaining_usd"] is None
                or Decimal(budget["remaining_usd"]) <= 0
            )
        return {
            "mode": "Live",
            "generation_mode": mode,
            "paid_budget": budget,
            "status": (
                "degraded"
                if provider_error
                or not (p.classifier.ready and p.retriever.ready and p.provider.api_key)
                else "configured"
            ),
            "classifier": p.classifier.metadata,
            "retrieval": p.retriever.metadata,
            "provider": p.provider.metadata,
            "automation": s.control(),
            "issues": (
                [
                    "Urgency is advisory; development accuracy 41%, operator confirmation required."
                ]
                + (
                    [
                        "Model response is unavailable or rate limited, or the test budget is blocked; inspect status. Safe handoffs remain available."
                    ]
                    if provider_error
                    else []
                )
                + (
                    []
                    if p.provider.api_key
                    else ["OpenRouter key missing; automatic answers unavailable"]
                )
            ),
        }

    @app.get("/api/v1/controls/automation")
    def control(request: Request):
        return request.app.state.store.control()

    def settings_view(request):
        s = request.app.state.settings
        p = request.app.state.pipeline
        return {
            **read_operator_settings(s, request.app.state.store),
            "credential_configured": bool(s.api_key),
            "policy_ready": bool(
                p.policy.get("calibration_ready") and p.policy.get("retrieval_ready")
            ),
            "paid_budget": budget_status(request),
        }

    @app.get("/api/v1/settings", dependencies=[Depends(auth)])
    def get_settings(request: Request):
        return settings_view(request)

    @app.put("/api/v1/settings", dependencies=[Depends(auth)])
    def save_settings(value: OperatorSettings, request: Request):
        p = request.app.state.pipeline
        s = request.app.state.settings
        store = request.app.state.store
        try:
            with FileLock(str(s.storage / "pipeline.lock")).acquire(timeout=0):
                if store.control()["enabled"]:
                    raise OperatorSettingsConflict(
                        "Stop automatic answers before changing settings"
                    )
                with store.connection() as db:
                    if db.execute(
                        "SELECT 1 FROM runs WHERE state IN ('queued','running') LIMIT 1"
                    ).fetchone():
                        raise OperatorSettingsConflict(
                            "Wait for the active run to finish before changing settings"
                        )
                provider, evaluator = build_providers(s, value)
                try:
                    if not provider.preflight().get("verified"):
                        raise ValueError("Generation provider not verified")
                except Exception:
                    raise HTTPException(
                        503,
                        "Selected models or test budget could not be verified. Check the key, model capability, budget and provider availability.",
                    ) from None
                persist_operator_settings(store, value)
                # Shared lock excludes worker/harness tickets and concurrent enables.
                # Nothing below invokes network or can release an answer.
                p.provider, p.evaluator, p.decider_mode = (
                    provider,
                    evaluator,
                    value.decider_mode,
                )
                apply_operator_values(s, value.model_dump())
                return settings_view(request)
        except Timeout:
            raise HTTPException(
                409, "A ticket or settings operation is in progress; retry when idle"
            ) from None
        except OperatorSettingsConflict as e:
            raise HTTPException(409, str(e)) from None
        except OperatorSettingsUnavailable:
            raise HTTPException(
                503, "Settings could not be durably saved; releases blocked"
            ) from None

    @app.put("/api/v1/controls/automation", dependencies=[Depends(auth)])
    def control_set(value: ControlInput, request: Request):
        p = request.app.state.pipeline
        if not value.enabled:
            return request.app.state.store.set_control(
                False, value.reason, "local_operator"
            )
        try:
            with FileLock(
                str(request.app.state.settings.storage / "pipeline.lock")
            ).acquire(timeout=0):
                if not (p.classifier.ready and p.retriever.ready and p.policy):
                    raise HTTPException(409, "Frozen assets and policy required")
                try:
                    p.provider.preflight()
                except Exception:
                    raise HTTPException(
                        409,
                        "Provider or paid-test budget preflight failed; automation stays stopped",
                    )
                return request.app.state.store.set_control(
                    True, value.reason, "local_operator"
                )
        except Timeout:
            raise HTTPException(
                409, "A ticket or settings operation is in progress; retry when idle"
            ) from None

    @app.post("/api/v1/tickets", status_code=202, dependencies=[Depends(auth)])
    def submit(value: TicketInput, request: Request):
        if not value.body.strip():
            raise HTTPException(422, "Body must contain text")
        s = request.app.state.store
        try:
            rid = s.create_run([value.model_dump()], kind="manual")
        except RuntimeError:
            raise HTTPException(409, "Another run is active; wait for it to finish")
        run = s.get_run(rid)
        tickets = s.list_tickets(limit=1, run_id=rid)["items"]
        record_id = tickets[0]["record_id"]
        return {
            "run_id": rid,
            "record_id": record_id,
            "job_id": record_id,
            "state": run["state"],
        }

    @app.get("/api/v1/tickets")
    def tickets(
        request: Request,
        limit: int = 25,
        offset: int = 0,
        channel: str | None = None,
        action: str | None = None,
        urgency: str | None = None,
        blocked: bool | None = None,
        search: str | None = None,
    ):
        return request.app.state.store.list_tickets(
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
            channel=channel,
            action=action,
            urgency=urgency,
            blocked=blocked,
            search=search,
        )

    @app.get("/api/v1/tickets/{record_id}")
    def ticket(record_id: str, request: Request):
        value = request.app.state.store.get_ticket(record_id)
        if not value:
            raise HTTPException(404, "Ticket not found")
        return value

    @app.get("/api/v1/tickets/{record_id}/original", dependencies=[Depends(auth)])
    def original(record_id: str, request: Request):
        return request.app.state.store.original(record_id)

    @app.get("/api/v1/tickets/{record_id}/events")
    def events(record_id: str, request: Request, limit: int = 25, offset: int = 0):
        return request.app.state.store.events(
            record_id=record_id, limit=min(100, max(1, limit)), offset=max(0, offset)
        )

    @app.post("/api/v1/tickets/{record_id}/review", dependencies=[Depends(auth)])
    def review(record_id: str, value: ReviewInput, request: Request):
        return request.app.state.store.review(record_id, value.status, value.notes)

    @app.get("/api/v1/sources/{chunk_id}")
    def source(chunk_id: str, request: Request):
        value = request.app.state.pipeline.retriever.resolve(chunk_id)
        if value is None:
            raise HTTPException(404, "Source not found")
        return value

    @app.post("/api/v1/runs", status_code=202, dependencies=[Depends(auth)])
    async def upload(request: Request, file: UploadFile = File(...)):
        raw = await file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(413, "Upload exceeds 10 MiB")
        try:
            records = json.loads(raw)
        except Exception:
            raise HTTPException(422, "Upload must be valid JSON")
        if not isinstance(records, list) or len(records) > 10000:
            raise HTTPException(422, "JSON array of at most 10000 records required")
        try:
            rid = await asyncio.to_thread(
                request.app.state.store.create_run, records, "development_upload"
            )
        except RuntimeError:
            raise HTTPException(409, "Another run is active; wait for it to finish")
        if not records:
            from evaluation.export import export_run

            await asyncio.to_thread(
                export_run,
                request.app.state.settings,
                request.app.state.store,
                request.app.state.pipeline,
                rid,
            )
        return {"run_id": rid, "expected_count": len(records)}

    @app.get("/api/v1/runs")
    def runs(request: Request, limit: int = 25, offset: int = 0):
        return request.app.state.store.list_runs(
            limit=min(100, max(1, limit)), offset=max(0, offset)
        )

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: str, request: Request):
        value = request.app.state.store.get_run(run_id)
        if not value:
            raise HTTPException(404, "Run not found")
        return value

    @app.get("/api/v1/runs/{run_id}/metrics")
    def run_metrics(run_id: str, request: Request):
        from .metrics import compute_metrics

        store = request.app.state.store
        tickets = sorted(
            store.list_tickets(limit=10000, run_id=run_id)["items"],
            key=lambda x: x["input_index"],
        )
        if not store.get_run(run_id):
            raise HTTPException(404, "Run not found")
        records = [store.original(x["record_id"]) for x in tickets]
        events = store.events(run_id=run_id, limit=100000)["items"]
        from evaluation.harness import frozen_body_boundary

        return compute_metrics(
            [x["outcome"] for x in tickets if x.get("outcome")],
            records,
            events=events,
            body_boundary=frozen_body_boundary(request.app.state.settings),
        )

    @app.get("/api/v1/runs/{run_id}/artifacts/{name}", dependencies=[Depends(auth)])
    def artifact(run_id: str, name: str, request: Request):
        allowed = {
            "manifest.json",
            "outcomes.jsonl",
            "decisions.jsonl",
            "metrics.json",
            "metrics.md",
            "classification.csv",
            "human_review_sample.csv",
            "classification_metrics.json",
            "retrieval_analysis.json",
            "segments.json",
            "errors.json",
            "reviewer_1.csv",
            "reviewer_2.csv",
            "availability.csv",
        }
        if name not in allowed or not request.app.state.store.get_run(run_id):
            raise HTTPException(404, "Artifact not found")
        path = request.app.state.settings.storage / "evaluation" / run_id / name
        if not path.is_file():
            raise HTTPException(
                404, "Artifact not exported; use documented harness command"
            )
        return FileResponse(path, filename=name)

    @app.get("/metrics")
    def prometheus(request: Request):
        from .metrics import prometheus_text

        return Response(
            prometheus_text(request.app.state.store),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/", include_in_schema=False)
    def home():
        return {
            "service": "CloudServe Support",
            "api_docs": "/docs",
            "metrics": "/metrics",
            "batch_command": "python -m evaluation.harness --input INPUT --output OUTPUT --kind development",
        }

    return app


app = create_app()


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Start the local FastAPI interface")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        "src.api:app", host="127.0.0.1", port=args.port, log_config="logging.json"
    )
