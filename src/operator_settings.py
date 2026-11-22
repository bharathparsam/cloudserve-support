"""Durable, audited operator model selection. Credentials stay in local environment.

The coordinator owns the shared pipeline lock and metadata-only provider
preflight. This module rechecks stopped/idle conditions in its SQLite write
transaction so a concurrent enable or batch admission cannot invalidate them.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal

from .providers.openrouter import OpenRouterProvider


class OperatorSettingsConflict(RuntimeError):
    """Configuration requires stopped automation and no active batch/job."""


class OperatorSettingsUnavailable(RuntimeError):
    """Configuration could not be read, audited, or verified safely."""


FREE_MODEL_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*:free\Z"
)
FIELDS = (
    "generation_model",
    "decider_model",
    "decider_mode",
    "timeout_seconds",
    "max_retries",
    "generation_mode",
)
SNAPSHOT_FIELDS = {
    "revision",
    "updated_at",
    "source",
    "credential_configured",
    "policy_ready",
    "paid_budget",
}


class OperatorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    generation_model: str = Field(max_length=200)
    decider_model: str = Field(default="legacy/disabled:free", max_length=200)
    decider_mode: Literal["disabled"] = "disabled"
    timeout_seconds: float = Field(ge=5, le=120)
    max_retries: int = Field(ge=0, le=2)
    generation_mode: Literal["free_only", "paid_test"] = "free_only"

    @field_validator("decider_model")
    @classmethod
    def explicit_free_variant(cls, value):
        if not FREE_MODEL_ID.fullmatch(value):
            raise ValueError("Select an explicit provider/model:free identifier")
        return value

    @model_validator(mode="after")
    def validate_generation_policy(self):
        if self.generation_mode == "free_only":
            if not FREE_MODEL_ID.fullmatch(self.generation_model):
                raise ValueError(
                    "Free-only mode requires an explicit provider/model:free identifier"
                )
        else:
            from .providers.paid_test import PaidTestProvider

            if self.generation_model not in PaidTestProvider.allowed_models:
                raise ValueError(
                    "Paid development testing supports GPT-4.1, GPT-4.1 Mini or GPT-4o Mini"
                )
            if self.max_retries != 0:
                raise ValueError("Paid development testing requires zero retries")
        return self


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(db):
    # Individual execute calls preserve an enclosing write transaction;
    # executescript would implicitly commit it and break atomic audit semantics.
    db.execute("""CREATE TABLE IF NOT EXISTS operator_settings(
        id INTEGER PRIMARY KEY CHECK(id=1),
        revision INTEGER NOT NULL CHECK(revision>=1),
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS operator_settings_events(
        event_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL UNIQUE,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        data TEXT NOT NULL
    )""")


def _environment_values(settings):
    return OperatorSettings(
        generation_model=settings.model,
        decider_model=settings.decider_model,
        decider_mode=settings.decider_mode,
        timeout_seconds=float(
            getattr(
                settings, "timeout_seconds", os.getenv("MODEL_TIMEOUT_SECONDS", "30")
            )
        ),
        max_retries=int(
            getattr(settings, "max_retries", os.getenv("MODEL_MAX_RETRIES", "2"))
        ),
        generation_mode=getattr(settings, "generation_mode", "free_only"),
    )


def _snapshot(values, revision, updated_at, source):
    return {
        **values.model_dump(),
        "revision": revision,
        "updated_at": updated_at,
        "source": source,
    }


def read_operator_settings(settings, store):
    """Return only safe settings; an invalid persisted record is never ignored."""
    try:
        with store.connection() as db:
            _ensure_tables(db)
            row = db.execute(
                "SELECT revision,data,updated_at FROM operator_settings WHERE id=1"
            ).fetchone()
        if row is None:
            return _snapshot(_environment_values(settings), 0, None, "environment")
        values = OperatorSettings.model_validate_json(row["data"])
        return _snapshot(values, row["revision"], row["updated_at"], "operator")
    except Exception:
        raise OperatorSettingsUnavailable(
            "Operator settings could not be read safely"
        ) from None


def persist_operator_settings(store, values):
    """Persist mutation plus audit atomically, after caller preflight under lock.

    This never stores a credential, clears a failed-control latch, enables
    automation, changes records, or invokes a model/provider.
    """
    values = (
        values
        if isinstance(values, OperatorSettings)
        else OperatorSettings.model_validate(values)
    )
    payload = values.model_dump()
    stamp = _now()
    try:
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _ensure_tables(db)
            control = db.execute(
                "SELECT enabled FROM operator_controls WHERE id=1"
            ).fetchone()
            if control is None or bool(control["enabled"]):
                raise OperatorSettingsConflict(
                    "Stop automatic answers before changing settings"
                )
            active_run = db.execute(
                "SELECT 1 FROM runs WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone()
            active_job = db.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone()
            if active_run or active_job:
                raise OperatorSettingsConflict(
                    "Wait for the active run to finish before changing settings"
                )
            previous = db.execute(
                "SELECT revision,data FROM operator_settings WHERE id=1"
            ).fetchone()
            revision = previous["revision"] + 1 if previous else 1
            before = json.loads(previous["data"]) if previous else None
            if before is not None:
                # Preserve a legacy observer selection in the audit event while
                # allowing an explicit switch to the simplified configuration.
                # All other stored fields must still satisfy the current schema.
                if before.get("decider_mode", "disabled") not in (
                    "disabled",
                    "shadow",
                    "enforce",
                ):
                    raise ValueError("Invalid legacy observer configuration")
                OperatorSettings.model_validate({**before, "decider_mode": "disabled"})
            encoded = json.dumps(payload, allow_nan=False, sort_keys=True)
            db.execute(
                """INSERT INTO operator_settings(id,revision,data,updated_at) VALUES(1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data,updated_at=excluded.updated_at""",
                (revision, encoded, stamp),
            )
            audit = {
                "before": before,
                "after": payload,
                "requirement_ids": ["FR-09", "FR-13", "NFR-07"],
                "verification_boundary": "Coordinator provider preflight and shared lock; no inference in settings persistence.",
            }
            db.execute(
                "INSERT INTO operator_settings_events VALUES(?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    revision,
                    stamp,
                    "local_operator",
                    json.dumps(audit, allow_nan=False, sort_keys=True),
                ),
            )
        return _snapshot(values, revision, stamp, "operator")
    except OperatorSettingsConflict:
        raise
    except Exception:
        if hasattr(store, "_latch_closed"):
            store._latch_closed()
        raise OperatorSettingsUnavailable(
            "Operator settings could not be durably saved"
        ) from None


def _provider_values(values):
    if isinstance(values, OperatorSettings):
        return values
    if not isinstance(values, dict):
        raise ValueError("Settings object required")
    return OperatorSettings.model_validate(
        {k: v for k, v in values.items() if k not in SNAPSHOT_FIELDS}
    )


def build_providers(settings, values):
    """Build local adapters without making network requests or inference calls."""
    values = _provider_values(values)
    if values.generation_mode == "paid_test":
        from .providers.budgeted_paid import BudgetedPaidProvider
        from .logging_store import Store

        generation = BudgetedPaidProvider(
            api_key=settings.api_key,
            model=values.generation_model,
            store=Store(settings.db_path),
            timeout=values.timeout_seconds,
            max_retries=0,
        )
    else:
        generation = OpenRouterProvider(
            api_key=settings.api_key,
            model=values.generation_model,
            timeout=values.timeout_seconds,
            max_retries=values.max_retries,
            free_only=True,
        )
    return generation, None
