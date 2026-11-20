"""All tests isolate runtime environment; injected audit failures must never reach app storage."""
import pytest

@pytest.fixture(autouse=True)
def isolated_runtime_environment(monkeypatch):
    for name in ('DATABASE_URL','STORAGE_PATH','CHROMA_PATH','CORPUS_PATH','CLASSIFIER_DIR','POLICY_PATH'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('OPENROUTER_API_KEY','')
    monkeypatch.setenv('OPERATOR_TOKEN','test-operator-only')
