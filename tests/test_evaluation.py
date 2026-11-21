import csv, json
from pathlib import Path
import pytest
from src.metrics import calculate
from evaluation.harness import (
    enrich_metrics,
    calibration_bands,
    tree_hash,
    run,
    configured_fingerprint,
    final_gate,
)
from evaluation.review import summarize
from scripts.preflight import assess_quota


def test_empty_null_rates_and_overlap():
    m = calculate([], 0)
    assert m["business"]["auto_answer_rate"] is None and m["governance"]["reconciled"]
    outcomes = [
        {
            "record_id": "a",
            "action": "escalate",
            "blocked": True,
            "timings": {"processing_seconds": 10},
            "checks": [],
        },
        {
            "record_id": "b",
            "action": "auto_respond",
            "blocked": False,
            "timings": {"processing_seconds": 30},
            "checks": [],
        },
    ]
    m = calculate(outcomes, 2)
    assert (
        m["counts"]["processed"]
        == m["counts"]["answered"] + m["counts"]["escalated"]
        == 2
    )
    assert (
        m["business"]["confirmed_fcr"] is None
        and m["business"]["replay_substantive_reply_mean_seconds"] == 30
    )
    assert m["technical"]["processing_p95_seconds"] == 29
    assert m["governance"]["released_privacy_incidents"] is None


def test_duplicate_terminal_positions_fail_reconciliation():
    row = {"record_id": "same", "action": "escalate", "blocked": False, "timings": {}}
    assert not calculate([row, row], 2)["governance"]["reconciled"]


def test_unprocessed_abstentions_stay_in_metric_denominators(tmp_path):
    records = [
        {
            "labels": {
                "intent": "rate_limit",
                "urgency": "low",
                "answerable_from_docs": True,
                "expected_doc_ids": ["doc"],
                "must_not_auto_respond": False,
            }
        },
        {
            "labels": {
                "intent": "security_incident",
                "urgency": "high",
                "answerable_from_docs": False,
                "must_not_auto_respond": True,
            }
        },
    ]
    outcomes = [
        {
            "record_id": "r1",
            "input_index": 0,
            "action": "escalate",
            "blocked": False,
            "classification": {
                "status": "unavailable",
                "intent": None,
                "urgency": None,
            },
            "sources": [],
            "reason": "classifier unavailable",
        }
    ]
    metrics = calculate(outcomes, 2)
    enrich_metrics(metrics, outcomes, records, tmp_path)
    report = metrics["technical"]["classification"]["intent"]
    assert (
        report["labelled_count"] == 2 and report["abstention_or_unprocessed_count"] == 2
    )
    assert sum(sum(r) for r in report["confusion_matrix"]) == 2
    assert metrics["technical"]["retrieval_proxy"]["answerable_denominator"] == 1
    assert (
        metrics["governance"]["prohibited_auto_answers"]["unprocessed_labelled_records"]
        == 1
    )


def test_probability_one_in_final_band_and_raw_distinct():
    pairs = [("a", "a", {"calibrated_probability": 1.0, "raw_probability": 0.4})]
    assert calibration_bands(pairs, "calibrated_probability")[-1]["count"] == 1
    assert calibration_bands(pairs, "raw_probability")[4]["count"] == 1


def test_no_money_balance_as_quota():
    q = assess_quota(
        {"verified": True, "quota": {"is_free_tier": False, "limit_remaining": 1000}},
        80,
    )
    assert (
        q["estimated_max_calls"] == 720
        and not q["quota_sufficient"]
        and q["remaining_free_requests"] is None
    )
    assert assess_quota(
        {
            "verified": True,
            "quota": {
                "remaining_free_requests": 1000,
                "free_request_allowance_verified": True,
            },
        },
        80,
    )["quota_sufficient"]


def test_blank_review_not_fabricated(tmp_path):
    p = tmp_path / "reviews.csv"
    p.write_text(
        "record_id,reviewer,support_1_5,correctness_1_5,clarity_1_5,uncertainty_1_5\nr1,,,,,\n"
    )
    m = summarize(p)
    assert m["reviewed"] == 0 and m["quality_mean"] is None


def test_final_gate_before_opening_input(tmp_path):
    from src.config import Settings
    from src.logging_store import Store

    class Provider:
        model = "fixture"

    class Pipeline:
        provider = Provider()

    settings = Settings(storage=tmp_path)
    store = Store(tmp_path / "db")
    with pytest.raises(RuntimeError, match="frozen artifacts"):
        run(
            tmp_path / "DO_NOT_OPEN.json",
            tmp_path / "out",
            runtime=(settings, store, Pipeline()),
            kind="supplied_validation",
        )
    assert not (tmp_path / "out").exists()


