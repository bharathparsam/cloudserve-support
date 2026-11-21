"""Short scheduled durable-handoff availability observation, never a production SLA proof."""

import argparse, csv, time, json
from datetime import datetime, timezone
from pathlib import Path
import httpx
from dotenv import dotenv_values

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1:8000")
p.add_argument("--seconds", type=int, default=120)
p.add_argument("--output", default="storage/availability.csv")
p.add_argument("--env-file", default=".env")
a = p.parse_args()
Path(a.output).parent.mkdir(parents=True, exist_ok=True)
config = dotenv_values(a.env_file)
headers = {"Authorization": "Bearer " + str(config.get("OPERATOR_TOKEN", ""))}
started = time.monotonic()
due = started
rows = []
with httpx.Client(timeout=5) as client, open(a.output, "w") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "timestamp",
            "available",
            "provider_live_verified",
            "probe_seconds",
            "expected_interval_seconds",
            "record_id",
            "reason",
        ]
    )
    while due - started < a.seconds:
        time.sleep(max(0, due - time.monotonic()))
        tick = time.monotonic()
        available = False
        provider = False
        record_id = ""
        reason = "probe_failed"
        try:
            status = client.get(a.url + "/api/v1/status")
            provider = bool(status.json().get("provider", {}).get("actual_model"))
            # Deterministic sensitive request avoids consuming model quota; real local persistence is exercised.
            r = client.post(
                a.url + "/api/v1/tickets",
                headers=headers,
                json={"channel": "chat", "body": "Please refund this invoice."},
            )
            if r.status_code == 202:
                record_id = r.json()["record_id"]
                for _ in range(20):
                    ticket = client.get(a.url + "/api/v1/tickets/" + record_id).json()
                    if ticket.get("outcome"):
                        available = ticket["outcome"]["action"] == "escalate" and any(
                            e["stage"] == "final" for e in ticket["events"]
                        )
                        reason = "durable_handoff" if available else "invalid_outcome"
                        break
                    time.sleep(0.1)
            else:
                reason = "admission_status_" + str(r.status_code)
        except Exception:
            reason = "probe_unavailable"
        row = [
            datetime.now(timezone.utc).isoformat(),
            available,
            provider,
            time.monotonic() - tick,
            15,
            record_id,
            reason,
        ]
        rows.append(row)
        w.writerow(row)
        f.flush()
        due += 15
summary = {
    "scheduled_window_seconds": a.seconds,
    "expected_interval_seconds": 15,
    "scheduled_probes": len(rows),
    "successful_durable_handoffs": sum(r[1] for r in rows),
    "observed_probe_success_rate": (
        sum(r[1] for r in rows) / len(rows) if rows else None
    ),
    "provider_live_inference_observed": any(r[2] for r in rows),
    "limitations": "Short local observation with synthetic deterministic handoff probes; no sustained production SLA, answer-quality or provider-availability claim.",
}
Path(a.output).with_suffix(".json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary))
