"""CLI/API process ownership keeps embedded index files apart with identical assets."""

from types import SimpleNamespace
from src.config import Settings
from src.runtime import create_runtime


def test_standalone_runtime_uses_unique_native_index_and_same_frozen_corpus(
    tmp_path, monkeypatch
):
    opened = []
    monkeypatch.setattr(
        "src.runtime.Classifier", lambda path: SimpleNamespace(ready=True, metadata={})
    )

    def retrieve(corpus, index, **kwargs):
        assert kwargs == {"embedding_model": "all-MiniLM-L6-v2", "top_k": 5}
        opened.append((corpus, index))
        return SimpleNamespace(ready=True, metadata={})

    monkeypatch.setattr("src.runtime.Retriever", retrieve)
    monkeypatch.setattr(
        "src.runtime.build_providers", lambda s, v: (SimpleNamespace(metadata={}), None)
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "api-index"))
    monkeypatch.setenv("RETRIEVAL_TOP_K", "5")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.80")
    for isolated in (False, True, True):
        _, _, pipeline = create_runtime(
            Settings(storage=tmp_path), isolated_index=isolated
        )
        assert pipeline.policy["confidence_threshold"] == 0.8
    assert opened[0][1] == tmp_path / "api-index"
    assert len({str(p[1]) for p in opened}) == 3
    assert opened[1][1].parent == opened[2][1].parent == tmp_path / "harness-index"
    assert len({str(p[0]) for p in opened}) == 1
