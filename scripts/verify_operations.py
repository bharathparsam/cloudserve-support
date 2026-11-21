"""Fixture-free local deployment verification. Never prints operator credentials."""

import argparse, json, time
from pathlib import Path
import httpx
from dotenv import dotenv_values
from src.ingest import now

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1:8000")
p.add_argument("--env-file", default=".env")
p.add_argument("--output", default="storage/operations-check.json")
a = p.parse_args()
config = dotenv_values(a.env_file)
headers = {"Authorization": "Bearer " + str(config.get("OPERATOR_TOKEN", ""))}
result = {
    "timestamp": now(),
    "base_url": a.url,
    "mode": "real local workflow; no hosted key required",
    "probes": {},
}
with httpx.Client(timeout=30) as client:
    for path in ("/health/live", "/health/ready", "/api/v1/status", "/metrics", "/"):
        r = client.get(a.url + path)
        result["probes"][path] = {"status": r.status_code}
        assert r.status_code == 200, (path, r.status_code)
        if path == "/api/v1/status":
            result["provider"] = r.json()["provider"]
            result["automation"] = r.json()["automation"]
    for service, url in [
        ("prometheus", "http://127.0.0.1:9090/-/ready"),
        ("grafana", "http://127.0.0.1:3000/api/health"),
    ]:
        r = client.get(url)
        result["probes"][service] = {"status": r.status_code}
        assert r.status_code == 200
    r = client.get("http://127.0.0.1:9090/api/v1/targets")
    active = r.json()["data"]["activeTargets"]
    result["prometheus_targets"] = [
        {"health": t["health"], "scrapeUrl": t["scrapeUrl"]} for t in active
    ]
    record = {
        "channel": "email",
        "subject": "Documented retry guidance",
        "body": "API HTTP 429 rate limits exponential backoff",
    }
    r = client.post(a.url + "/api/v1/tickets", headers=headers, json=record)
    assert r.status_code == 202, r.status_code
    ids = r.json()
    result["ticket"] = ids
    for _ in range(100):
        ticket = client.get(a.url + "/api/v1/tickets/" + ids["record_id"]).json()
        if ticket.get("outcome"):
            break
        time.sleep(0.2)
    assert ticket.get("outcome"), "Ticket did not reach a durable outcome"
    result["outcome"] = {
        "action": ticket["outcome"]["action"],
        "reason": ticket["outcome"]["reason"],
        "source_count": len(ticket["outcome"]["sources"]),
        "provenance": ticket["outcome"]["provenance"],
    }
    assert (
        ticket["outcome"]["action"] == "escalate"
    )  # generated credentials intentionally have no hosted key
    assert ticket["outcome"][
        "sources"
    ], "Real retrieval should resolve this documented question"
    for source in ticket["outcome"]["sources"]:
        resolved = client.get(a.url + "/api/v1/sources/" + source["chunk_id"])
        assert (
            resolved.status_code == 200
            and resolved.json()["content"] == source["content"]
        )
    result["sources_resolved"] = True
    for _ in range(100):
        artifact = client.get(
            a.url + "/api/v1/runs/" + ids["run_id"] + "/artifacts/manifest.json",
            headers=headers,
        )
        if artifact.status_code == 200 and artifact.json().get("state") == "completed":
            break
        time.sleep(0.2)
    assert artifact.status_code == 200, "Worker did not export completed run"
    result["export_manifest"] = artifact.json()
    assert result["export_manifest"]["state"] == "completed"
    protected = client.get(a.url + "/api/v1/tickets/" + ids["record_id"] + "/original")
    assert protected.status_code == 401
    protected = client.get(
        a.url + "/api/v1/tickets/" + ids["record_id"] + "/original", headers=headers
    )
    assert protected.status_code == 200
    result["protected_original"] = {"without_token": 401, "with_token": 200}
    result["run_metrics"] = client.get(
        a.url + "/api/v1/runs/" + ids["run_id"] + "/metrics"
    ).json()
target = Path(a.output)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(result, indent=2))
print(json.dumps({"verified": True, "evidence": str(target), "run_id": ids["run_id"]}))
