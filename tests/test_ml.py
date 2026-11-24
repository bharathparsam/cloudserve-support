"""Real frozen local artifacts; no hosted model/key or supplied validation."""

import json, os
from pathlib import Path
import numpy as np
import pytest
from src.classify import Classifier
from src.retrieve import Retriever, chunks, encode_queries

ROOT = Path(__file__).resolve().parents[1]


def test_missing_models_explicit_unavailable(tmp_path):
    c = Classifier(tmp_path)
    assert not c.ready and c.predict("hello").status == "unavailable"


def test_embedding_model_mismatch_does_not_open_existing_index(tmp_path):
    r = Retriever(
        ROOT / "assets/corpus/documentation.json",
        tmp_path / "chroma",
        embedding_model="different/model",
    )
    assert not r.ready
    assert r.metadata["error_type"] == "ValueError"
    assert not (tmp_path / "chroma").exists()


def test_real_classifier_vocab_probabilities_and_determinism():
    c = Classifier(ROOT / "assets/models")
    assert c.ready
    a = c.predict("How should I retry API HTTP 429 rate limits?")
    b = c.predict("How should I retry API HTTP 429 rate limits?")
    assert a == b and a.status == "ok"
    assert (
        len(a.intent.raw_probabilities) == 22 and len(a.urgency.raw_probabilities) == 3
    )
    assert sum(a.intent.raw_probabilities.values()) == pytest.approx(1)
    assert a.intent.provider_confidence is None
    assert 0 <= a.intent.calibrated_probability <= 1


def test_split_is_disjoint_and_complete():
    split = json.loads((ROOT / "assets/models/split-manifest.json").read_text())
    groups = [set(split[k]) for k in ("train", "calibration", "check")]
    assert len(set.union(*groups)) == 500
    assert (
        not groups[0] & groups[1]
        and not groups[0] & groups[2]
        and not groups[1] & groups[2]
    )


def test_corpus_passage_offsets_and_token_artifacts():
    corpus = {
        r["doc_id"]: r
        for r in json.loads((ROOT / "assets/corpus/documentation.json").read_text())
    }
    passages = json.loads((ROOT / "assets/corpus/passages.json").read_text())
    assert len(corpus) == 29
    for p in passages:
        assert corpus[p["doc_id"]]["content"][p["start"] : p["end"]] == p["content"]
    e = np.load(ROOT / "assets/corpus/passage-embeddings.npy", allow_pickle=False)
    assert e.shape == (len(passages), 384)


def test_retrieval_bootstraps_new_store_and_resolves(tmp_path, monkeypatch):
    # Downloaded cache is only needed by real query embeddings; test remains offline.
    cache = ROOT / "storage/cache/huggingface"
    if not cache.exists():
        pytest.skip("Run asset preparation for offline embedding cache")
    monkeypatch.setenv("HF_HOME", str(cache))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    r = Retriever(
        ROOT / "assets/corpus/documentation.json",
        tmp_path / "chroma",
        local_files_only=True,
    )
    assert r.ready, r.metadata
    hits = r.search("API HTTP 429 rate limits exponential backoff")
    assert 1 <= len(hits) <= 4
    assert all(s.score >= r.metadata["retrieval_floor"] for s in hits)
    assert any(s.doc_id == "DOC-API-001" for s in hits)
    for s in hits:
        resolved = r.resolve(s.chunk_id)
        assert resolved.content == s.content and resolved.corpus_hash == s.corpus_hash
    r.top_k = 5
    r.search("API HTTP 429 rate limits exponential backoff")
    assert len(r.last_candidates) == 5
    assert r.resolve("invented") is None
    assert r.search("purple unicorn zeppelin marmalade") == []
    assert (
        r.last_candidates and r.last_candidates[0].score < r.metadata["retrieval_floor"]
    )
    r.search("rate limit " * 400)
    assert (
        not r.last_query_metadata["truncated"] and r.last_query_metadata["tokens"] > 254
    )
