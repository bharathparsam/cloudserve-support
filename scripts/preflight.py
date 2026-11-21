"""Explicit live free-provider preflight; dollar balance is not free-request quota."""

import argparse, json
from pathlib import Path
from datetime import datetime, timezone
from src.runtime import create_runtime
from src.ingest import now, digest
from evaluation.harness import tree_hash, configured_fingerprint


def assess_quota(provider_metadata, records, max_retries=2, extra_calls=0):
    # Conservative upper bound: sensitivity + draft + check, each with bounded retries.
    calls = records * (3 + extra_calls) * (1 + max_retries)
    quota = provider_metadata.get("quota", {})
    remaining = quota.get("remaining_free_requests")
    evidenced = (
        isinstance(remaining, int)
        and not isinstance(remaining, bool)
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
        "quota_note": "Only explicitly evidenced remaining free request allowance counts. Monetary limit_remaining/usage_daily and is_free_tier do not establish it. No paid capacity or shrinking input is allowed.",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="storage/preflight.json")
    p.add_argument("--records", type=int, default=0)
    args = p.parse_args()
    if args.records < 0 or args.records > 10000:
        raise SystemExit("Record budget must be 0–10000")
    target = Path(args.output)
    if target.exists():
        raise SystemExit(
            "Preflight output exists; preserve evidence and choose a new output path"
        )
    settings, store, pipeline = create_runtime()
    result = {
        "timestamp": now(),
        "code_hash": tree_hash(settings.root),
        "configured_artifacts": configured_fingerprint(settings),
        "classifier_ready": pipeline.classifier.ready,
        "retriever_ready": pipeline.retriever.ready,
        "policy_hash": digest(pipeline.policy),
        "configured_model": pipeline.provider.model,
        "automation": store.control(),
    }
    try:
        result["provider"] = pipeline.provider.preflight()
    except Exception as e:
        result["provider"] = {"verified": False, "error_class": type(e).__name__}
    extra = (
        3
        if pipeline.evaluator is not None and pipeline.decider_mode != "disabled"
        else 0
    )
    result["decider"] = {
        "mode": pipeline.decider_mode,
        "policy_ready": bool(pipeline.evaluator and pipeline.evaluator.policy_ready),
    }
    if pipeline.evaluator:
        try:
            result["decider"]["provider"] = pipeline.evaluator.provider.preflight()
        except Exception as e:
            result["decider"]["provider"] = {
                "verified": False,
                "error_class": type(e).__name__,
            }
    result.update(
        assess_quota(
            result["provider"],
            args.records,
            pipeline.provider.max_retries,
            extra_calls=extra,
        )
    )
    if pipeline.decider_mode == "enforce" and not result["decider"]["policy_ready"]:
        result["quota_sufficient"] = False
    result["quota_sufficient"] = bool(
        result["quota_sufficient"]
        and result["classifier_ready"]
        and result["retriever_ready"]
        and pipeline.policy.get("calibration_ready")
        and pipeline.policy.get("retrieval_ready")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
