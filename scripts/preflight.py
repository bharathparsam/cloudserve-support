"""Provider/quota preflight. Paid evaluation is an explicit auditable exception."""

import argparse, hashlib, json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from src.ingest import now, digest
from evaluation.harness import (
    tree_hash,
    configured_fingerprint,
    paid_authorization,
    PAID_DEVIATION,
)


def assess_quota(provider_metadata, records, max_retries=2, extra_calls=0):
    calls = records * (3 + extra_calls) * (1 + max_retries)
    quota = provider_metadata.get("quota", {})
    remaining = quota.get("remaining_free_requests")
    evidenced = (
        type(remaining) is int
        and remaining >= 0
        and quota.get("free_request_allowance_verified") is True
    )
    return {
        "record_budget": records,
        "estimated_base_calls": records * (3 + extra_calls),
        "estimated_max_calls": calls,
        "remaining_free_requests": remaining if evidenced else None,
        "quota_sufficient": bool(
            provider_metadata.get("verified") and evidenced and remaining >= calls
        ),
        "quota_note": "Only verified free request allowance establishes free quota. Monetary balance is not free-request quota.",
    }


def empirical_smoke(store, run_ids, model):
    """Read paid metadata only from explicitly selected COMPLETED development runs."""
    allowed = {"development", "development_upload", "manual", "paid_development_test"}
    if not run_ids:
        raise RuntimeError("At least one completed paid development smoke run required")
    costs = {}
    answered_records = set()
    sample = []
    for run_id in dict.fromkeys(run_ids):
        run = store.get_run(run_id)
        if not run or run["kind"] not in allowed or run["state"] != "completed":
            raise RuntimeError(
                "Smoke must be a completed development run; supplied validation is forbidden"
            )
        with store.connection() as db:
            answered = {
                row[0]
                for row in db.execute(
                    "SELECT record_id FROM outcomes WHERE run_id=? AND action='auto_respond'",
                    (run_id,),
                )
            }
            rows = db.execute(
                "SELECT record_id,json_extract(data,'$.provider'),json_extract(data,'$.model'),json_extract(data,'$.provenance') FROM decision_events WHERE run_id=? AND stage IN ('sensitivity','generation','validation')",
                (run_id,),
            ).fetchall()
        for row in rows:
            if row[3] in ("Cached", "Fixture replay"):
                continue
            for value in row[1:3]:
                if not value:
                    continue
                metadata = json.loads(value, parse_float=Decimal)
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("fixture") is True
                    or metadata.get("cost_policy") != "paid_test_only"
                    or metadata.get("configured_model") != model
                ):
                    continue
                call_id = metadata.get("last_call_id")
                usage = metadata.get("usage")
                if (
                    not isinstance(call_id, str)
                    or metadata.get("last_call_status") != "success"
                    or not metadata.get("actual_model")
                ):
                    continue
                try:
                    raw = usage.get("cost") if isinstance(usage, dict) else None
                    amount = Decimal(str(raw))
                    if isinstance(raw, bool) or not amount.is_finite() or amount < 0:
                        raise ValueError()
                except (InvalidOperation, ValueError, TypeError):
                    raise RuntimeError("Smoke cost evidence unavailable") from None
                task = metadata.get("last_task")
                if task not in ("assess", "draft", "validate"):
                    task = None
                if call_id in costs and (
                    costs[call_id]["cost"] != amount or costs[call_id]["task"] != task
                ):
                    raise RuntimeError("Conflicting smoke cost evidence")
                costs[call_id] = {
                    "cost": amount,
                    "record_id": row[0],
                    "task": task,
                    "actual_model": metadata["actual_model"],
                }
        answered_records |= answered
        sample.append(
            {
                "run_id": run_id,
                "completed_records": run["processed_count"],
                "answer_count": len(answered),
            }
        )
    per_answer = [
        sum(
            (entry["cost"] for entry in costs.values() if entry["record_id"] == rid),
            Decimal(0),
        )
        for rid in answered_records
    ]
    per_answer = [value for value in per_answer if value > 0]
    if not per_answer:
        raise RuntimeError(
            "Successful paid answer with observed nonzero usage cost required before batch"
        )
    return {
        "verified": True,
        "runs": sample,
        "observed_calls": len(costs),
        "observed_total_cost_usd": str(
            sum((entry["cost"] for entry in costs.values()), Decimal(0))
        ),
        "answered_cost_sample_count": len(per_answer),
        "maximum_observed_answer_record_cost_usd": str(max(per_answer)),
        "task_cost_maxima_usd": {
            task: str(
                max(entry["cost"] for entry in costs.values() if entry["task"] == task)
            )
            for task in ("assess", "draft", "validate")
            if any(entry["task"] == task for entry in costs.values())
        },
        "task_cost_sample_counts": {
            task: sum(entry["task"] == task for entry in costs.values())
            for task in ("assess", "draft", "validate")
        },
        "configured_model": model,
        "actual_models": sorted({entry["actual_model"] for entry in costs.values()}),
        "limitations": "Selected development smoke only. Empirical costs do not guarantee batch cost, coverage, model availability or latency.",
    }


