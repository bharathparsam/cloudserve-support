"""Reports snapshot real stored decisions without providers or state changes."""
import io
import json
import zipfile
from fastapi.testclient import TestClient
from src.api import create_app
from src.reports import save_run_report, save_all_reports
from test_integration import fixture_runtime, enable_fixture

HEADERS={'Authorization':'Bearer operator-test'}


def complete(runtime):
    s, store, pipeline = runtime
    enable_fixture(runtime)
    rid=store.create_run([{'channel':'chat','body':'How should I retry 429?'}])
    job=store.claim_job(run_id=rid)
    pipeline.process(job['original'],rid,job['record_id'],job['input_index'])
    return rid


def test_final_report_is_saved_reusable_and_does_not_call_provider(fixture_runtime):
    s,store,p=fixture_runtime
    rid=complete(fixture_runtime)
    p.provider.complete=lambda *a,**k: (_ for _ in ()).throw(AssertionError('No provider calls'))
    before=store.get_run(rid)
    path=save_run_report(s,store,rid)
    first_bytes=path.read_bytes()
    assert path==save_run_report(s,store,rid)
    assert path.read_bytes()==first_bytes and store.get_run(rid)==before
    with zipfile.ZipFile(path) as z:
        assert {'manifest.json','report.md','metrics.json','metrics.md','outcomes.jsonl','decisions.jsonl','classification.csv','errors.json'}<=set(z.namelist())
        manifest=json.loads(z.read('manifest.json'))
        assert manifest['report_kind']=='final' and manifest['included_outcomes']==1
        assert 'source_code' not in z.namelist() and '.env' not in z.namelist()
        assert all(not n.startswith('/') and '..' not in n.split('/') for n in z.namelist())
        assert b'operator-test' not in b''.join(z.read(n) for n in z.namelist())


def test_progress_snapshot_does_not_finish_or_modify_run(fixture_runtime):
    s,store,p=fixture_runtime
    rid=store.create_run([{'channel':'chat','body':'First'}, {'channel':'chat','body':'Second'}])
    before=store.get_run(rid)
    first=save_run_report(s,store,rid)
    m=json.loads(first.with_name('manifest.json').read_text())
    assert m['report_kind']=='in_progress_snapshot' and m['pending_records']==2
    assert store.get_run(rid)==before
    store.fail_run(rid,'Synthetic interruption',state='interrupted')
    second=save_run_report(s,store,rid)
    assert first!=second and first.is_file()
    assert json.loads(second.with_name('manifest.json').read_text())['report_kind']=='incomplete'


def test_history_zip_contains_old_current_and_explicit_errors(fixture_runtime,monkeypatch):
    s,store,p=fixture_runtime
    old=complete(fixture_runtime)
    active=store.create_run([{'channel':'forum','body':'Still pending'}])
    archive=save_all_reports(s,store)
    with zipfile.ZipFile(archive) as z:
        index=json.loads(z.read('index.json'))
        assert index['complete_export'] and index['exported_count']==2
        assert {r['report_kind'] for r in index['reports']}=={'final','in_progress_snapshot'}
        assert f'{old}/report.zip' in z.namelist() and f'{active}/report.zip' in z.namelist()
    import src.reports as module
    original=module.save_run_report
    def fail_one(settings,db,rid):
        if rid==active:raise OSError('Synthetic archive failure')
        return original(settings,db,rid)
    monkeypatch.setattr(module,'save_run_report',fail_one)
    with zipfile.ZipFile(save_all_reports(s,store)) as z:
        index=json.loads(z.read('index.json'))
        assert not index['complete_export'] and index['exported_count']==1
        assert index['errors']==[{'run_id':active,'error_type':'OSError'}]


def test_report_endpoints_auth_snapshot_and_archive(fixture_runtime):
    s,store,p=fixture_runtime
    rid=store.create_run([{'channel':'chat','body':'Pending'}])
    with TestClient(create_app(fixture_runtime,start_worker=False)) as c:
        url=f'/api/v1/console/runs/{rid}/report'
        assert c.get(url).status_code==401
        assert c.get('/api/v1/console/reports/archive').status_code==401
        r=c.get(url,headers=HEADERS)
        assert r.status_code==200 and r.headers['content-type']=='application/zip'
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            assert json.loads(z.read('manifest.json'))['report_kind']=='in_progress_snapshot'
        assert c.get('/api/v1/console/runs/missing/report',headers=HEADERS).status_code==404
        assert c.get('/api/v1/console/reports/archive',headers=HEADERS).status_code==200
        assert store.get_run(rid)['state']=='queued'
