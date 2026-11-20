"""Exports consume persisted fixture outcomes; they are never live inference."""
import json,time
import pytest
from src.config import Settings
from src.logging_store import Store
from src.graph import PipelineService
from src.contracts import Classification
from evaluation.export import export_run

class Classifier:
    ready=False;metadata={'fixture':True}
    def predict(self,text):return Classification(status='unavailable',reason='fixture')
class Retriever:
    ready=False;metadata={'fixture':True}
    def search(self,text):return []
class Provider:
    api_key='';model='fixture';metadata={'fixture':True}
    def complete(self,*a,**kw):raise AssertionError('No provider call expected for deterministic sensitive example')

def prepared(tmp_path,records=None):
    settings=Settings(storage=tmp_path/'storage');store=Store(settings.db_path)
    pipeline=PipelineService(store,Classifier(),Retriever(),Provider(),{})
    run_id=store.create_run(records if records is not None else [{'channel':'chat','body':'Please refund our invoice.','customer_name':'PRIVATE PERSON','labels':{'intent':'billing_query'}}],kind='development_upload')
    while job:=store.claim_job(run_id=run_id):pipeline.process(job['original'],run_id,job['record_id'],job['input_index'])
    return settings,store,pipeline,run_id

def test_persisted_export_has_real_state_timing_safe_data_and_no_reprocessing(tmp_path):
    settings,store,pipeline,run_id=prepared(tmp_path)
    before=store.get_run(run_id)
    pipeline.process=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Must not reprocess'))
    result=export_run(settings,store,pipeline,run_id)
    target=settings.storage/'evaluation'/run_id
    assert result['state']=='completed'
    assert result['started_at']==before['started_at'] and result['finished_at']==before['completed_at']
    assert result['elapsed_seconds']>=0 and result['processing_code_hash'] is None
    assert 'PRIVATE PERSON' not in ''.join(p.read_text() for p in target.iterdir() if p.is_file())
    assert result['provenance_counts']['Fixture replay']==1
    metrics=json.loads((target/'metrics.json').read_text());assert metrics['technical']['classification']['intent']['labelled_count']==1
    assert metrics['business']['confirmed_fcr'] is None
    assert (target/'availability.csv').read_text().count('\n')==1
    assert len((target/'outcomes.jsonl').read_text().splitlines())==1
    assert store.get_run(run_id)['state']=='completed'
    with store.connection() as db:assert db.execute('SELECT COUNT(*) FROM artifact_manifests WHERE run_id=?',(run_id,)).fetchone()[0]>=10

def test_export_never_overwrites_existing_evidence(tmp_path):
    settings,store,pipeline,run_id=prepared(tmp_path);export_run(settings,store,pipeline,run_id)
    target=settings.storage/'evaluation'/run_id/'manifest.json';original=target.read_text()
    with pytest.raises(FileExistsError):export_run(settings,store,pipeline,run_id)
    assert target.read_text()==original and store.get_run(run_id)['state']=='completed'

def test_export_failure_keeps_partial_manifest_and_marks_incomplete(tmp_path,monkeypatch):
    settings,store,pipeline,run_id=prepared(tmp_path)
    def fail(*a,**kw):raise OSError('simulated export device unavailable')
    monkeypatch.setattr('evaluation.export.write_reviews',fail)
    with pytest.raises(OSError):export_run(settings,store,pipeline,run_id)
    result=json.loads((settings.storage/'evaluation'/run_id/'manifest.json').read_text())
    assert result['state']=='incomplete' and result['export_error_class']=='OSError'
    assert store.get_run(run_id)['state']=='incomplete'
    assert store.list_tickets(run_id=run_id)['items'][0]['outcome']['action']=='escalate'

def test_empty_export_null_duration_and_rates(tmp_path):
    settings,store,pipeline,run_id=prepared(tmp_path,records=[])
    result=export_run(settings,store,pipeline,run_id)
    metrics=json.loads((settings.storage/'evaluation'/run_id/'metrics.json').read_text())
    assert result['state']=='completed' and result['elapsed_seconds'] is None
    assert metrics['business']['auto_answer_rate'] is None

def test_export_does_not_touch_active_run(tmp_path):
    settings=Settings(storage=tmp_path/'storage');store=Store(settings.db_path);run_id=store.create_run([{}])
    with pytest.raises(ValueError,match='still active'):export_run(settings,store,None,run_id)
    assert store.get_run(run_id)['state']=='queued'
