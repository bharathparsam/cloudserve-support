"""Synthetic adapter fixtures verify benchmark accounting, never live model quality."""
import hashlib
import json
from pathlib import Path
import pytest
from src.config import Settings
from src.contracts import SourcePassage
from evaluation.decider_benchmark import run, trusted_calibration


def setup(tmp_path, key=''):
    root=tmp_path/'app';models=root/'assets/models';models.mkdir(parents=True)
    data=[{'channel':'email','body':'TRAIN MUST NOT QUERY','labels':{'answerable_from_docs':True,'expected_doc_ids':['TRAIN']}},
          {'ticket_id':'DEV-PRIVATE','channel':'chat','body':'How do retries work, Alice Jones?','customer_name':'Alice Jones','labels':{'answerable_from_docs':True,'expected_doc_ids':['DOC-A']}},
          {'channel':'forum','body':'CHECK MUST NOT QUERY','labels':{'answerable_from_docs':False,'expected_doc_ids':[]}},
          {'channel':'email','body':'Novel issue','labels':{'answerable_from_docs':False,'expected_doc_ids':[]}}]
    raw=json.dumps(data).encode();source=tmp_path/'renamed-development.json';source.write_bytes(raw)
    data_hash=hashlib.sha256(raw).hexdigest();split={'data_hash':data_hash,'train':[0],'calibration':[1,3],'check':[2]}
    splitraw=json.dumps(split).encode();(models/'split-manifest.json').write_bytes(splitraw)
    (models/'manifest.json').write_text(json.dumps({'data_hash':data_hash,'split_manifest_hash':hashlib.sha256(splitraw).hexdigest()}))
    return Settings(root=root,storage=tmp_path/'storage',api_key=key),source

class Retriever:
    ready=True;metadata={'fixture':True,'corpus_hash':'fixture'}
    passage=SourcePassage(doc_id='DOC-A',chunk_id='chunk-a',title='Retry',content='Use backoff.',start=0,end=12,corpus_hash='fixture',score=.7)
    def __init__(self):self.queries=[];self.last_candidates=[]
    def search(self,text,k=4):
        self.queries.append(text);self.last_candidates=[self.passage]
        return [self.passage] if 'retries' in text else []
class Provider:
    model='fixture:free';max_retries=2
    def __init__(self,quota=None,fail=False):self.metadata={'fixture':True};self.quota=quota or {};self.fail=fail
    def preflight(self):
        if self.fail:raise RuntimeError('Synthetic preflight failure')
        return {'verified':True,'fixture':True,'quota':self.quota}
class Evaluator:
    def __init__(self,provider=None,quota_error=False):self.provider=provider or Provider();self.calls=[];self.quota_error=quota_error
    def fingerprint(self):return {'fixture':True,'thresholds':'unmeasured'}
    def retrieval(self,ticket,sources):
        self.calls.append(ticket)
        self.provider.metadata.update(last_call_id='fixture-'+str(len(self.calls)),actual_model='fixture:free',last_http_status=429 if self.quota_error else 200)
        if self.quota_error:return {'status':'unknown','passages':[],'raw_answers':{}}
        return {'status':'unknown','answerability_probability':.8,'passages':[{'doc_id':s.doc_id,'chunk_id':s.chunk_id,'probability':.9,'status':'unknown'} for s in sources], 'raw_answers':{'sufficiency':{'type':'noul','noul':.8}}}


def test_unknown_source_rejected_before_json_parse(tmp_path):
    settings,source=setup(tmp_path)
    source.write_text('this is deliberately not JSON; never parse an unknown source')
    with pytest.raises(ValueError,match='fingerprint'):
        trusted_calibration(source,settings)

def test_selects_calibration_only_and_labels_fixture_scores(tmp_path):
    settings,source=setup(tmp_path,key='synthetic-test-key');r=Retriever();e=Evaluator(Provider(quota={'remaining_free_requests':100,'free_request_allowance_verified':True}))
    manifest,summary=run(source,tmp_path/'out',limit=2,settings=settings,retriever=r,evaluator=e)
    assert manifest['state']=='completed' and manifest['selection_split']=='development calibration only'
    assert manifest['provenance']=='Fixture replay' and not manifest['live_inference_observed']
    rows=[json.loads(x) for x in (tmp_path/'out/results.jsonl').read_text().splitlines()]
    assert [x['input_index'] for x in rows]==[1,3]
    assert all('TRAIN' not in q and 'CHECK' not in q for q in r.queries)
    exported=(tmp_path/'out/results.jsonl').read_text()
    assert 'Alice Jones' not in exported and 'DEV-PRIVATE' not in exported
    assert all('labels' not in t.model_dump() and 'history' not in t.model_dump() for t in e.calls)
    assert summary['decider']['unknown_decisions']==2 and summary['decider']['decision_coverage']==0
    assert summary['baseline']['raw_expected_doc_hit_at_4']==1
    assert summary['baseline']['no_support_specificity_proxy']==1
    assert summary['decider']['semantic_grounding_accuracy'] is None
    assert not summary['thresholds_selected']

