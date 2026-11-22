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

    def retrieve(corpus, index):
        opened.append((corpus, index))
        return SimpleNamespace(ready=True, metadata={})

    monkeypatch.setattr("src.runtime.Retriever", retrieve)
    monkeypatch.setattr(
        "src.runtime.build_providers", lambda s, v: (SimpleNamespace(metadata={}), None)
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "api-index"))
    for isolated in (False, True, True):
        create_runtime(Settings(storage=tmp_path), isolated_index=isolated)
    assert opened[0][1] == tmp_path / "api-index"
    assert len({str(p[1]) for p in opened}) == 3
    assert opened[1][1].parent == opened[2][1].parent == tmp_path / "harness-index"
    assert len({str(p[0]) for p in opened}) == 1
