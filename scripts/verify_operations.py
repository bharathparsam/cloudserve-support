"""Read-only checks of the local API, metrics endpoint and monitoring services."""

import argparse
import json
from pathlib import Path
import httpx
from src.ingest import now


def verify(url, metrics_url, prometheus_url, grafana_url):
    result = {
        "timestamp": now(),
        "no_ticket_submission": True,
        "no_inference": True,
        "checks": {},
    }
    with httpx.Client(timeout=15) as client:
        for name, target in {
            "api_live": url + "/health/live",
            "api_ready": url + "/health/ready",
            "api_schema": url + "/openapi.json",
            "metrics": metrics_url + "/metrics",
            "prometheus": prometheus_url + "/-/ready",
            "grafana": grafana_url + "/api/health",
        }.items():
            response = client.get(target)
            result["checks"][name] = {"status": response.status_code}
            response.raise_for_status()
        targets = client.get(prometheus_url + "/api/v1/targets")
        targets.raise_for_status()
        active = targets.json()["data"]["activeTargets"]
        result["prometheus_targets"] = [
            {"health": t["health"], "scrape_url": t["scrapeUrl"]} for t in active
        ]
        if not active or any(t["health"] != "up" for t in active):
            raise RuntimeError(
                "Prometheus target is not healthy; keep metrics running and allow one scrape"
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--metrics-url", default="http://127.0.0.1:8001")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:3000")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output)
    if target.exists():
        raise SystemExit("Output exists; preserve evidence and choose a new path")
    result = verify(args.url, args.metrics_url, args.prometheus_url, args.grafana_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