def test_existing_output_evidence_not_overwritten(tmp_path):
    p = tmp_path / "out"
    p.mkdir()
    (p / "manifest.json").write_text("preserve")
    with pytest.raises(ValueError):
        run(tmp_path / "missing", p)
    assert (p / "manifest.json").read_text() == "preserve"


def test_tree_hash_includes_artifacts_and_dependencies(tmp_path):
    (tmp_path / "assets/models").mkdir(parents=True)
    asset = tmp_path / "assets/models/model.joblib"
    asset.write_bytes(b"one")
    before = tree_hash(tmp_path)
    asset.write_bytes(b"two")
    assert tree_hash(tmp_path) != before
    before = tree_hash(tmp_path)
    (tmp_path / "requirements.txt").write_text("version==1")
    assert tree_hash(tmp_path) != before


def local_runtime(tmp_path):
    from src.config import Settings
    from src.logging_store import Store
    from src.graph import PipelineService
    from src.contracts import Classification

    class C:
        metadata = {"fixture": True}
        ready = False

        def predict(self, text):
            return Classification(status="unavailable", reason="fixture unavailability")

    class R:
        metadata = {"fixture": True}
        ready = False

        def search(self, text):
            return []

    class P:
        metadata = {"fixture": True}
        model = "fixture"
        api_key = "fixture"

        def preflight(self):
            return {"verified": True, "fixture": True}

        def complete(self, *a, **k):
            raise TimeoutError()

    s = Settings(storage=tmp_path / "storage")
    store = Store(s.db_path)
    return s, store, PipelineService(store, C(), R(), P(), {})


def test_audit_failure_retains_incomplete_evidence_and_unlocks(tmp_path):
    runtime = local_runtime(tmp_path)
    s, store, p = runtime
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON decision_events BEGIN SELECT RAISE(ABORT,'audit fails'); END"
        )
    input_path = tmp_path / "development-renamed.json"
    input_path.write_text('[{"channel":"chat","body":"how?"}]')
    with pytest.raises(Exception):
        run(input_path, tmp_path / "out", runtime=runtime)
    manifest = json.loads((tmp_path / "out/manifest.json").read_text())
    assert (
        manifest["state"] == "incomplete"
        and manifest["reconciliation"]["processed"] == 0
    )
    assert (tmp_path / "out/outcomes.jsonl").read_text() == ""
    assert not (s.storage / "evaluation.lock").exists()
    assert store.get_run(manifest["run_id"])["state"] == "incomplete"


def test_final_fixture_clears_prior_assessment_cache(tmp_path):
    from src.ingest import now

    s, store, p = local_runtime(tmp_path)
    store.set_control(True, "Test final gating only")
    p.classifier.ready = True
    p.retriever.ready = True
    p.policy = {"calibration_ready": True, "retrieval_ready": True}
    frozen = {
        "code_hash": tree_hash(s.root),
        "configured_model": "fixture",
        "configured_artifacts": configured_fingerprint(s),
    }
    (s.storage / "freeze.json").write_text(json.dumps(frozen))
    checked = {
        "code_hash": frozen["code_hash"],
        "configured_artifacts": frozen["configured_artifacts"],
        "configured_model": "fixture",
        "provider": {"verified": True},
        "quota_sufficient": True,
        "timestamp": now(),
        "record_budget": 1,
    }
    (s.storage / "preflight.json").write_text(json.dumps(checked))
    store.cache_put("old-development", {"decision": "clear"})
    path = tmp_path / "synthetic-test.json"
    path.write_text('[{"channel":"chat","body":"how?"}]')
    manifest, _ = run(
        path, tmp_path / "out", runtime=(s, store, p), kind="supplied_validation"
    )
    assert manifest["state"] == "completed" and manifest["cache_initial_entries"] == 0
    assert store.cache_get("old-development") is None
    assert manifest["provider"]["fixture"] is True


