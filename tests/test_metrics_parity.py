"""Isolated fixture evidence for API/export boundary and monitoring semantics."""

import json
from fastapi.testclient import TestClient
from test_integration import fixture_runtime
from src.api import create_app
from src.metrics import provider_observations
from evaluation.export import export_run


def test_api_export_use_identical_frozen_length_groups(fixture_runtime, tmp_path):
    settings, store, pipeline = fixture_runtime
    (settings.storage / "freeze.json").write_text(
        '{"short_long_body_character_boundary":20}'
    )
    records = [
        {"channel": "chat", "body": "short question"},
        {
            "channel": "email",
            "body": "This synthetic question has more than twenty characters.",
        },
    ]
    rid = store.create_run(records)
    while job := store.claim_job(run_id=rid):
        pipeline.process(job["original"], rid, job["record_id"], job["input_index"])
    export_run(settings, store, pipeline, rid)
    saved = json.loads(
        (settings.storage / "evaluation" / rid / "metrics.json").read_text()
    )
    with TestClient(create_app(fixture_runtime, start_worker=False)) as client:
        response = client.get("/api/v1/runs/" + rid + "/metrics")
        assert response.status_code == 200
        observed = response.json()
    assert observed["governance"]["segments"] == saved["governance"]["segments"]
    groups = observed["governance"]["segments"]["groups"]["body_length"]
    assert groups["short"]["input_count"] == 1 and groups["long"]["input_count"] == 1
    assert observed["technical"] == saved["technical"]


def test_provider_observation_does_not_fabricate_health_from_cache_fixture_or_preflight():
    def event(stage, metadata, provenance=None, time="2026-11-22T00:00:00+00:00"):
        return {
            "stage": stage,
            "timestamp": time,
            "data": {"provider": metadata, "provenance": provenance},
        }

    success = {
        "last_call_id": "a",
        "last_call_status": "success",
        "actual_model": "example/model",
        "last_http_status": 200,
    }
    assert provider_observations([event("sensitivity", success, "Cached")]) == {}
    assert (
        provider_observations([event("sensitivity", {**success, "fixture": True})])
        == {}
    )
    refusal = {
        "last_call_id": "b",
        "last_call_status": "failed",
        "last_error": "paid_budget_reservation_rejected",
        "request_attempts": 0,
    }
    assert provider_observations([event("sensitivity", refusal)]) == {}
    result = provider_observations(
        [
            event("generation", success),
            event(
                "decider",
                {
                    "last_call_id": "c",
                    "last_call_status": "failed",
                    "last_http_status": 429,
                },
                time="2026-11-22T00:00:01+00:00",
            ),
        ]
    )
    assert result["generation"]["success"] and not result["decider"]["success"]
    assert (
        result["decider"]["timestamp_seconds"]
        > result["generation"]["timestamp_seconds"]
    )


def test_prometheus_unknown_provider_is_nan_and_labels_no_customer_data(
    fixture_runtime,
):
    from src.metrics import prometheus_text

    text = prometheus_text(fixture_runtime[1])
    assert 'cloudserve_provider_last_call_observed{role="generation"} 0' in text
    assert 'cloudserve_provider_last_call_success{role="generation"} NaN' in text
    assert "not current availability or SLA" in text
    assert "record_id=" not in text and "customer_id=" not in text


def test_repeated_provider_snapshot_cannot_refresh_an_old_inference_timestamp():
    meta = {
        "last_call_id": "one-call",
        "last_call_status": "success",
        "last_http_status": 200,
        "actual_model": "example/model",
    }
    events = [
        {
            "stage": "sensitivity",
            "timestamp": "2026-11-22T00:00:00+00:00",
            "data": {"provider": meta},
        },
        {
            "stage": "validation",
            "timestamp": "2026-11-22T01:00:00+00:00",
            "data": {"provider": meta},
        },
    ]
    observed = provider_observations(events)
    from datetime import datetime

    assert (
        observed["generation"]["timestamp_seconds"]
        == datetime.fromisoformat("2026-11-22T00:00:00+00:00").timestamp()
    )
