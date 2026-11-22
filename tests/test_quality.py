"""Dataset isolation, original-record weighting and audit state; entirely offline."""

import json
from types import SimpleNamespace

import pytest

from scripts.train_quality import conflict_summary, expanded, load_development, views
from scripts.train import run as train
from scripts.index import run as index
from src.retrieve import Retriever


def test_unknown_bytes_rejected_before_json_decode(tmp_path):
    source = tmp_path / "renamed-development.json"
    source.write_bytes(b"not valid json")
    models = tmp_path / "models"
    models.mkdir()
    (models / "manifest.json").write_text(json.dumps({"data_hash": "0" * 64}))
    with pytest.raises(ValueError, match="fingerprint"):
        load_development(source, models)
    with pytest.raises(ValueError, match="fingerprint"):
        train(SimpleNamespace(input=source))
    (models / "split-manifest.json").write_text(json.dumps({"data_hash": "0" * 64}))
    with pytest.raises(ValueError, match="fingerprint"):
        index(SimpleNamespace(development=source, models=models))


def test_verbatim_views_exclude_sidecar_and_balance_original_records():
    row = {
        "subject": "API retry",
        "body": "How should I retry?",
        "labels": {"intent": "must not enter features"},
        "history": "hidden closure",
        "region": "excluded",
    }
    assert views(row) == [
        "API retry\nHow should I retry?",
        "API retry",
        "How should I retry?",
    ]
    xs, ys, weights = expanded(
        [row, {**row, "subject": ""}], ["rate_limit", "rate_limit"], True
    )
    assert len(xs) == 4 and weights.sum() == pytest.approx(2)
    assert all("hidden" not in x and "excluded" not in x for x in xs)
    assert set(ys) == {"rate_limit"}


def test_contradictory_identical_text_ceiling_is_label_diagnostic():
    rows = [
        {"subject": "same", "body": "text", "labels": {"urgency": x}}
        for x in ("high", "high", "low")
    ]
    result = conflict_summary(rows, "urgency")
    assert result["contradictory_text_groups"] == 1
    assert result["records_in_contradictory_groups"] == 3
    assert result["deterministic_exact_text_resubstitution_ceiling"] == pytest.approx(
        2 / 3
    )
    assert result["ceiling_is_not_unseen_accuracy"]


def test_empty_and_unavailable_retrieval_clear_previous_ticket_metadata():
    r = Retriever.__new__(Retriever)
    r.ready = True
    r.last_candidates = ["previous private ticket source"]
    r.last_query_metadata = {"old": "previous private ticket"}
    assert r.search("  ") == []
    assert r.last_candidates == [] and "old" not in r.last_query_metadata
    r.ready = False
    r.last_candidates = ["old"]
    assert r.search("real query") == [] and r.last_candidates == []
