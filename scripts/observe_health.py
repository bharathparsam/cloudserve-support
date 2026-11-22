"""Read-only scheduled local service observation; no inference or SLA extrapolation."""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


def observe(url, seconds, output):
    target = Path(output)
    if target.exists():
        raise ValueError("Preserve prior observation; choose a new output path")
    target.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    samples = []
    with target.open("w") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "timestamp",
                "scheduled_offset_seconds",
                "actual_offset_seconds",
                "ready",
                "status_reachable",
                "probe_seconds",
                "error_class",
            ],
        )
        writer.writeheader()
        for offset in range(0, seconds, 15):
            time.sleep(max(0, start + offset - time.monotonic()))
            tick = time.monotonic()
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scheduled_offset_seconds": offset,
                "actual_offset_seconds": tick - start,
                "ready": False,
                "status_reachable": False,
                "error_class": None,
            }
            try:
                with urlopen(url + "/health/ready", timeout=5) as response:
                    row["ready"] = json.load(response).get("ready") is True
                with urlopen(url + "/api/v1/status", timeout=5) as response:
                    row["status_reachable"] = response.status == 200
            except Exception as exc:
                row["error_class"] = type(exc).__name__
            row["probe_seconds"] = time.monotonic() - tick
            writer.writerow(row)
            stream.flush()
            samples.append(row)
    result = {
        "started_at": samples[0]["timestamp"] if samples else None,
        "scheduled_window_seconds": seconds,
        "interval_seconds": 15,
        "expected_samples": len(range(0, seconds, 15)),
        "actual_samples": len(samples),
        "successful_samples": sum(
            r["ready"] and r["status_reachable"] for r in samples
        ),
        "observed_ready_rate": (
            sum(r["ready"] and r["status_reachable"] for r in samples) / len(samples)
            if samples
            else None
        ),
        "elapsed_seconds": time.monotonic() - start,
        "provider_inference_probed": False,
        "limitations": "Read-only readiness/status observation during declared local window. No new hosted calls or durable-handoff admission probes. This is not sustained production SLA or provider availability evidence.",
    }
    target.with_suffix(".json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 15 <= args.seconds <= 86400:
        raise SystemExit("Observation must be 15–86400 seconds")
    observe(args.url, args.seconds, args.output)
