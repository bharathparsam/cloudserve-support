import sqlite3,threading
import pytest
from src.logging_store import Store
from src.contracts import *

def outcome(job):
    c=Classification(status='unavailable',reason='fixture')
    return Outcome(record_id=job['record_id'],run_id=job['run_id'],input_index=job['input_index'],action='auto_respond',blocked=False,reason='test',answer='checked text',classification=c,route=RouteDecision(action='auto_candidate',reason_codes=['test'],reason='test',policy_hash='test'),checks=[GuardrailResult(name=n,status='pass') for n in ('schema','source_resolution','privacy','instruction_integrity','tone_scope','confidence_policy','grounding','semantic_instruction_integrity','semantic_tone_scope')],provenance='Fixture replay')
def test_stop_reenable_epoch_blocks_old_release_and_persists(tmp_path):
    p=tmp_path/'audit.db';store=Store(p);assert not store.control()['enabled']
    run=store.create_run([{'channel':'chat','body':'request'}]);j=store.claim_job();epoch=store.set_control(True,'Start')['epoch']
    ready=threading.Event();resume=threading.Event();result=[]
    def delayed():ready.set();resume.wait();result.append(store.finalize(outcome(j),epoch))
    thread=threading.Thread(target=delayed);thread.start();ready.wait();store.set_control(False,'Incident stop');store.set_control(True,'Explicit restart');resume.set();thread.join()
    assert result[0].action=='escalate' and result[0].answer is None
    again=Store(p);assert again.get_run(run)['reconciled'];assert again.get_ticket(j['record_id'])['outcome']['blocked']
    with again.connection() as db:assert db.execute('SELECT COUNT(*) FROM releases').fetchone()[0]==0

def test_valid_release_atomic_and_idempotent(tmp_path):
    s=Store(tmp_path/'db');r=s.create_run([{}]);j=s.claim_job();epoch=s.set_control(True,'Start')['epoch'];o=s.finalize(outcome(j),epoch)
    assert o.action=='auto_respond';assert s.finalize(outcome(j),epoch).record_id==o.record_id
    assert s.get_run(r)['processed_count']==1
    with s.connection() as db:assert db.execute('SELECT COUNT(*) FROM releases').fetchone()[0]==1

def test_audit_failure_rolls_back_release(tmp_path):
    s=Store(tmp_path/'db');r=s.create_run([{}]);j=s.claim_job();epoch=s.set_control(True,'Start')['epoch']
    with s.connection() as db:db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON decision_events BEGIN SELECT RAISE(ABORT,'test audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):s.finalize(outcome(j),epoch)
    with s.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM releases').fetchone()[0]==0

def test_restart_interrupts_and_duplicate_ids_preserved(tmp_path):
    p=tmp_path/'db';s=Store(p);r=s.create_run([{'ticket_id':'dup','channel':'chat','body':'one'},{'ticket_id':'dup','channel':'chat','body':'two'}]);s.claim_job()
    restarted=Store(p);restarted.recover_interrupted();assert restarted.get_run(r)['state']=='interrupted';assert restarted.list_tickets(run_id=r)['total']==2

def test_missing_mandatory_check_cannot_release(tmp_path):
    s=Store(tmp_path/'db');s.create_run([{}]);j=s.claim_job();epoch=s.set_control(True,'Start')['epoch'];o=outcome(j);o.checks=[GuardrailResult(name='arbitrary',status='pass')]
    with pytest.raises(ValueError):s.finalize(o,epoch)

def test_failed_stop_latches_releases_closed_across_store_instances(tmp_path):
    p=tmp_path/'db';s=Store(p);s.create_run([{}]);j=s.claim_job();epoch=s.set_control(True,'Start')['epoch']
    with s.connection() as db:db.execute("CREATE TRIGGER fail_stop BEFORE INSERT ON control_events BEGIN SELECT RAISE(ABORT,'test stop audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):s.set_control(False,'Incident stop')
    assert not s.control()['enabled']
    restarted=Store(p);assert not restarted.control()['enabled']
    assert restarted.finalize(outcome(j),epoch).action=='escalate'
    with s.connection() as db:db.execute('DROP TRIGGER fail_stop')
    assert s.set_control(True,'Explicit healthy re-enable')['enabled']

def test_enforced_decider_cannot_release_with_only_legacy_checks(tmp_path):
    s=Store(tmp_path/'db');s.create_run([{}]);j=s.claim_job();epoch=s.set_control(True,'Synthetic enable')['epoch']
    o=outcome(j);o.versions={'decider':{'mode':'enforce'}}
    with pytest.raises(ValueError):s.finalize(o,epoch)
    with s.connection() as db:assert db.execute('SELECT count(*) FROM releases').fetchone()[0]==0
