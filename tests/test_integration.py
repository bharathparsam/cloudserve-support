"""Orchestration tests use explicitly synthetic fixture providers, never live evidence."""

import json, threading, time
from pathlib import Path
import pytest
from src.contracts import *
from src.ingest import normalize


class FixtureClassifier:
    ready = True
    metadata = {"fixture": True}

    def predict(self, text):
        d = Dimension(
            label="rate_limit",
            raw_probabilities={"rate_limit": 1.0},
            raw_probability=1,
            calibrated_probability=1,
        )
        u = Dimension(
            label="low",
            raw_probabilities={"low": 1.0},
            raw_probability=1,
            calibrated_probability=1,
        )
        return Classification(
            status="ok",
            intent=d,
            urgency=u,
            artifact_id="test",
            calibration_status="measured",
        )


class FixtureRetriever:
    ready = True
    metadata = {"fixture": True, "corpus_hash": "test"}
    passage = SourcePassage(
        doc_id="DOC-TEST",
        chunk_id="chunk-test",
        title="Rate limits",
        content="Use exponential backoff when receiving HTTP 429.",
        start=0,
        end=55,
        corpus_hash="test",
        score=0.95,
    )

    def search(self, text, k=4):
        return [self.passage]

    def resolve(self, chunk_id):
        return self.passage if chunk_id == "chunk-test" else None


class FixtureProvider:
    api_key = "fixture"
    model = "fixture-free"
    metadata = {"fixture": True}

    def preflight(self):
        return {"verified": True, "fixture": True}

    def complete(self, task, payload, schema, max_tokens=500):
        if schema is Assessment:
            return Assessment(decision="clear", reasons=["Published how-to question"])
        if schema is AnswerDraft:
            return AnswerDraft(
                status="draft",
                automation_disclosure="This is an automated response.",
                claims=[
                    Claim(
                        claim_id="c1",
                        text="Use exponential backoff when receiving HTTP 429.",
                        citations=[Citation(doc_id="DOC-TEST", chunk_id="chunk-test")],
                    )
                ],
            )
        if schema is SemanticCheck:
            return SemanticCheck(
                claims=[GuardrailResult(name="c1", status="pass", claim_ids=["c1"])],
                instruction_integrity="pass",
                tone_scope="pass",
                answer_relevance="pass",
                evidence=[
                    SourceEvidence(
                        claim_id="c1",
                        doc_id="DOC-TEST",
                        chunk_id="chunk-test",
                        quote="Use exponential backoff when receiving HTTP 429.",
                    )
                ],
            )
        raise ValueError("Unexpected schema")


def test_four_channels_and_sidecar_isolation():
    for ch in ("email", "chat", "docs_comment", "forum"):
        raw = {
            "channel": ch,
            "body": "How do retries work?",
            "labels": {"intent": "security_incident"},
            "history": {"escalated": True},
            "customer_tier": "enterprise",
        }
        a = normalize(raw, "run", "record")
        raw.update(
            labels={"intent": "rate_limit"}, history={}, customer_tier="standard"
        )
        b = normalize(raw, "run", "record")
        assert a.body == b.body and a.subject == b.subject and a.valid
        assert "labels" not in a.model_dump() and "history" not in a.model_dump()


def test_malformed_records_not_coerced():
    for raw in (
        None,
        3,
        [],
        {},
        {"channel": "fax", "body": "x"},
        {"channel": "chat", "body": "  "},
    ):
        assert not normalize(raw, "run", "record").valid


@pytest.fixture
def fixture_runtime(tmp_path):
    from src.config import Settings
    from src.logging_store import Store
    from src.graph import PipelineService

    settings = Settings(storage=tmp_path, operator_token="operator-test")
    store = Store(tmp_path / "decisions.db")
    policy = {
        "version": "test",
        "eligible_intents": ["rate_limit"],
        "intent_threshold": 0.5,
        "urgency_threshold": 0.5,
        "relevance_floor": 0.5,
        "calibration_available": True,
    }
    pipeline = PipelineService(
        store, FixtureClassifier(), FixtureRetriever(), FixtureProvider(), policy
    )
    return settings, store, pipeline


def test_shared_harness_duplicate_malformed_and_empty(fixture_runtime, tmp_path):
    from evaluation.harness import run

    records = [
        {"ticket_id": "same", "channel": "chat", "body": "How do I retry 429?"},
        {"ticket_id": "same", "channel": "email", "body": "How do I retry 429?"},
        None,
    ]
    path = tmp_path / "renamed-unseen-size.json"
    path.write_text(json.dumps(records))
    manifest, metrics = run(path, tmp_path / "out", runtime=fixture_runtime)
    assert manifest["state"] == "completed"
    assert metrics["counts"]["processed"] == 3
    out = [
        json.loads(x)
        for x in (tmp_path / "out/outcomes.jsonl").read_text().splitlines()
    ]
    assert len({x["record_id"] for x in out}) == 3
    assert all(x["action"] == "escalate" for x in out)  # default stopped
    assert all(x["closed"] is None for x in out)
    path.write_text("[]")
    m, v = run(path, tmp_path / "empty", runtime=fixture_runtime)
    assert v["business"]["auto_answer_rate"] is None and m["state"] == "completed"


