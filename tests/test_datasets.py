"""Synthetic fixed-source fixtures; never reads supplied validation or real keys."""
import hashlib
import json
from pathlib import Path
import pytest
from src.config import Settings
from src.contracts import TicketInput
from src.datasets import DatasetUnavailable, list_datasets, load_development, ticket_template


def prepared(tmp_path, monkeypatch, records=None):
    monkeypatch.delenv('DEVELOPMENT_INPUT_DIR', raising=False)
    root=tmp_path/'app';source=root/'data/input/development_tickets.json';source.parent.mkdir(parents=True)
    models=root/'assets/models';models.mkdir(parents=True)
    records=records if records is not None else [{'ticket_id':f'SYNTHETIC-{i}','channel':'chat','body':f'Synthetic development question {i}.','customer_name':'PRIVATE FIXTURE NAME','labels':{'intent':'onboarding'},'history':{'first_contact_resolution':True}} for i in range(500)]
    raw=json.dumps(records).encode();source.write_bytes(raw)
    (models/'manifest.json').write_text(json.dumps({'data_hash':hashlib.sha256(raw).hexdigest()}))
    return Settings(root=root,storage=tmp_path/'storage'),source


def test_fixed_dataset_metadata_contains_no_source_content(tmp_path,monkeypatch):
    settings,source=prepared(tmp_path,monkeypatch)
    result=list_datasets(settings);item=result['items'][0]
    assert item['id']=='development' and item['record_count']==500 and item['available'] is True
    assert result['template_url']=='/api/v1/datasets/template'
    encoded=json.dumps(result)
    for private in ('PRIVATE FIXTURE NAME','SYNTHETIC-0','Synthetic development question 0.',str(source)):
        assert private not in encoded


def test_all_or_first_records_preserve_protected_sidecars(tmp_path,monkeypatch):
    settings,_=prepared(tmp_path,monkeypatch)
    records=load_development(settings)
    assert len(records)==500 and records[0]['labels']=={'intent':'onboarding'}
    assert records[0]['history']['first_contact_resolution'] is True
    subset=load_development(settings,2)
    assert [r['ticket_id'] for r in subset]==['SYNTHETIC-0','SYNTHETIC-1']
    subset[0]['body']='mutated local copy'
    assert load_development(settings,1)[0]['body']=='Synthetic development question 0.'


@pytest.mark.parametrize('limit',[True,False,0,-1,501,1.0,'2','../validation',{},[]])
def test_invalid_limits_rejected_before_loading(tmp_path,monkeypatch,limit):
    settings,source=prepared(tmp_path,monkeypatch);source.unlink()
    with pytest.raises(ValueError,match='integer'):
        load_development(settings,limit)


def test_wrong_fingerprint_rejected_before_source_json_parse(tmp_path,monkeypatch):
    settings,source=prepared(tmp_path,monkeypatch);source.write_text('not authorized JSON and must not be parsed')
    with pytest.raises(DatasetUnavailable,match='fingerprint'):
        load_development(settings)
    assert list_datasets(settings)['items'][0]['available'] is False


def test_missing_and_bad_manifest_are_unavailable(tmp_path,monkeypatch):
    settings,source=prepared(tmp_path,monkeypatch);source.unlink()
    assert not list_datasets(settings)['items'][0]['available']
    with pytest.raises(DatasetUnavailable):load_development(settings)
    (settings.classifier_dir/'manifest.json').write_text('{bad manifest')
    assert not list_datasets(settings)['items'][0]['available']


def test_only_fixed_file_is_considered_no_directory_listing(tmp_path,monkeypatch):
    settings,source=prepared(tmp_path,monkeypatch)
    # A poison file proves no code opens/list-scans other input files.
    poison=source.with_name('validation_tickets.json');poison.write_text('DO NOT READ')
    original=Path.open
    def guarded(path,*args,**kwargs):
        assert path.name!='validation_tickets.json','Validation must never be opened'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',guarded)
    monkeypatch.setattr(Path,'iterdir',lambda _:(_ for _ in ()).throw(AssertionError('No directory enumeration')))
    assert len(load_development(settings,1))==1 and list_datasets(settings)['items'][0]['available']


def test_exclusive_server_directory_override_uses_fixed_filename(tmp_path,monkeypatch):
    settings,source=prepared(tmp_path,monkeypatch)
    override=tmp_path/'mounted-input';override.mkdir()
    monkeypatch.setenv('DEVELOPMENT_INPUT_DIR',str(override))
    assert not list_datasets(settings)['items'][0]['available'] # no fallback to stale host file
    target=override/'development_tickets.json';target.write_bytes(source.read_bytes())
    assert len(load_development(settings,1))==1
    target.write_text('changed')
    assert not list_datasets(settings)['items'][0]['available']


def test_symlink_and_wrong_record_count_rejected(tmp_path,monkeypatch):
    settings,source=prepared(tmp_path,monkeypatch)
    other=tmp_path/'renamed-source.json';source.rename(other);source.symlink_to(other)
    assert not list_datasets(settings)['items'][0]['available']
    with pytest.raises(DatasetUnavailable,match='regular'):load_development(settings)
    source.unlink();raw=b'[]';source.write_bytes(raw)
    (settings.classifier_dir/'manifest.json').write_text(json.dumps({'data_hash':hashlib.sha256(raw).hexdigest()}))
    with pytest.raises(DatasetUnavailable,match='500'):load_development(settings)


def test_template_is_independent_valid_four_channel_array():
    template=ticket_template()
    assert {TicketInput.model_validate(r).channel for r in template}=={'email','chat','docs_comment','forum'}
    assert all('labels' not in r and 'history' not in r and 'customer_name' not in r for r in template)
    template[0]['body']='changed'
    assert ticket_template()[0]['body']!='changed'
