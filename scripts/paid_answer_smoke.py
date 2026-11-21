"""Explicit user-authorized paid development test through the real shared workflow.

Normal application settings remain free-only. One selected calibration ticket,
no fallback/retries, maximum total USD0.25, actual reported cost in every event.
"""

from __future__ import annotations
import argparse, json
from decimal import Decimal
from pathlib import Path
from src.runtime import create_runtime
from src.datasets import load_development
from src.ingest import normalize
from src.providers.paid_test import PaidTestProvider
from evaluation.harness import run


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--authorize-paid-test", action="store_true")
    p.add_argument(
        "--model",
        choices=["openai/gpt-4.1-mini", "openai/gpt-4o-mini"],
        default="openai/gpt-4.1-mini",
    )
    p.add_argument("--budget-usd", default="0.25")
    p.add_argument("--output", required=True)
    a = p.parse_args()
    if not a.authorize_paid_test:
        raise SystemExit(
            "Explicit --authorize-paid-test required; ordinary application remains free-only"
        )
    budget = Decimal(a.budget_usd)
    if not budget.is_finite() or not Decimal("0") < budget <= Decimal("0.25"):
        raise SystemExit(
            "Paid development-test budget must be positive and no greater than USD0.25"
        )
    output = Path(a.output)
    if output.exists():
        raise SystemExit("Preserve prior evidence; use a new output path")
    runtime = create_runtime()
    settings, store, pipeline = runtime
    split = json.loads((settings.classifier_dir / "split-manifest.json").read_text())
    index = 307
    if index not in split["calibration"]:
        raise SystemExit("Ticket must belong to development calibration")
    with store.connection() as db:
        if db.execute(
            "SELECT 1 FROM runs WHERE state IN ('queued','running') LIMIT 1"
        ).fetchone():
            raise SystemExit("A user batch is active; leave it untouched")
    normalized = normalize(
        load_development(settings)[index],
        "paid-development-test",
        "paid-development-test",
    )
    if not normalized.valid:
        raise SystemExit("Trusted development record invalid")
    provider = PaidTestProvider(
        api_key=settings.api_key,
        model=a.model,
        budget_usd=budget,
        timeout=30,
        max_retries=0,
    )
    provider.preflight()
    output.mkdir(parents=True)
    (output / "input.json").write_text(
        json.dumps(
            [
                {
                    "channel": normalized.channel,
                    "subject": normalized.subject,
                    "body": normalized.body,
                }
            ],
            indent=2,
        )
    )
    before = store.control()
    changed = False
    result = {
        "authorization": "User explicitly approved paid inference for testing and raised the cap on 2026-11-20",
        "scope": "one development calibration record; no supplied validation",
        "budget_usd": str(budget),
        "model": a.model,
        "calibration_index": index,
        "normal_operator_settings_changed": False,
        "no_customer_delivery": True,
    }
    pipeline.provider = provider
    settings.model = provider.model
    settings.generation_cost_policy = "paid_test_only"
    settings.paid_test_budget_usd = str(budget)
    try:
        if not before["enabled"]:
            store.set_control(
                True,
                "Explicit one-ticket paid development test; maximum USD" + str(budget),
                "implementation_coordinator",
            )
            changed = True
        manifest, metrics = run(
            output / "input.json",
            output / "results",
            runtime=runtime,
            kind="paid_development_test",
        )
        result.update(
            run_id=manifest["run_id"], state=manifest["state"], counts=metrics["counts"]
        )
    finally:
        if changed:
            store.set_control(
                False,
                "Paid development test ended; restore stopped state",
                "implementation_coordinator",
            )
        result["provider"] = provider.metadata
        result["control_after"] = store.control()
        (output / "paid-test-summary.json").write_text(
            json.dumps(result, indent=2, default=str)
        )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("provider", "control_after")}
        )
    )


if __name__ == "__main__":
    main()
