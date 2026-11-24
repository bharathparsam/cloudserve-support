from src.config import Settings
import pytest


def test_explicit_temporary_storage_overrides_inherited_database(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////app/storage/decisions.db")
    settings = Settings(storage=tmp_path / "isolated")
    assert settings.db_path == tmp_path / "isolated/decisions.db"


def test_template_names_and_legacy_model_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_MODEL", "legacy/model:free")
    assert Settings(storage=tmp_path).model == "legacy/model:free"
    monkeypatch.setenv("MODEL_NAME", "configured/model:free")
    monkeypatch.setenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.80")
    monkeypatch.setenv("RETRIEVAL_TOP_K", "5")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    settings = Settings(storage=tmp_path)
    assert settings.model == "configured/model:free"
    assert settings.embedding_model == "all-MiniLM-L6-v2"
    assert settings.confidence_threshold == 0.8
    assert settings.retrieval_top_k == 5
    assert settings.log_level == "WARNING"


@pytest.mark.parametrize(
    "name,value",
    [
        ("CONFIDENCE_THRESHOLD", "nan"),
        ("CONFIDENCE_THRESHOLD", "1.1"),
        ("RETRIEVAL_TOP_K", "0"),
        ("LOG_LEVEL", "INVALID"),
    ],
)
def test_invalid_configuration_rejected(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings(storage=tmp_path)