def development_workload_profile(
    settings, pipeline, input_path, records, scope="development"
):
    """Upper local workload, not a forecast of semantic clearance or live answers.

    Scope and trusted bytes are checked before parsing any provided input. Only
    normalizer-allowed sanitized text reaches the local classifier/retriever.
    No provider calls, evaluation labels, historical outcomes or customer groups
    influence the counts. Assessment is optimistically clear for route counting.
    """
    if scope != "development":
        raise RuntimeError(
            "Development input profile is forbidden for supplied validation"
        )
    if type(records) is not int or records != 500:
        raise RuntimeError(
            "Development profile requires the complete 500-record development artifact"
        )
    split_path = settings.root / "assets/models/split-manifest.json"
    split_raw = split_path.read_bytes()
    split = json.loads(split_raw)
    trusted = split.get("data_hash")
    if not isinstance(trusted, str) or len(trusted) != 64:
        raise RuntimeError("Trusted development fingerprint unavailable")
    raw = Path(input_path).read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != trusted:
        raise RuntimeError("Input does not match trusted development fingerprint")
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) != records:
        raise RuntimeError(
            "Development profile record count must match the complete trusted input"
        )
    if not pipeline.classifier.ready or not pipeline.retriever.ready:
        raise RuntimeError(
            "Ready local classifier and retriever required for workload profile"
        )
    from src.contracts import Assessment
    from src.guardrails import INJECTION, AUTHORITY
    from src.ingest import normalize
    from src.route import route, SENSITIVE

    reasons = Counter()
    assessment_skips = Counter()
    valid = candidates = 0
    for i, original in enumerate(data):
        ticket = normalize(
            original, "offline-development-workload-profile", input_index=i
        )
        if not ticket.valid:
            reasons["invalid_input"] += 1
            continue
        valid += 1
        predictive_text = ticket.subject + "\n" + ticket.body
        classification = pipeline.classifier.predict(predictive_text)
        # Mirror Graph._assess before guardrails.assess, in actual execution
        # order. Retrieval misses, other unsupported classes and cache hits do
        # not qualify for an assessment discount.
        if classification.status != "ok":
            assessment_skips["classification_unavailable"] += 1
        elif (
            classification.intent is not None
            and classification.intent.label in SENSITIVE
        ):
            assessment_skips["mandatory_sensitive_intent"] += 1
        elif INJECTION.search(predictive_text):
            assessment_skips["instruction_injection"] += 1
        elif AUTHORITY.search(predictive_text):
            assessment_skips["sensitive_authority_request"] += 1
        sources = pipeline.retriever.search(predictive_text)
        decision = route(
            ticket,
            classification,
            sources,
            Assessment(decision="clear", reasons=["optimistic offline workload bound"]),
            {"enabled": True},
            pipeline.policy,
        )
        candidates += decision.action == "auto_candidate"
        reasons.update(decision.reason_codes)
    return {
        "verified": True,
        "scope": "development",
        "record_count": len(data),
        "valid_record_count": valid,
        "upper_assess_calls": valid - sum(assessment_skips.values()),
        "local_assessment_skipped_count": sum(assessment_skips.values()),
        "local_assessment_skip_reason_counts": dict(sorted(assessment_skips.items())),
        "upper_draft_calls": candidates,
        "upper_validate_calls": candidates,
        "optimistic_local_auto_candidates": candidates,
        "local_route_reason_counts": dict(sorted(reasons.items())),
        "data_sha256": actual_hash,
        "split_manifest_sha256": hashlib.sha256(split_raw).hexdigest(),
        "classifier_artifact_id": getattr(pipeline.classifier, "metadata", {}).get(
            "artifact_id"
        ),
        "retrieval_manifest_sha256": digest(
            getattr(pipeline.retriever, "metadata", {})
        ),
        "policy_sha256": digest(pipeline.policy),
        "prediction_input": "normalized sanitized subject and body only; no labels/history/group features",
        "no_provider_calls": True,
        "limitations": "Mirror Graph._assess classification-unavailable/mandatory-SENSITIVE early exits, then literal guardrails.assess INJECTION/AUTHORITY exits. Retrieval misses and other unsupported classes still budget assessment; no cache credit. Locally eligible records budget draft and validation assuming every semantic assessment clears. Costs are empirical estimates, not guarantees. Hard ledger cap and durable handoffs remain mandatory.",
    }


