from src.config import Settings

def test_explicit_temporary_storage_overrides_inherited_database(monkeypatch,tmp_path):
    monkeypatch.setenv('DATABASE_URL','sqlite:////app/storage/decisions.db')
    settings=Settings(storage=tmp_path/'isolated')
    assert settings.db_path==tmp_path/'isolated/decisions.db'
