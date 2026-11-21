"""Frozen shared contract v1. Provider payloads forbid extra fields."""

from __future__ import annotations
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1
Channel = Literal["email", "chat", "docs_comment", "forum"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TicketInput(BaseModel):
    channel: Channel
    body: str = Field(min_length=1, max_length=20000)
    subject: str = Field(default="", max_length=1000)
    ticket_id: str | None = None
    received_at: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None

    @field_validator("body")
    @classmethod
    def nonblank_body(cls, value):
        if not value.strip():
            raise ValueError("Body must contain text")
        return value

    @field_validator("received_at")
    @classmethod
    def iso_receipt(cls, value):
        if value is not None:
            from datetime import datetime

            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class NormalizedTicket(StrictModel):
    schema_version: int = 1
    record_id: str
    run_id: str
    input_index: int
    ticket_id: str | None = None
    channel: str | None = None
    subject: str = ""
    body: str = ""
    received_at: str | None = None
    ingested_at: str
    input_hash: str
    valid: bool
    errors: list[str] = []


class Dimension(StrictModel):
    label: str
    raw_probabilities: dict[str, float]
    raw_probability: float = Field(ge=0, le=1)
    calibrated_probability: float = Field(ge=0, le=1)
    alternatives: list[dict[str, Any]] = []
    provider_confidence: None = None


class Classification(StrictModel):
    status: Literal["ok", "unavailable"]
    intent: Dimension | None = None
    urgency: Dimension | None = None
    artifact_id: str | None = None
    calibration_status: str = "unavailable"
    reason: str | None = None


class SourcePassage(StrictModel):
    doc_id: str
    chunk_id: str
    title: str
    content: str
    start: int
    end: int
    corpus_hash: str
    score: float
    score_kind: str = "cosine_similarity"


class Assessment(StrictModel):
    decision: Literal["clear", "escalate", "unknown"]
    reasons: list[str]
    evidence: list[str] = []


class Citation(StrictModel):
    doc_id: str
    chunk_id: str


class Claim(StrictModel):
    claim_id: str
    text: str = Field(min_length=1, max_length=2000)
    citations: list[Citation] = Field(min_length=1)


class AnswerDraft(StrictModel):
    status: Literal["draft", "insufficient_support"]
    automation_disclosure: str
    claims: list[Claim] = Field(max_length=10)
    uncertainties: list[str] = []


class GuardrailResult(StrictModel):
    name: str
    version: str = "1"
    status: Literal["pass", "fail", "unknown", "not_applicable"]
    reasons: list[str] = []
    claim_ids: list[str] = []


class SemanticCheck(StrictModel):
    claims: list[GuardrailResult]
    instruction_integrity: Literal["pass", "fail", "unknown"]
    tone_scope: Literal["pass", "fail", "unknown"]


class RouteDecision(StrictModel):
    action: Literal["auto_candidate", "escalate"]
    reason_codes: list[str]
    reason: str
    policy_hash: str
    thresholds: dict[str, Any] = {}


class Handoff(StrictModel):
    summary: str
    reason: str
    original_reference: str
    classification: Classification
    sources: list[SourcePassage] = []
    troubleshooting: str = "Previous troubleshooting not supplied."
    missing_context: list[str] = ["Previous troubleshooting not supplied."]
    unresolved_questions: list[str] = [
        "An operator must assess the request and confirm next steps."
    ]
    provenance: str = "local_template"


class Outcome(StrictModel):
    schema_version: int = 1
    record_id: str
    run_id: str
    input_index: int
    action: Literal["auto_respond", "escalate"]
    blocked: bool
    reason: str
    answer: str | None = None
    handoff: Handoff | None = None
    classification: Classification
    sources: list[SourcePassage] = []
    route: RouteDecision
    checks: list[GuardrailResult] = []
    provenance: str = "Live"
    timings: dict[str, float] = {}
    versions: dict[str, Any] = {}
    closed: None = None


class ControlInput(StrictModel):
    enabled: bool
    reason: str = Field(min_length=3, max_length=500)


class ReviewInput(StrictModel):
    status: Literal["open", "in_review", "reviewed"]
    notes: str = Field(default="", max_length=2000)
