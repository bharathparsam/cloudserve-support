"""Explicit operator acknowledgement of fully reserved UNKNOWN timeout charges.

Default OFF. Never settles unknown cost, releases funds, increases the cap,
changes customer outcomes or removes audit barriers. No provider requests.
"""

import argparse, json
from pathlib import Path
from src.ingest import digest
from filelock import FileLock, Timeout
from src.config import Settings
from src.logging_store import Store
from src.paid_budget import BudgetLedger, BudgetUnavailable


def read_legacy_freeze(path, expected_code_hash):
    try:
        frozen = json.loads(Path(path).read_text())
        core = {k: v for k, v in frozen.items() if k != "protocol_hash"}
        if (
            not expected_code_hash
            or frozen.get("code_hash") != expected_code_hash
            or frozen.get("protocol_hash") != digest(core)
        ):
            raise ValueError()
        fingerprint = frozen["configured_artifacts"]
        if fingerprint.get("sha256") != digest(fingerprint["details"]):
            raise ValueError()
        generation = fingerprint["details"]["parameters"]["generation"]
        for field in (
            "assessment_max_tokens",
            "draft_max_tokens",
            "validation_max_tokens",
        ):
            value = generation[field]
            if type(value) is not int or not 0 < value <= 32768:
                raise ValueError()
        return frozen
    except (OSError, ValueError, KeyError, TypeError):
        raise BudgetUnavailable(
            "Legacy freeze protocol, code hash or explicit task token limits unavailable"
        ) from None


def acknowledge(
    settings,
    request_ids,
    reason,
    acknowledge_unknown_cost=False,
    legacy_code_hash=None,
    legacy_freeze_path=None,
):
    if acknowledge_unknown_cost is not True:
        raise BudgetUnavailable("Explicit unknown-cost acknowledgement required")
    lock = FileLock(str(settings.storage / "pipeline.lock"))
    try:
        with lock.acquire(timeout=0):
            legacy_freeze = (
                read_legacy_freeze(
                    legacy_freeze_path or settings.storage / "freeze.json",
                    legacy_code_hash,
                )
                if legacy_code_hash is not None or legacy_freeze_path is not None
                else None
            )
            ledger = BudgetLedger(Store(settings.db_path))
            snapshot = ledger.acknowledge_timeout_policy(
                request_ids, reason, legacy_code_hash, legacy_freeze
            )
            return {
                "timeout_policy": ledger.authorization().get(
                    "timeout_accounting_policy"
                ),
                "budget": snapshot,
                "notice": "Actual timeout cost remains UNKNOWN. Full reservation retained; known spend is a lower bound. Total cap unchanged. No timed-out answer released.",
            }
    except Timeout:
        raise BudgetUnavailable("Pipeline is busy; acknowledgement refused") from None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--request-id", action="append", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--acknowledge-unknown-cost", action="store_true")
    p.add_argument("--legacy-code-hash")
    p.add_argument(
        "--legacy-freeze",
        help="Historical frozen protocol with exact code hash and task token limits; default storage/freeze.json",
    )
    a = p.parse_args()
    try:
        result = acknowledge(
            Settings(),
            a.request_id,
            a.reason,
            a.acknowledge_unknown_cost,
            a.legacy_code_hash,
            a.legacy_freeze,
        )
    except BudgetUnavailable as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