def paid_reserve_estimate(
    smoke,
    records,
    validation_reserve_usd="0",
    safety_factor="2",
    development_profile=None,
):
    try:
        base = Decimal(str(smoke["maximum_observed_answer_record_cost_usd"]))
        factor = Decimal(str(safety_factor))
        future = Decimal(str(validation_reserve_usd))
        if (
            not all(v.is_finite() for v in (base, factor, future))
            or base <= 0
            or factor < 1
            or future < 0
            or type(records) is not int
            or records <= 0
        ):
            raise ValueError()
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise RuntimeError("Paid empirical reserve parameters invalid") from None
    method = "all_records_maximum_observed_answer_cost"
    if development_profile is not None:
        profile = development_profile
        try:
            counts = [
                profile[k]
                for k in (
                    "upper_assess_calls",
                    "upper_draft_calls",
                    "upper_validate_calls",
                )
            ]
            maxima = [
                Decimal(str(smoke["task_cost_maxima_usd"][k]))
                for k in ("assess", "draft", "validate")
            ]
            if (
                profile.get("scope") != "development"
                or profile.get("verified") is not True
                or profile.get("record_count") != records
                or type(profile.get("valid_record_count")) is not int
                or not 0 <= profile["valid_record_count"] <= records
                or type(profile.get("local_assessment_skipped_count", 0)) is not int
                or profile.get("local_assessment_skipped_count", 0) < 0
                or profile["valid_record_count"]
                != counts[0] + profile.get("local_assessment_skipped_count", 0)
                or any(type(n) is not int or not 0 <= n <= records for n in counts)
                or counts[1] != counts[2]
                or counts[1] > counts[0]
                or any(not n.is_finite() or n < 0 for n in maxima)
            ):
                raise ValueError()
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise RuntimeError(
                "Development workload estimate requires all three model-matched task cost maxima and valid counts"
            ) from None
        processing = (
            sum((n * cost for n, cost in zip(counts, maxima)), Decimal(0)) * factor
        )
        method = "development_local_workload_task_maxima"
    else:
        processing = base * records * factor
    return {
        "empirical_processing_reserve_usd": str(processing),
        "validation_reserve_usd": str(future),
        "estimated_reserve_usd": str(processing + future),
        "empirical_safety_factor": str(factor),
        "empirical_estimate_method": method,
        "estimate_is_guarantee": False,
        "budget_exhaustion_policy": "Continue every remaining ticket to durable handoff if budget is insufficient; report coverage/target miss. Never spend beyond durable cap or auto top up.",
    }


