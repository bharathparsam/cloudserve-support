"""Durable local decisions and serialized stop/release transactions."""
from __future__ import annotations
import json,sqlite3,uuid,os
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timezone
from .contracts import Outcome,GuardrailResult,Handoff

def now(): return datetime.now(timezone.utc).isoformat()
def dump(x): return json.dumps(x,ensure_ascii=False,allow_nan=False,default=str)
def load(x): return json.loads(x) if x else None
class Store:
    def __init__(self,path):
        self.path=str(path); self._fail_closed=False; self.barrier=Path(str(path)+'.release-blocked'); Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY,kind TEXT,state TEXT,expected_count INTEGER,processed_count INTEGER DEFAULT 0,created_at TEXT,started_at TEXT,completed_at TEXT,heartbeat TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS input_records(record_id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(run_id),input_index INTEGER,original TEXT, UNIQUE(run_id,input_index));
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,record_id TEXT UNIQUE REFERENCES input_records(record_id),run_id TEXT REFERENCES runs(run_id),state TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS decision_events(decision_id TEXT PRIMARY KEY,timestamp TEXT,record_id TEXT REFERENCES input_records(record_id),run_id TEXT REFERENCES runs(run_id),stage TEXT,data TEXT);
CREATE INDEX IF NOT EXISTS events_record ON decision_events(record_id,timestamp);
CREATE TABLE IF NOT EXISTS outcomes(record_id TEXT PRIMARY KEY REFERENCES input_records(record_id),run_id TEXT REFERENCES runs(run_id),action TEXT CHECK(action IN ('auto_respond','escalate')),blocked INTEGER,data TEXT);
CREATE TABLE IF NOT EXISTS releases(record_id TEXT PRIMARY KEY REFERENCES outcomes(record_id),answer TEXT,epoch INTEGER,timestamp TEXT);
CREATE TABLE IF NOT EXISTS handoffs(record_id TEXT PRIMARY KEY REFERENCES outcomes(record_id),data TEXT);
CREATE TABLE IF NOT EXISTS operator_controls(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER,epoch INTEGER,reason TEXT,actor TEXT,timestamp TEXT);
CREATE TABLE IF NOT EXISTS control_events(event_id TEXT PRIMARY KEY,enabled INTEGER,epoch INTEGER,reason TEXT,actor TEXT,timestamp TEXT);
CREATE TABLE IF NOT EXISTS model_cache(cache_key TEXT PRIMARY KEY,data TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS review_records(record_id TEXT PRIMARY KEY REFERENCES input_records(record_id),status TEXT,notes TEXT,timestamp TEXT);
CREATE TABLE IF NOT EXISTS artifact_manifests(run_id TEXT REFERENCES runs(run_id),name TEXT,data TEXT,PRIMARY KEY(run_id,name));
''')
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)',(now(),))
            db.execute('INSERT OR IGNORE INTO operator_controls VALUES(1,0,0,?,?,?)',('Initial stopped state','system',now()))
        try: os.chmod(self.path,0o600)
        except OSError: pass
    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=15); db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA busy_timeout=15000')
        try:
            yield db; db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()
    def create_run(self,records,kind='development'):
        rid=str(uuid.uuid4()); stamp=now()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE state IN ('running','queued') LIMIT 1").fetchone(): raise RuntimeError('Another batch is active')
            db.execute('INSERT INTO runs(run_id,kind,state,expected_count,created_at,heartbeat,completed_at) VALUES(?,?,?,?,?,?,?)',(rid,kind,'queued' if records else 'completed',len(records),stamp,stamp,None if records else stamp))
            for index,original in enumerate(records):
                rec=str(uuid.uuid4())
                db.execute('INSERT INTO input_records VALUES(?,?,?,?)',(rec,rid,index,dump(original)))
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',(rec,rec,rid,'queued',stamp))
        return rid
    def claim_job(self,run_id=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT j.*,i.input_index,i.original FROM jobs j JOIN input_records i USING(record_id) WHERE j.state='queued' "+('AND j.run_id=? ' if run_id else '')+'ORDER BY j.created_at,i.input_index LIMIT 1',(run_id,) if run_id else ()).fetchone()
            if not row: return None
            db.execute("UPDATE jobs SET state='running' WHERE job_id=?",(row['job_id'],))
            db.execute("UPDATE runs SET state='running',started_at=COALESCE(started_at,?),heartbeat=? WHERE run_id=?",(now(),now(),row['run_id']))
            result=dict(row); result['original']=load(result['original']); return result
    def _event(self,db,record_id,run_id,stage,data):
        db.execute('INSERT INTO decision_events VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),now(),record_id,run_id,stage,dump(data)))
    def event(self,record_id,run_id,stage,data):
        with self.connection() as db:
            self._event(db,record_id,run_id,stage,data)
            db.execute('UPDATE runs SET heartbeat=? WHERE run_id=?',(now(),run_id))
    def _latch_closed(self):
        self._fail_closed=True
        try:
            with self.barrier.open('w') as f:
                f.write('Operator control/audit failure; explicit healthy re-enable required.'); f.flush(); os.fsync(f.fileno())
        except OSError:pass
    def control(self):
        try:
            with self.connection() as db:
                result=dict(db.execute('SELECT enabled,epoch,reason,actor,timestamp FROM operator_controls WHERE id=1').fetchone())
            result['enabled']=bool(result['enabled']) and not self._fail_closed and not self.barrier.exists()
            if self._fail_closed or self.barrier.exists():result['reason']='Release blocked after control/audit failure; explicit re-enable required'
            return result
        except Exception:
            self._latch_closed();raise
    def set_control(self,enabled,reason,actor='operator'):
        try:
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                epoch=db.execute('SELECT epoch FROM operator_controls WHERE id=1').fetchone()[0]+1
                db.execute('UPDATE operator_controls SET enabled=?,epoch=?,reason=?,actor=?,timestamp=? WHERE id=1',(int(enabled),epoch,reason,actor,now()))
                db.execute('INSERT INTO control_events VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),int(enabled),epoch,reason,actor,now()))
            self._fail_closed=False
            if self.barrier.exists():self.barrier.unlink()
            return self.control()
        except Exception:
            self._latch_closed();raise
    def finalize(self,outcome,expected_epoch):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT data FROM outcomes WHERE record_id=?',(outcome.record_id,)).fetchone()
            if existing: return Outcome.model_validate(load(existing[0]))
            ctrl=db.execute('SELECT enabled,epoch FROM operator_controls WHERE id=1').fetchone()
            if outcome.action=='auto_respond' and (not ctrl['enabled'] or ctrl['epoch']!=expected_epoch or self._fail_closed or self.barrier.exists()):
                reason='Automation changed while processing; draft withheld.'
                outcome=outcome.model_copy(update={'action':'escalate','answer':None,'blocked':True,'reason':reason,'handoff':Handoff(summary='Request needs operator review after automation control changed.',reason=reason,original_reference='/api/v1/tickets/'+outcome.record_id+'/original',classification=outcome.classification,sources=outcome.sources),'checks':outcome.checks+[GuardrailResult(name='operator_stop',status='fail',reasons=['epoch_changed_or_stopped'])]})
            elif outcome.action=='auto_respond':
                if outcome.versions.get('decider',{}).get('mode')=='enforce':
                    required_decider={'decider_input_policy','decider_retrieval_sufficiency','decider_grounding','decider_instruction_integrity','decider_tone_scope'}
                    if not required_decider <= {c.name for c in outcome.checks}:raise ValueError('Enforced Decider checks missing')
                required={'schema','source_resolution','privacy','instruction_integrity','tone_scope','confidence_policy','grounding','semantic_instruction_integrity','semantic_tone_scope'}
                if not outcome.answer or not required.issubset({c.name for c in outcome.checks}) or any(c.status!='pass' for c in outcome.checks): raise ValueError('Release requires a checked answer')
                outcome=outcome.model_copy(update={'checks':outcome.checks+[GuardrailResult(name='operator_stop',status='pass'),GuardrailResult(name='durable_audit',status='pass')]})
            db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?)',(outcome.record_id,outcome.run_id,outcome.action,int(outcome.blocked),outcome.model_dump_json()))
            if outcome.action=='auto_respond': db.execute('INSERT INTO releases VALUES(?,?,?,?)',(outcome.record_id,outcome.answer,ctrl['epoch'],now()))
            elif outcome.handoff: db.execute('INSERT INTO handoffs VALUES(?,?)',(outcome.record_id,outcome.handoff.model_dump_json()))
            self._event(db,outcome.record_id,outcome.run_id,'final',{'action':outcome.action,'blocked':outcome.blocked,'reason':outcome.reason,'checks':[c.model_dump() for c in outcome.checks]})
            db.execute("UPDATE jobs SET state='done' WHERE record_id=?",(outcome.record_id,))
            processed=db.execute('SELECT COUNT(*) FROM outcomes WHERE run_id=?',(outcome.run_id,)).fetchone()[0]
            expected=db.execute('SELECT expected_count FROM runs WHERE run_id=?',(outcome.run_id,)).fetchone()[0]
            db.execute('UPDATE runs SET processed_count=?,state=?,completed_at=?,heartbeat=? WHERE run_id=?',(processed,'completed' if processed==expected else 'running',now() if processed==expected else None,now(),outcome.run_id))
        return outcome
    def get_ticket(self,record_id):
        from .ingest import normalize
        with self.connection() as db:
            row=db.execute('SELECT i.*,j.state FROM input_records i JOIN jobs j USING(record_id) WHERE record_id=?',(record_id,)).fetchone()
            if not row:return None
            norm=normalize(load(row['original']),row['run_id'],record_id,row['input_index'])
            out=db.execute('SELECT data FROM outcomes WHERE record_id=?',(record_id,)).fetchone()
            review=db.execute('SELECT status,notes,timestamp FROM review_records WHERE record_id=?',(record_id,)).fetchone()
        return {'record_id':record_id,'run_id':row['run_id'],'input_index':row['input_index'],'ticket':{'subject':norm.subject,'body':norm.body,'channel':norm.channel},'status':row['state'],'outcome':load(out[0]) if out else None,'events':self.events(record_id=record_id,limit=1000)['items'],'review':dict(review) if review else {'status':'open','notes':''}}
    def list_tickets(self,limit=25,offset=0,**filters):
        with self.connection() as db: ids=[r[0] for r in db.execute('SELECT record_id FROM input_records '+('WHERE run_id=? ' if filters.get('run_id') else '')+'ORDER BY rowid DESC',(filters['run_id'],) if filters.get('run_id') else ())]
        result=[]
        for rec in ids:
            item=self.get_ticket(rec); out=item['outcome'] or {}; ticket=item['ticket']
            if filters.get('channel') and ticket['channel']!=filters['channel']:continue
            if filters.get('action') and out.get('action')!=filters['action']:continue
            if filters.get('blocked') is not None and out.get('blocked')!=filters['blocked']:continue
            if filters.get('urgency') and ((out.get('classification') or {}).get('urgency') or {}).get('label')!=filters['urgency']:continue
            if filters.get('search') and filters['search'].lower() not in (ticket['subject']+' '+ticket['body']).lower():continue
            result.append(item)
        return {'items':result[offset:offset+limit],'total':len(result)}
    def get_run(self,run_id):
        with self.connection() as db:
            row=db.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
        if not row:return None
        result=dict(row); result['reconciled']=result['state']=='completed' and result['processed_count']==result['expected_count']; return result
    def list_runs(self,limit=25,offset=0):
        with self.connection() as db:
            rows=db.execute('SELECT run_id FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall(); count=db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        return {'items':[self.get_run(x[0]) for x in rows],'total':count}
    def events(self,record_id=None,run_id=None,limit=100,offset=0):
        key,val=('record_id',record_id) if record_id else ('run_id',run_id)
        where=' WHERE '+key+'=?' if val else ''; args=(val,) if val else ()
        with self.connection() as db:
            rows=db.execute('SELECT * FROM decision_events'+where+' ORDER BY rowid LIMIT ? OFFSET ?',args+(limit,offset)).fetchall(); count=db.execute('SELECT COUNT(*) FROM decision_events'+where,args).fetchone()[0]
        return {'items':[dict(x)|{'data':load(x['data'])} for x in rows],'total':count}
    def original(self,record_id):
        with self.connection() as db: row=db.execute('SELECT original FROM input_records WHERE record_id=?',(record_id,)).fetchone()
        return load(row[0]) if row else None
    def review(self,record_id,status,notes):
        from .guardrails import sanitize_text
        with self.connection() as db: db.execute('INSERT INTO review_records VALUES(?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET status=excluded.status,notes=excluded.notes,timestamp=excluded.timestamp',(record_id,status,sanitize_text(notes),now()))
        return {'status':status,'notes':sanitize_text(notes),'timestamp':now()}
    def cache_get(self,key):
        with self.connection() as db: row=db.execute('SELECT data FROM model_cache WHERE cache_key=?',(key,)).fetchone()
        return load(row[0]) if row else None
    def cache_put(self,key,value):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO model_cache VALUES(?,?,?)',(key,dump(value),now()))
        return self.cache_get(key)
    def recover_interrupted(self,before=None):
        with self.connection() as db:
            db.execute("UPDATE runs SET state='interrupted',error='Process restarted before completion',heartbeat=? WHERE state IN ('queued','running') AND (? IS NULL OR created_at<?)",(now(),before,before))
            db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running') AND run_id IN (SELECT run_id FROM runs WHERE state='interrupted')")
    def fail_run(self,run_id,error,state='incomplete'):
        with self.connection() as db:
            db.execute('UPDATE runs SET state=?,error=?,heartbeat=? WHERE run_id=?',(state,str(error)[:200],now(),run_id))
            db.execute("UPDATE jobs SET state='interrupted' WHERE run_id=? AND state IN ('queued','running')",(run_id,))
