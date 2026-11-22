"""The shared API/CLI workflow. Every node persists evidence before advancing."""

from __future__ import annotations
import json, time
from pathlib import Path
from typing import TypedDict, Any
from langgraph.graph import StateGraph, START, END
from .contracts import (
    Classification,
    Assessment,
    Outcome,
    GuardrailResult,
    RouteDecision,
)
from .ingest import normalize, digest
from . import route, generate, guardrails, handoff


class State(TypedDict, total=False):
    ticket: Any
    classification: Any
    sources: Any
    assessment: Any
    route: Any
    draft: Any
    checks: Any
    outcome: Any
    control: Any
    reason: str
    blocked: bool
    started: float
    provenance: str
    queue_seconds: float
    raw_sources: Any
    provider_metadata: Any


class PipelineService:
    def __init__(
        self,
        store,
        classifier,
        retriever,
        provider,
        policy,
        evaluator=None,
        decider_mode="disabled",
    ):
        self.store, self.classifier, self.retriever, self.provider, self.policy = (
            store,
            classifier,
            retriever,
            provider,
            policy,
        )
        self.evaluator, self.decider_mode = evaluator, decider_mode
        if evaluator is not None or decider_mode != "disabled":
            raise ValueError(
                "The optional Decider experiment has been removed; use disabled mode"
            )
        graph = StateGraph(State)
        for name in (
            "classify",
            "retrieve",
            "assess",
            "route",
            "draft",
            "validate",
            "finalize",
        ):
            graph.add_node(name, getattr(self, "_" + name))
        graph.add_edge(START, "classify")
        graph.add_edge("classify", "retrieve")
        graph.add_edge("retrieve", "assess")
        graph.add_edge("assess", "route")
        graph.add_conditional_edges(
            "route",
            lambda s: "draft" if s["route"].action == "auto_candidate" else "finalize",
        )
        graph.add_edge("draft", "validate")
        graph.add_edge("validate", "finalize")
        graph.add_edge("finalize", END)
        self.graph = graph.compile()

    def event(self, s, stage, data):
        t = s["ticket"]
        requirements = {
            "ingest": ["FR-01", "A2"],
            "classification": ["FR-02", "A3"],
            "retrieval": ["FR-03", "A4"],
            "sensitivity": ["FR-05"],
            "routing": ["FR-04", "A5"],
            "generation": ["FR-06", "A6"],
            "validation": ["FR-07", "A7"],
            "final": ["FR-09", "FR-13", "A8"],
        }
        data = {
            **data,
            "code_hash": getattr(self, "code_hash", None),
            "schema_version": 1,
            "input_index": t.input_index,
            "input_hash": t.input_hash,
            "requirement_ids": requirements.get(stage, []),
            "spec_version": "build-specs-v1",
            "policy_hash": digest(self.policy),
        }
        self.store.event(t.record_id, t.run_id, stage, data)

    def process(self, original, run_id, record_id, input_index):
        begin_record = getattr(self.provider, "begin_record", None)
        if begin_record:
            begin_record(record_id)
        ticket = normalize(original, run_id, record_id, input_index)
        from datetime import datetime, timezone

        run = self.store.get_run(run_id)
        queue_seconds = (
            max(
                0,
                (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(run["created_at"])
                ).total_seconds(),
            )
            if run
            and run["kind"] in ("manual", "development_upload", "development_builtin")
            else 0.0
        )
        state = State(
            ticket=ticket,
            queue_seconds=queue_seconds,
            started=time.monotonic(),
            control=self.store.control(),
            checks=[],
            blocked=False,
            provenance=(
                "Fixture replay"
                if getattr(self.provider, "metadata", {}).get("fixture")
                else "Live"
            ),
        )
        self.event(
            state,
            "ingest",
            {
                "valid": ticket.valid,
                "errors": ticket.errors,
                "input_hash": ticket.input_hash,
                "schema_version": 1,
            },
        )
        return self.graph.invoke(state)["outcome"]

    def _classify(self, s):
        t = s["ticket"]
        try:
            c = (
                self.classifier.predict(t.subject + "\n" + t.body)
                if t.valid
                else Classification(status="unavailable", reason="invalid_input")
            )
        except Exception:
            c = Classification(status="unavailable", reason="classifier_unavailable")
        self.event(s, "classification", c.model_dump())
        return {"classification": c}

    def _retrieve(self, s):
        if hasattr(self.retriever, "last_candidates"):
            self.retriever.last_candidates = []
        try:
            sources = (
                self.retriever.search(s["ticket"].subject + "\n" + s["ticket"].body)
                if s["ticket"].valid
                else []
            )
        except Exception:
            sources = []
        self.event(
            s,
            "retrieval",
            {
                "sources": [x.model_dump() for x in sources],
                "reason": None if sources else "no_support_or_unavailable",
                "query_metadata": getattr(self.retriever, "last_query_metadata", {}),
            },
        )
        return {
            "sources": sources,
            "raw_sources": (
                list(getattr(self.retriever, "last_candidates", sources))
                if s["ticket"].valid
                else []
            ),
        }

    def _assess(self, s):
        t = s["ticket"]
        if not t.valid or not s["control"]["enabled"]:
            reason = "invalid_input" if not t.valid else "automation_stopped"
            a = Assessment(decision="unknown", reasons=[reason])
            self.event(
                s,
                "sensitivity",
                {
                    "status": "skipped",
                    "reason": reason,
                    "assessment": a.model_dump(),
                    "provenance": s["provenance"],
                },
            )
            return {"assessment": a}
        classification = s["classification"]
        if classification.status != "ok" or (
            classification.intent is not None
            and classification.intent.label in route.SENSITIVE
        ):
            reason = (
                "classification_unavailable"
                if classification.status != "ok"
                else "mandatory_sensitive_intent"
            )
            assessment = Assessment(decision="escalate", reasons=[reason])
            self.event(
                s,
                "sensitivity",
                {
                    "status": "not_called",
                    "reason": reason,
                    "assessment": assessment.model_dump(),
                    "provider": {
                        "cost_policy": "not_applicable",
                        "status": "not_called",
                        "request_attempts": 0,
                    },
                    "provenance": s["provenance"],
                },
            )
            return {"assessment": assessment}
        cache_key = digest(
            {
                "cache_type": "generation-sensitivity-v3",
                "text": t.subject + "\n" + t.body,
                "schema": 1,
                "params": {
                    "temperature": 0,
                    "max_tokens": 500,
                    "reasoning_policy": getattr(
                        self.provider, "reasoning_policy", "provider_default"
                    ),
                },
                "policy": self.policy,
                "model": getattr(self.provider, "model", None),
                "corpus": getattr(self.retriever, "metadata", {}),
                "prompt": (
                    digest(
                        Path(__file__)
                        .resolve()
                        .parents[1]
                        .joinpath("prompts/build/PR-03.txt")
                        .read_text()
                    )
                    if Path(__file__)
                    .resolve()
                    .parents[1]
                    .joinpath("prompts/build/PR-03.txt")
                    .exists()
                    else "unavailable"
                ),
            }
        )
        cached = self.store.cache_get(cache_key)
        provenance = (
            "Fixture replay"
            if getattr(self.provider, "metadata", {}).get("fixture")
            else "Live"
        )
        if cached:
            try:
                a = Assessment.model_validate(cached["assessment"])
                provider_metadata = cached["provider"]
                provenance = (
                    "Fixture replay"
                    if getattr(self.provider, "metadata", {}).get("fixture")
                    else "Cached"
                )
            except Exception:
                cached = None
        if not cached:
            try:
                a = (
                    guardrails.assess(self.provider, t)
                    if t.valid
                    else Assessment(decision="unknown", reasons=["invalid_input"])
                )
            except Exception:
                a = Assessment(decision="unknown", reasons=["provider_unavailable"])
            local_assessment = bool(
                guardrails.INJECTION.search(t.subject + "\n" + t.body)
                or guardrails.AUTHORITY.search(t.subject + "\n" + t.body)
            )
            provider_metadata = (
                {
                    "cost_policy": "not_applicable",
                    "status": "not_called",
                    "reason": "deterministic_local_assessment",
                    "request_attempts": 0,
                }
                if local_assessment
                else json.loads(
                    json.dumps(getattr(self.provider, "metadata", {}), default=str)
                )
            )
            if a.decision != "unknown":
                self.store.cache_put(
                    cache_key,
                    {"assessment": a.model_dump(), "provider": provider_metadata},
                )
        self.event(
            s,
            "sensitivity",
            {
                "assessment": a.model_dump(),
                "provenance": provenance,
                "cache_key": cache_key,
                "provider": provider_metadata,
            },
        )
        return {
            "assessment": a,
            "provenance": provenance,
            "provider_metadata": provider_metadata,
        }

    def _route(self, s):
        r = route.route(
            s["ticket"],
            s["classification"],
            s["sources"],
            s["assessment"],
            s["control"],
            self.policy,
        )
        from .diagnostics import explain_route

        r = explain_route(
            r,
            s["classification"],
            s["assessment"],
            s.get("raw_sources", s["sources"]),
            self.provider,
        )
        self.event(s, "routing", r.model_dump())
        return {
            "route": r,
            "blocked": any(
                code in ("instruction_injection",) for code in r.reason_codes
            ),
        }

    def _draft(self, s):
        try:
            d = generate.draft(self.provider, s["ticket"], s["sources"])
            self.event(
                s,
                "generation",
                {
                    "status": d.status,
                    "claim_count": len(d.claims),
                    "model": getattr(self.provider, "metadata", {}),
                    "draft_exposed": False,
                },
            )
            return {
                "draft": d,
                "provider_metadata": json.loads(
                    json.dumps(getattr(self.provider, "metadata", {}), default=str)
                ),
            }
        except Exception as e:
            self.event(
                s,
                "generation",
                {
                    "status": "unavailable",
                    "error_class": type(e).__name__,
                    "provider": dict(getattr(self.provider, "metadata", {})),
                },
            )
            from .diagnostics import provider_failure

            return {
                "draft": None,
                "blocked": True,
                "reason": provider_failure(self.provider, "Answer generation"),
                "provider_metadata": json.loads(
                    json.dumps(getattr(self.provider, "metadata", {}), default=str)
                ),
            }

    def _validate(self, s):
        identifiers = []
        if s.get("draft") is not None:
            # Local protected input is inspected here only; identities never enter provider state.
            import re

            original = self.store.original(s["ticket"].record_id)
            if isinstance(original, dict):
                identifiers = [
                    str(original.get(k, ""))
                    for k in ("customer_name", "customer_id")
                    if original.get(k)
                ]
                raw = (
                    str(original.get("subject", ""))
                    + "\n"
                    + str(original.get("body", ""))
                )
                identifiers.extend(
                    re.findall(
                        r'(?i)(?:password|api[_ -]?key|token|secret)\s*[:=]\s*["\']?([^\s"\']+)',
                        raw,
                    )
                )
        if s.get("draft") is None:
            checks = [
                GuardrailResult(
                    name="schema", status="unknown", reasons=["generation_unavailable"]
                )
            ]
        else:
            try:
                checks = guardrails.validate(
                    self.provider,
                    s["ticket"],
                    s["draft"],
                    s["sources"],
                    s["classification"],
                    s["route"],
                    private_identifiers=identifiers,
                )
            except Exception:
                checks = [
                    GuardrailResult(
                        name="validation",
                        status="unknown",
                        reasons=["validator_unavailable"],
                    )
                ]
        self.event(
            s,
            "validation",
            {
                "checks": [x.model_dump() for x in checks],
                "provider": dict(getattr(self.provider, "metadata", {})),
            },
        )
        passed = bool(checks) and all(x.status == "pass" for x in checks)
        failures = ", ".join(
            f"{c.name} ({c.status})" for c in checks if c.status != "pass"
        )
        return {
            "checks": checks,
            "blocked": not passed,
            "reason": (
                None if passed else "Mandatory answer checks did not pass: " + failures
            ),
            "provider_metadata": json.loads(
                json.dumps(getattr(self.provider, "metadata", {}), default=str)
            ),
        }

    def _finalize(self, s):
        t = s["ticket"]
        candidate = s["route"].action == "auto_candidate"
        answer = None
        if candidate and not s.get("blocked") and s.get("draft"):
            answer = guardrails.render(s["draft"])
        reason = s.get("reason") or s["route"].reason
        if not candidate:
            self.event(s, "generation", {"status": "skipped", "reason": reason})
            self.event(
                s,
                "validation",
                {
                    "status": "skipped",
                    "reason": "Answer checks were not run because no answer was generated. "
                    + reason,
                    "upstream_reason_codes": s["route"].reason_codes,
                },
            )
        outcome = Outcome(
            record_id=t.record_id,
            run_id=t.run_id,
            input_index=t.input_index,
            action="auto_respond" if answer else "escalate",
            blocked=s.get("blocked", False),
            reason=reason,
            answer=answer,
            handoff=(
                None
                if answer
                else handoff.build(t, s["classification"], s["sources"], reason)
            ),
            classification=s["classification"],
            sources=s["sources"],
            route=s["route"],
            checks=s["checks"],
            provenance=s["provenance"],
            timings={
                "processing_seconds": time.monotonic() - s["started"],
                "queue_seconds": s["queue_seconds"],
                "end_to_end_seconds": s["queue_seconds"]
                + time.monotonic()
                - s["started"],
            },
            versions={
                "code_hash": getattr(self, "code_hash", None),
                "schema": 1,
                "policy": digest(self.policy),
                "model": s.get("provider_metadata", {}),
                "decider": {
                    "mode": self.decider_mode,
                    "fingerprint": (
                        self.evaluator.fingerprint() if self.evaluator else None
                    ),
                },
            },
        )
        return {"outcome": self.store.finalize(outcome, s["control"]["epoch"])}