def build_preflight(
    settings,
    store,
    pipeline,
    records,
    scope="development",
    authorize_paid_evaluation=False,
    smoke_run_ids=(),
    validation_reserve_usd="0",
    safety_factor="2",
    development_input=None,
):
    if development_input is not None and scope != "development":
        raise RuntimeError(
            "Development input profile is forbidden for supplied validation"
        )
    paid = getattr(settings, "generation_cost_policy", "free_only") != "free_only"
    if paid and not authorize_paid_evaluation:
        raise RuntimeError("Paid evaluation requires explicit authorization")
    result = {
        "timestamp": now(),
        "scope": scope,
        "code_hash": tree_hash(settings.root),
        "configured_artifacts": configured_fingerprint(settings),
        "classifier_ready": pipeline.classifier.ready,
        "retriever_ready": pipeline.retriever.ready,
        "policy_hash": digest(pipeline.policy),
        "configured_model": pipeline.provider.model,
        "automation": store.control(),
        "record_budget": records,
        "paid_evaluation_authorized": bool(paid and authorize_paid_evaluation),
        "specification_deviation": PAID_DEVIATION if paid else None,
    }
    if development_input is not None:
        result["development_workload_profile"] = development_workload_profile(
            settings, pipeline, development_input, records, scope
        )
    frozen = settings.storage / "freeze.json"
    if frozen.is_file():
        freeze = json.loads(frozen.read_text())
        core = {k: v for k, v in freeze.items() if k != "protocol_hash"}
        if freeze.get("protocol_hash") != digest(core):
            raise RuntimeError("Freeze protocol fingerprint invalid")
        result["freeze_protocol_hash"] = freeze["protocol_hash"]
    elif scope == "supplied_validation":
        raise RuntimeError("Final supplied-validation preflight requires prior freeze")
    if paid:
        authorization = paid_authorization(store, pipeline.provider)
        result["paid_authorization"] = {
            "sha256": authorization["sha256"],
            "details": authorization["details"],
        }
        result["paid_budget"] = authorization["budget"]
        result["smoke"] = empirical_smoke(store, smoke_run_ids, pipeline.provider.model)
        result.update(
            paid_reserve_estimate(
                result["smoke"],
                records,
                validation_reserve_usd,
                safety_factor,
                result.get("development_workload_profile"),
            )
        )
    try:
        result["provider"] = pipeline.provider.preflight()
    except Exception as exc:
        result["provider"] = {"verified": False, "error_class": type(exc).__name__}
    if paid:
        required = Decimal(result["estimated_reserve_usd"])
        budget = result["paid_budget"]
        try:
            held_unknown = Decimal(str(budget.get("held_unknown_usd", "0")))
            if not held_unknown.is_finite() or held_unknown < 0:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise RuntimeError("Durable held-unknown reserve is invalid") from None
        # Provider account credit can still include a timed-out request's charge
        # until usage is reported. Cover that hold as well as future work. The
        # durable ledger's remaining amount already subtracts the hold, so its
        # cap comparison below must continue using only the future reserve.
        credit_required = required + held_unknown
        result["credit_required_reserve_usd"] = str(credit_required)
        try:
            result["credit_check"] = pipeline.provider.verify_credit(credit_required)
        except Exception:
            result["credit_check"] = {
                "verified": False,
                "sufficient": False,
                "reason": "credit_unavailable",
            }
        result["quota_sufficient"] = False
        result["quota_note"] = (
            "Paid exception, not free quota or original NFR-07 compliance."
        )
        result["paid_budget_sufficient"] = bool(
            not budget.get("blocked")
            and Decimal(str(budget["remaining_usd"])) >= required
            and result["credit_check"].get("verified")
            and result["credit_check"].get("sufficient")
            and result["provider"].get("verified")
        )
    else:
        result.update(
            assess_quota(
                result["provider"],
                records,
                pipeline.provider.max_retries,
                0,
            )
        )
    assets = bool(
        pipeline.classifier.ready
        and pipeline.retriever.ready
        and pipeline.policy.get("calibration_ready")
        and pipeline.policy.get("retrieval_ready")
    )
    if paid:
        result["paid_budget_sufficient"] = bool(
            result["paid_budget_sufficient"] and assets
        )
    else:
        result["quota_sufficient"] = bool(result["quota_sufficient"] and assets)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="storage/preflight.json")
    p.add_argument("--records", type=int, required=True)
    p.add_argument(
        "--scope", choices=["development", "supplied_validation"], default="development"
    )
    p.add_argument("--authorize-paid-evaluation", action="store_true")
    p.add_argument("--smoke-run", action="append", default=[])
    p.add_argument("--validation-reserve-usd", default="0")
    p.add_argument("--empirical-safety-factor", default="2")
    p.add_argument(
        "--development-input",
        help="Optional complete trusted development artifact for local workload budget estimate; forbidden for supplied validation",
    )
    args = p.parse_args()
    if not 0 < args.records <= 10000:
        raise SystemExit("Record budget must be 1–10000")
    if args.development_input is not None and args.scope != "development":
        raise SystemExit(
            "Development input profile is forbidden for supplied validation"
        )
    target = Path(args.output)
    if target.exists():
        raise SystemExit(
            "Preflight output exists; preserve evidence and choose a new output path"
        )
    from src.runtime import create_runtime

    settings, store, pipeline = create_runtime(isolated_index=True)
    result = build_preflight(
        settings,
        store,
        pipeline,
        args.records,
        args.scope,
        args.authorize_paid_evaluation,
        args.smoke_run,
        args.validation_reserve_usd,
        args.empirical_safety_factor,
        args.development_input,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
