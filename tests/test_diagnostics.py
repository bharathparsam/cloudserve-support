from types import SimpleNamespace
from src.contracts import Assessment, RouteDecision
from src.diagnostics import provider_failure, explain_route
from test_integration import fixture_runtime


def test_low_confidence_explains_both_independent_floors():
    route = RouteDecision(
        action="escalate",
        reason_codes=["confidence_below_floor"],
        reason="confidence below floor",
        policy_hash="fixture",
        thresholds={"intent": 0.201231, "retrieval": 0.588827},
    )
    classification = SimpleNamespace(
        intent=SimpleNamespace(calibrated_probability=0.116279)
    )
    result = explain_route(
        route,
        classification,
        Assessment(decision="clear", reasons=[]),
        [SimpleNamespace(score=0.458563)],
        None,
    )
    assert result.action == "escalate" and result.reason_codes == route.reason_codes
    assert (
        "0.116" in result.reason
        and "0.201" in result.reason
        and "0.459" in result.reason
        and "0.589" in result.reason
    )


def test_rate_limit_is_provider_unavailable_not_customer_sensitive():
    p = SimpleNamespace(metadata={"last_http_status": 429})
    route = RouteDecision(
        action="escalate",
        reason_codes=["sensitivity_unknown"],
        reason="sensitivity unknown",
        policy_hash="fixture",
    )
    result = explain_route(
        route,
        None,
        Assessment(decision="unknown", reasons=["provider_unavailable"]),
        [],
        p,
    )
    assert "rate limited" in result.reason and "HTTP 429" in result.reason
    assert "sensitive request" not in result.reason


def test_bad_evidence_does_not_reuse_prior_provider_error():
    p = SimpleNamespace(metadata={"last_http_status": 429})
    route = RouteDecision(
        action="escalate",
        reason_codes=["sensitivity_unknown"],
        reason="unknown",
        policy_hash="fixture",
    )
    result = explain_route(
        route,
        None,
        Assessment(decision="unknown", reasons=["assessment_evidence_not_in_input"]),
        [],
        p,
    )
    assert "429" not in result.reason and "could not be confirmed" in result.reason


def test_provider_response_body_never_becomes_explanation():
    p = SimpleNamespace(
        metadata={
            "last_http_status": 200,
            "last_error": "invalid_schema",
            "raw_error": "PRIVATE BODY",
        }
    )
    assert "structured response" in provider_failure(p, "Answer generation")
    assert "PRIVATE" not in provider_failure(p, "Answer generation")


def test_cached_assessment_retains_its_call_evidence_and_record_scoped_metadata(
    fixture_runtime,
):
    from test_integration import enable_fixture

    store, pipeline = enable_fixture(fixture_runtime)
    pipeline.policy["confidence_threshold"] = 1.1
    pipeline.provider.metadata = {
        "fixture": True,
        "last_call_id": "original-assessment",
        "last_call_status": "success",
    }
    original = {"channel": "chat", "body": "How should I retry a request?"}

    def process():
        run = store.create_run([original])
        job = store.claim_job()
        return pipeline.process(job["original"], run, job["record_id"], 0)

    first = process()
    pipeline.provider.metadata = {
        "fixture": True,
        "last_call_id": "unrelated-failed-call",
        "last_call_status": "failed",
    }
    pipeline.provider.complete = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Expected cached assessment")
    )
    second = process()
    assert second.versions["model"]["last_call_id"] == "original-assessment"
    assert second.versions["model"]["last_call_status"] == "success"
    assert first.route.action == second.route.action == "escalate"
    from src.metrics import calculate

    assert calculate([second.model_dump()], 1)["counts"]["provider_failure_proxy"] == 0


def test_structured_provider_failure_is_counted_even_when_reason_has_no_keyword():
    from src.metrics import calculate

    outcome = {
        "action": "escalate",
        "blocked": False,
        "reason": "Sensitivity assessment unavailable: model reached its output token limit.",
        "versions": {"model": {"last_call_status": "failed"}},
        "checks": [],
    }
    assert calculate([outcome], 1)["counts"]["provider_failure_proxy"] == 1


def test_paid_development_exception_cannot_open_supplied_validation(
    fixture_runtime, tmp_path, monkeypatch
):
    import pytest
    from evaluation.harness import run

    fixture_runtime[0].generation_cost_policy = "paid_test_only"
    monkeypatch.setattr(
        "evaluation.harness.load_records",
        lambda path: (_ for _ in ()).throw(
            AssertionError("Protected input must not be opened")
        ),
    )
    with pytest.raises(RuntimeError, match="only to development"):
        run(
            tmp_path / "unread-protected-input.json",
            tmp_path / "unused-results",
            runtime=fixture_runtime,
            kind="supplied_validation",
        )