def test_metrics_null_and_overlap():
    from src.metrics import calculate

    x = calculate([{"action": "escalate", "blocked": True, "timings": {}}], 1)
    assert x["counts"]["processed"] == 1 and x["counts"]["blocked"] == 1
    assert x["business"]["confirmed_fcr"] is None and x["governance"]["reconciled"]


def test_api_auth_invalid_input_and_routes(fixture_runtime):
    from fastapi.testclient import TestClient
    from src.api import create_app

    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        assert c.get("/health/live").status_code == 200
        assert (
            c.post(
                "/api/v1/tickets", json={"channel": "email", "body": "hello"}
            ).status_code
            == 401
        )
        headers = {"Authorization": "Bearer operator-test"}
        assert (
            c.post(
                "/api/v1/tickets", headers=headers, json={"channel": "fax", "body": "x"}
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/v1/tickets",
                headers=headers,
                json={"channel": "email", "body": "   "},
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/v1/tickets",
                headers=headers,
                json={"channel": "email", "body": "hello", "received_at": "invalid"},
            ).status_code
            == 422
        )
        result = c.post(
            "/api/v1/tickets",
            headers=headers,
            json={"channel": "email", "body": "How do retries work?"},
        )
        assert result.status_code == 202, result.text
        rid = result.json()["record_id"]
        assert c.get("/api/v1/tickets/" + rid + "/original").status_code == 401
        assert c.get("/api/no-such-route").status_code == 404
        assert (
            c.put(
                "/api/v1/controls/automation",
                headers={**headers, "Origin": "http://evil.invalid"},
                json={"enabled": False, "reason": "test stop"},
            ).status_code
            == 403
        )
        assert c.get("/metrics").status_code == 200


def enable_fixture(runtime):
    settings, store, pipeline = runtime
    pipeline.policy.update(
        calibration_ready=True, retrieval_ready=True, retrieval_floor=0.5
    )
    store.set_control(True, "Fixture-only test enable")
    return store, pipeline


def test_graph_commits_only_validated_answer(fixture_runtime):
    store, pipeline = enable_fixture(fixture_runtime)
    run = store.create_run(
        [{"channel": "email", "body": "How should I retry HTTP 429?"}]
    )
    job = store.claim_job()
    out = pipeline.process(job["original"], run, job["record_id"], 0)
    assert out.action == "auto_respond" and "[DOC-TEST/chunk-test]" in out.answer
    assert all(x.status == "pass" for x in out.checks)
    assert {e["stage"] for e in store.events(run_id=run)["items"]} >= {
        "ingest",
        "classification",
        "retrieval",
        "sensitivity",
        "routing",
        "generation",
        "validation",
        "final",
    }


def test_delayed_graph_stop_then_reenable_cannot_release(fixture_runtime):
    store, pipeline = enable_fixture(fixture_runtime)
    entered = threading.Event()
    resume = threading.Event()
    provider = pipeline.provider
    original_complete = provider.complete

    def delayed(task, *args, **kwargs):
        if task == "draft":
            entered.set()
            assert resume.wait(5)
        return original_complete(task, *args, **kwargs)

    provider.complete = delayed
    run = store.create_run(
        [{"channel": "chat", "body": "How should I retry HTTP 429?"}]
    )
    job = store.claim_job()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            pipeline.process(job["original"], run, job["record_id"], 0)
        )
    )
    thread.start()
    assert entered.wait(5)
    store.set_control(False, "Incident stop")
    store.set_control(True, "Explicit re-enable")
    resume.set()
    thread.join(5)
    assert results[0].action == "escalate" and results[0].answer is None
    assert any(
        c.name == "operator_stop" and c.status == "fail" for c in results[0].checks
    )


def test_provider_outage_batch_continues(fixture_runtime, tmp_path):
    from evaluation.harness import run

    store, pipeline = enable_fixture(fixture_runtime)

    def down(*args, **kwargs):
        raise TimeoutError("Synthetic outage")

    pipeline.provider.complete = down
    path = tmp_path / "outage.json"
    path.write_text(
        json.dumps(
            [
                {"channel": c, "body": "How do retries work?"}
                for c in ("email", "chat", "docs_comment", "forum")
            ]
        )
    )
    manifest, metrics = run(path, tmp_path / "outage-evidence", runtime=fixture_runtime)
    assert manifest["state"] == "completed" and metrics["counts"]["escalated"] == 4


