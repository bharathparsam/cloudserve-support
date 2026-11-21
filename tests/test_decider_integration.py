"""Synthetic decider observations exercise real graph/store gates, never live-quality evidence."""

import pytest
from src.contracts import Assessment, GuardrailResult
from test_integration import fixture_runtime, enable_fixture


class EvaluatorFixture:
    def __init__(
        self, input_decision="clear", retrieval_status="pass", grounding="pass"
    ):
        self.provider = type("P", (), {"metadata": {"fixture": True}})()
        self.metadata = {"fixture": True}
        self.policy_ready = True
        self.input_decision = input_decision
        self.retrieval_status = retrieval_status
        self.grounding = grounding
        self.calls = []

    def fingerprint(self):
        return {"fixture": True, "version": 1}

    def assess(self, t):
        self.calls.append("input")
        return Assessment(decision=self.input_decision, reasons=["synthetic_fixture"])

    def retrieval(self, t, s):
        self.calls.append("retrieval")
        return {
            "status": self.retrieval_status,
            "raw_answers": {"answerable": 0.2},
            "reasons": ["synthetic_fixture"],
        }

    def validate(self, *args):
        self.calls.append("validation")
        return [
            GuardrailResult(name=n, status=self.grounding)
            for n in (
                "decider_grounding",
                "decider_instruction_integrity",
                "decider_tone_scope",
            )
        ]


def process(runtime, mode, evaluator):
    store, pipeline = enable_fixture(runtime)
    pipeline.evaluator = evaluator
    pipeline.decider_mode = mode
    run = store.create_run([{"channel": "email", "body": "How do retries work?"}])
    job = store.claim_job()
    return store, pipeline.process(job["original"], run, job["record_id"], 0)


def test_shadow_cannot_authorize_or_block_existing_release(fixture_runtime):
    e = EvaluatorFixture("unknown", "unknown", "fail")
    store, out = process(fixture_runtime, "shadow", e)
    assert out.action == "auto_respond" and not out.blocked
    assert not any(c.name.startswith("decider_") for c in out.checks)
    observed = [
        x for x in store.events(run_id=out.run_id)["items"] if x["stage"] == "decider"
    ]
    assert observed and all(x["data"]["status"] == "observed" for x in observed)


def test_enforce_unknown_stops_before_drafting(fixture_runtime):
    e = EvaluatorFixture("unknown")
    store, out = process(fixture_runtime, "enforce", e)
    assert (
        out.action == "escalate" and out.answer is None and "validation" not in e.calls
    )
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM releases").fetchone()[0] == 0


def test_enforce_grounding_failure_blocks(fixture_runtime):
    store, out = process(fixture_runtime, "enforce", EvaluatorFixture(grounding="fail"))
    assert out.action == "escalate" and out.blocked and out.answer is None
    assert any(c.name == "decider_grounding" and c.status == "fail" for c in out.checks)


def test_enforce_pass_preserves_all_original_mandatory_gates(fixture_runtime):
    store, out = process(fixture_runtime, "enforce", EvaluatorFixture())
    assert out.action == "auto_respond"
    names = {c.name for c in out.checks}
    assert {
        "privacy",
        "source_resolution",
        "grounding",
        "operator_stop",
        "durable_audit",
        "decider_grounding",
        "decider_input_policy",
        "decider_retrieval_sufficiency",
    } <= names


def test_operator_stop_avoids_hosted_observation_calls(fixture_runtime):
    _, store, pipeline = fixture_runtime
    e = EvaluatorFixture()
    pipeline.evaluator = e
    pipeline.decider_mode = "shadow"
    pipeline.provider.complete = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Stopped operation cannot call hosted writer")
    )
    run = store.create_run([{"channel": "chat", "body": "How do retries work?"}])
    job = store.claim_job()
    out = pipeline.process(job["original"], run, job["record_id"], 0)
    assert out.action == "escalate" and not e.calls


def test_enforce_cannot_override_original_similarity_floor(fixture_runtime):
    fixture_runtime[2].policy["relevance_floor"] = 1.0
    _, out = process(fixture_runtime, "enforce", EvaluatorFixture())
    assert out.action == "escalate" and out.answer is None