def test_missing_key_blocks_before_heavy_retrieval_and_accounts_selection(tmp_path):
    settings,source=setup(tmp_path)
    manifest,summary=run(source,tmp_path/'out',limit=1,settings=settings)
    assert manifest['state']=='blocked' and manifest['blocker']=='local_openrouter_key_missing'
    assert manifest['selected_count']==1 and summary['records_accounted']==1
    assert summary['baseline']['raw_expected_doc_hit_at_4'] is None
    assert not manifest['live_inference_observed']

def test_full_unknown_quota_blocked_but_explicit_limit_is_partial_smoke(tmp_path):
    settings,source=setup(tmp_path,key='synthetic-test-key');e=Evaluator();r=Retriever()
    manifest,_=run(source,tmp_path/'full',settings=settings,retriever=r,evaluator=e)
    assert manifest['state']=='blocked' and 'quota' in manifest['blocker']
    assert e.calls==[] and r.queries==[]
    explicit,_=run(source,tmp_path/'whole-with-limit',limit=2,settings=settings,retriever=r,evaluator=e)
    assert explicit['state']=='blocked' and not explicit['partial']
    manifest,_=run(source,tmp_path/'partial',limit=1,settings=settings,retriever=r,evaluator=e)
    assert manifest['state']=='completed' and manifest['partial'] and not manifest['quota_sufficient']
    assert len(e.calls)==1

def test_first_quota_error_prevents_further_requests_but_retains_all_rows(tmp_path):
    settings,source=setup(tmp_path,key='synthetic-test-key');e=Evaluator(Provider(quota={'remaining_free_requests':100,'free_request_allowance_verified':True}),quota_error=True);r=Retriever()
    manifest,summary=run(source,tmp_path/'out',limit=2,settings=settings,retriever=r,evaluator=e)
    assert manifest['state']=='completed_with_unavailable_results'
    assert len(e.calls)==1 and len(r.queries)==2 and summary['records_accounted']==2
    rows=[json.loads(x) for x in (tmp_path/'out/results.jsonl').read_text().splitlines()]
    assert rows[1]['reason']=='quota_exhausted_remaining_records_not_called'
    assert summary['errors_or_unavailable']==2

def test_preflight_failure_does_not_query_or_infer(tmp_path):
    settings,source=setup(tmp_path,key='synthetic-test-key');e=Evaluator(Provider(fail=True));r=Retriever()
    manifest,_=run(source,tmp_path/'out',limit=1,settings=settings,retriever=r,evaluator=e)
    assert manifest['state']=='blocked' and e.calls==[] and r.queries==[]

def test_existing_evidence_and_overlap_are_rejected(tmp_path):
    settings,source=setup(tmp_path);out=tmp_path/'out';out.mkdir();(out/'keep.txt').write_text('retain')
    with pytest.raises(ValueError,match='empty'):
        run(source,out,settings=settings)
    assert (out/'keep.txt').read_text()=='retain'
    model=settings.classifier_dir
    split={'data_hash':hashlib.sha256(source.read_bytes()).hexdigest(),'train':[0,1],'calibration':[1],'check':[2]}
    raw=json.dumps(split).encode();(model/'split-manifest.json').write_bytes(raw)
    (model/'manifest.json').write_text(json.dumps({'data_hash':split['data_hash'],'split_manifest_hash':hashlib.sha256(raw).hexdigest()}))
    with pytest.raises(ValueError,match='overlaps'):trusted_calibration(source,settings)


def test_preflight_fixture_marker_cannot_be_exported_as_live(tmp_path):
    settings,source=setup(tmp_path,key='synthetic-test-key');provider=Provider();provider.metadata={};e=Evaluator(provider)
    manifest,summary=run(source,tmp_path/'out',limit=1,settings=settings,retriever=Retriever(),evaluator=e)
    assert manifest['provenance']=='Fixture replay' and not manifest['live_inference_observed']
    assert summary['live_inference_records']==0