def test_known_identifiers_block_outbound_and_fixture_provenance(fixture_runtime):
    store, pipeline = enable_fixture(fixture_runtime)
    original = pipeline.provider.complete

    def leak(task, *args, **kwargs):
        response = original(task, *args, **kwargs)
        if task == "draft":
            response.claims[0].text = "Alice Jones should use exponential backoff."
        return response

    pipeline.provider.complete = leak
    run = store.create_run(
        [
            {
                "channel": "email",
                "body": "How do I retry 429?",
                "customer_name": "Alice Jones",
            }
        ]
    )
    job = store.claim_job()
    out = pipeline.process(job["original"], run, job["record_id"], 0)
    assert (
        out.action == "escalate"
        and out.answer is None
        and out.provenance == "Fixture replay"
    )
    assert any(c.name == "privacy" and c.status == "fail" for c in out.checks)
    assert "Alice Jones" not in out.model_dump_json()


def test_handoff_preserves_only_reported_troubleshooting():
    from src.handoff import build

    t = normalize(
        {
            "channel": "email",
            "body": "I already tried restarting the CLI. It still fails.",
            "history": {"first_contact_resolution": True},
        },
        "run",
        "record",
    )
    h = build(
        t, Classification(status="unavailable", reason="test"), [], "Needs operator"
    )
    assert "I already tried restarting the CLI." in h.troubleshooting
    assert "not independently verified" in h.troubleshooting
    assert "resolved" not in h.troubleshooting


def test_cli_can_process_while_api_worker_is_idle(fixture_runtime, tmp_path):
    from src.jobs import JobRunner
    from evaluation.harness import run

    settings, store, pipeline = fixture_runtime
    pipeline.storage_path = settings.storage
    pipeline.settings = settings
    runner = JobRunner(store, pipeline)
    runner.start()
    try:
        time.sleep(0.05)
        path = tmp_path / "idle-worker-input.json"
        path.write_text('[{"channel":"chat","body":"How do retries work?"}]')
        manifest, metrics = run(
            path, tmp_path / "idle-worker-output", runtime=fixture_runtime
        )
        assert manifest["state"] == "completed" and metrics["counts"]["processed"] == 1
    finally:
        runner.stop()


def test_provider_final_error_metrics_use_persisted_status(fixture_runtime):
    from src.metrics import prometheus_text

    _, store, _ = fixture_runtime
    run = store.create_run([{"channel": "chat", "body": "question"}])
    job = store.claim_job()
    store.event(
        job["record_id"],
        run,
        "sensitivity",
        {
            "provider": {
                "last_call_id": "synthetic-call",
                "last_http_status": 429,
                "last_retry_count": 2,
            }
        },
    )
    result = prometheus_text(store)
    assert 'cloudserve_provider_errors{class="429"} 1' in result
    assert "cloudserve_provider_retries 2" in result


def test_local_safety_assessment_never_records_an_idle_paid_call(fixture_runtime):
    store, pipeline = enable_fixture(fixture_runtime)
    pipeline.provider.metadata = {
        "fixture": True,
        "cost_policy": "paid_test_only",
        "request_attempts": 0,
        "last_call_id": None,
        "usage": None,
    }
    original = {"channel": "chat", "body": "Please refund this invoice."}
    run_id = store.create_run([original])
    job = store.claim_job(run_id=run_id)
    result = pipeline.process(original, run_id, job["record_id"], 0)
    assert result.action == "escalate"
    event = next(
        e
        for e in store.events(run_id=run_id, limit=100)["items"]
        if e["stage"] == "sensitivity"
    )
    assert event["data"]["provider"] == {
        "cost_policy": "not_applicable",
        "status": "not_called",
        "reason": "deterministic_local_assessment",
        "request_attempts": 0,
    }


def test_mandatory_sensitive_class_never_spends_on_unnecessary_clearance(
    fixture_runtime,
):
    store, pipeline = enable_fixture(fixture_runtime)

    class SensitiveClassifier(FixtureClassifier):
        def predict(self, text):
            result = super().predict(text)
            return result.model_copy(
                update={
                    "intent": Dimension(
                        label="security_incident",
                        raw_probabilities={"security_incident": 1.0},
                        raw_probability=1.0,
                        calibrated_probability=1.0,
                    )
                }
            )

    class MustNotCall(FixtureProvider):
        def complete(self, *args, **kwargs):
            raise AssertionError("A mandatory handoff needs no hosted model")

    pipeline.classifier = SensitiveClassifier()
    pipeline.provider = MustNotCall()
    original = {
        "channel": "email",
        "body": "A security concern needs specialist investigation.",
    }
    rid = store.create_run([original])
    job = store.claim_job(run_id=rid)
    outcome = pipeline.process(original, rid, job["record_id"], 0)
    assert outcome.action == "escalate"
    sensitivity = next(
        e
        for e in store.events(run_id=rid, limit=100)["items"]
        if e["stage"] == "sensitivity"
    )
    assert sensitivity["data"]["reason"] == "mandatory_sensitive_intent"
    assert sensitivity["data"]["provider"]["cost_policy"] == "not_applicable"