def test_enforce_enable_rejected_without_reviewed_calibration(fixture_runtime):
    from src.api import create_app
    from fastapi.testclient import TestClient

    _, _, pipeline = fixture_runtime
    e = EvaluatorFixture()
    e.policy_ready = False
    pipeline.evaluator = e
    pipeline.decider_mode = "enforce"
    with TestClient(create_app(fixture_runtime, start_worker=False)) as client:
        response = client.put(
            "/api/v1/controls/automation",
            headers={"Authorization": "Bearer operator-test"},
            json={"enabled": True, "reason": "Synthetic test enable"},
        )
        assert response.status_code == 409
        assert client.get("/api/v1/status").json()["decider"]["policy_ready"] is False


def test_stop_during_decider_observation_prevents_old_release(fixture_runtime):
    import threading

    store, pipeline = enable_fixture(fixture_runtime)
    e = EvaluatorFixture()
    pipeline.evaluator = e
    pipeline.decider_mode = "shadow"
    entered = threading.Event()
    resume = threading.Event()
    results = []
    original = e.validate

    def delayed(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    e.validate = delayed
    run = store.create_run([{"channel": "chat", "body": "How do retries work?"}])
    job = store.claim_job()
    t = threading.Thread(
        target=lambda: results.append(
            pipeline.process(job["original"], run, job["record_id"], 0)
        )
    )
    t.start()
    assert entered.wait(5)
    store.set_control(False, "Synthetic incident stop")
    store.set_control(True, "Synthetic explicit re-enable")
    resume.set()
    t.join(5)
    assert results[0].action == "escalate" and results[0].answer is None
    assert any(
        c.name == "operator_stop" and c.status == "fail" for c in results[0].checks
    )


def test_same_version_semantic_retrieval_reuses_first_valid_observation(
    fixture_runtime,
):
    store, pipeline = enable_fixture(fixture_runtime)
    e = EvaluatorFixture()
    pipeline.evaluator = e
    pipeline.decider_mode = "enforce"
    first = store.create_run([{"channel": "email", "body": "How do retries work?"}])
    j = store.claim_job()
    a = pipeline.process(j["original"], first, j["record_id"], 0)
    e.retrieval_status = "fail"
    second = store.create_run([{"channel": "email", "body": "How do retries work?"}])
    j = store.claim_job()
    b = pipeline.process(j["original"], second, j["record_id"], 0)
    assert a.action == b.action == "auto_respond" and e.calls.count("retrieval") == 1
    observations = [
        x["data"]
        for x in store.events(run_id=second)["items"]
        if x["stage"] == "decider" and "retrieval_provenance" in x["data"]
    ]
    assert observations[0]["retrieval_provenance"] == "Cached"


def test_status_reports_actual_generation_rate_limit(fixture_runtime):
    from fastapi.testclient import TestClient
    from src.api import create_app

    fixture_runtime[2].provider.metadata = {"fixture": True, "last_http_status": 429}
    with TestClient(create_app(fixture_runtime, start_worker=False)) as c:
        status = c.get("/api/v1/status").json()
        assert status["status"] == "degraded" and any(
            "rate limited" in issue for issue in status["issues"]
        )


def test_cached_retrieval_preserves_its_original_provider_evidence(fixture_runtime):
    store, pipeline = enable_fixture(fixture_runtime)
    e = EvaluatorFixture()
    pipeline.evaluator = e
    pipeline.decider_mode = "shadow"
    original = e.retrieval

    def observed(*args):
        value = original(*args)
        e.provider.metadata["last_call_id"] = "synthetic-original-retrieval"
        return value

    e.retrieval = observed
    run = store.create_run([{"channel": "chat", "body": "How do retries work?"}])
    j = store.claim_job()
    pipeline.process(j["original"], run, j["record_id"], 0)
    e.provider.metadata["last_call_id"] = "synthetic-unrelated-later-call"
    run = store.create_run([{"channel": "chat", "body": "How do retries work?"}])
    j = store.claim_job()
    pipeline.process(j["original"], run, j["record_id"], 0)
    observed = [
        x["data"]
        for x in store.events(run_id=run)["items"]
        if x["stage"] == "decider" and "retrieval_provenance" in x["data"]
    ]
    assert observed[0]["retrieval_provenance"] == "Cached"
    assert observed[0]["provider"]["last_call_id"] == "synthetic-original-retrieval"