def test_configured_fingerprint_changes_for_external_model_corpus_policy_and_params(
    tmp_path, monkeypatch
):
    from src.config import Settings

    external = tmp_path / "external"
    external.mkdir()
    modeldir = external / "models"
    modeldir.mkdir()
    for name in ("manifest.json", "intent.joblib", "urgency.joblib"):
        (modeldir / name).write_bytes(b"{}")
    corpus = external / "documentation.json"
    corpus.write_text("[]")
    policy = external / "routing.json"
    policy.write_text("{}")
    for name in ("passages.json", "index-manifest.json"):
        (external / name).write_text("{}")
    (external / "passage-embeddings.npy").write_bytes(b"embedding-v1")
    monkeypatch.setenv("CLASSIFIER_DIR", str(modeldir))
    monkeypatch.setenv("CORPUS_PATH", str(corpus))
    monkeypatch.setenv("POLICY_PATH", str(policy))
    settings = Settings(root=tmp_path / "root", storage=tmp_path / "storage")
    fingerprint = configured_fingerprint(settings)
    assert "external:" in fingerprint["details"]["files"]["corpus"]["path_reference"]
    assert str(external) not in json.dumps(fingerprint)
    for path in (
        modeldir / "intent.joblib",
        corpus,
        policy,
        external / "passage-embeddings.npy",
    ):
        prior = configured_fingerprint(settings)["sha256"]
        original = path.read_bytes()
        path.write_bytes(original + b" changed")
        assert configured_fingerprint(settings)["sha256"] != prior
        path.write_bytes(original)
    before = configured_fingerprint(settings)["sha256"]
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    assert configured_fingerprint(settings)["sha256"] != before


def test_configured_fingerprint_tracks_actual_local_embedding_snapshot(
    tmp_path, monkeypatch
):
    from src.config import Settings

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "documentation.json").write_text("[]")
    (corpus / "index-manifest.json").write_text(
        '{"embedding_revision":"frozen-revision"}'
    )
    hub = tmp_path / "hub"
    snapshot = (
        hub
        / "models--sentence-transformers--all-MiniLM-L6-v2/snapshots/frozen-revision"
    )
    snapshot.mkdir(parents=True)
    weights = snapshot / "model.safetensors"
    weights.write_bytes(b"original")
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    monkeypatch.setenv("CORPUS_PATH", str(corpus / "documentation.json"))
    settings = Settings(root=tmp_path / "root", storage=tmp_path / "storage")
    before = configured_fingerprint(settings)
    assert before["details"]["embedding"]["snapshot_available"]
    weights.write_bytes(b"changed")
    assert configured_fingerprint(settings)["sha256"] != before["sha256"]


def test_common_metrics_are_pure_and_match_export_enrichment(tmp_path):
    from src.metrics import compute_metrics

    records = [
        {
            "channel": "chat",
            "body": "hello",
            "customer_name": "PRIVATE PERSON",
            "labels": {
                "intent": "rate_limit",
                "urgency": "low",
                "answerable_from_docs": True,
                "expected_doc_ids": ["doc"],
            },
            "customer_tier": "standard",
        }
    ]
    outcomes = [
        {
            "record_id": "r1",
            "input_index": 0,
            "action": "escalate",
            "blocked": False,
            "classification": {
                "status": "unavailable",
                "intent": None,
                "urgency": None,
            },
            "sources": [],
            "reason": "unavailable",
            "timings": {},
        }
    ]
    records_before = json.dumps(records)
    outcomes_before = json.dumps(outcomes)
    pure = compute_metrics(outcomes, records, events=[], body_boundary=10)
    exported = calculate(outcomes, 1)
    enrich_metrics(exported, outcomes, records, tmp_path, body_boundary=10)
    assert (
        pure["technical"] == exported["technical"]
        and pure["governance"]["segments"] == exported["governance"]["segments"]
    )
    assert "PRIVATE PERSON" not in json.dumps(pure)
    assert (
        json.dumps(records) == records_before
        and json.dumps(outcomes) == outcomes_before
    )


def test_final_gate_rejects_changed_overridden_policy_before_input(
    tmp_path, monkeypatch
):
    from src.ingest import now

    policy = tmp_path / "external-routing.json"
    policy.write_text("{}")
    monkeypatch.setenv("POLICY_PATH", str(policy))
    settings, store, p = local_runtime(tmp_path)
    store.set_control(True, "Synthetic final gate test")
    frozen = {
        "code_hash": tree_hash(settings.root),
        "configured_model": "fixture",
        "configured_artifacts": configured_fingerprint(settings),
    }
    checked = {
        "code_hash": frozen["code_hash"],
        "configured_artifacts": frozen["configured_artifacts"],
        "configured_model": "fixture",
        "provider": {"verified": True},
        "quota_sufficient": True,
        "timestamp": now(),
        "record_budget": 1,
    }
    (settings.storage / "freeze.json").write_text(json.dumps(frozen))
    (settings.storage / "preflight.json").write_text(json.dumps(checked))
    policy.write_text('{"changed":true}')
    with pytest.raises(RuntimeError, match="Configured runtime assets"):
        run(
            tmp_path / "DO_NOT_OPEN.json",
            tmp_path / "out",
            runtime=(settings, store, p),
            kind="supplied_validation",
        )
    assert not (tmp_path / "out").exists()
