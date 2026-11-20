"""Export an existing durable run without rerunning models or changing decisions."""
from __future__ import annotations
import json,platform,uuid
from datetime import datetime
from pathlib import Path
from src.ingest import digest,now
from src.metrics import compute_metrics
from .harness import enrich_metrics,write_reviews,tree_hash,configured_fingerprint

def _write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False,default=str),encoding='utf-8')

def export_run(settings,store,pipeline,run_id,output_path=None):
    """Produce safe assessment artifacts from persisted outputs and protected sidecars.

    This function never calls the prediction pipeline or provider. It requires a
    terminal run. Existing output directories are immutable, including partial
    exports. Export failures mark evidence incomplete and preserve what was written.
    """
    run=store.get_run(run_id)
    if run is None:raise ValueError('Run not found')
    if run['state'] not in ('completed','incomplete','failed','interrupted'):raise ValueError('Run is still active; export only terminal persisted state')
    if output_path is None:
        uuid.UUID(run_id)  # Stored identifiers are UUIDs; no path traversal.
        output=Path(settings.storage)/'evaluation'/run_id
    else:output=Path(output_path)
    # Existence itself is ownership evidence; never replace a previous attempt.
    if output.exists():raise FileExistsError('Export directory exists; preserve it and choose a new path')
    manifest={'schema_version':1,'run_id':run_id,'kind':run['kind'],'state':'incomplete','processing_state':run['state'],'created_at':run['created_at'],'started_at':run['started_at'],'finished_at':run['completed_at'],'exported_at':now(),'record_count':run['expected_count'],'export_mode':'persisted outcomes; no model reprocessing','interventions':None,'interventions_unavailable_reason':'UI operator activity is recorded separately; uninterrupted CLI intervention count is not inferred','elapsed_seconds':None,'duration_definition':'UTC run started_at to completed_at, including scheduling/model/retry time; unavailable if either timestamp absent','processing_code_hash':None,'processing_code_hash_unavailable_reason':'No processing code hash stored on the run; exporter code is reported separately','cache_policy':'Read persisted outcome provenance; no cache access or model call during export'}
    output_created=False
    try:
        output.mkdir(parents=True,exist_ok=False);output_created=True
        _write(output/'manifest.json',manifest)
        tickets=store.list_tickets(run_id=run_id,limit=max(run['expected_count'],1))['items']
        tickets=sorted(tickets,key=lambda t:t['input_index'])
        if [t['input_index'] for t in tickets]!=list(range(run['expected_count'])):raise RuntimeError('Persisted input positions do not reconcile')
        records=[store.original(t['record_id']) for t in tickets]
        outcomes=[t['outcome'] for t in tickets if t.get('outcome') is not None]
        hashes={o.get('versions',{}).get('code_hash') for o in outcomes}
        if len(hashes)==1 and None not in hashes:
            manifest['processing_code_hash']=next(iter(hashes));manifest.pop('processing_code_hash_unavailable_reason',None)
        events=store.events(run_id=run_id,limit=max(100000,len(tickets)*32))
        if len(events['items'])!=events['total']:raise RuntimeError('Decision export pagination incomplete')
        events=events['items']
        protocol_path=Path(settings.storage)/'freeze.json'
        protocol=json.loads(protocol_path.read_text()) if protocol_path.is_file() else {}
        boundary=protocol.get('short_long_body_character_boundary')
        metrics=compute_metrics(outcomes,records,events=events,body_boundary=boundary)
        coverage=metrics['governance']['stage_coverage']
        complete=bool(run['state']=='completed' and metrics['governance']['reconciled'] and len(outcomes)==run['expected_count'] and coverage['records_with_complete_stage_accounting']==len(outcomes))
        manifest.update({'state':'completed' if complete else 'incomplete','input_hash':digest(records),'reconciliation':metrics['counts'],'stage_coverage':coverage,'short_long_body_character_boundary':boundary,'exporter_code_hash':tree_hash(settings.root),'configured_artifacts_at_export':configured_fingerprint(settings),'runtime_at_export':platform.platform(),'dependencies_hash_at_export':digest((settings.root/'requirements.txt').read_text()),'provenance_counts':metrics['technical']['cache_counts'],'human_review_sampling':'first 50 generated answers by ascending input position; two independent blank sheets','historical_times_used_for_latency':False})
        if run['started_at'] and run['completed_at']:
            elapsed=(datetime.fromisoformat(run['completed_at'])-datetime.fromisoformat(run['started_at'])).total_seconds()
            if elapsed>=0:manifest['elapsed_seconds']=elapsed
            else:manifest['duration_unavailable_reason']='Persisted timestamps are inconsistent'
        provider_evidence=[]
        for event in events:
            data=event.get('data',{})
            evidence=data.get('provider') or (data.get('model') if isinstance(data.get('model'),dict) else None)
            if evidence:
                provider_evidence.append({'decision_id':event['decision_id'],'stage':event['stage'],'timestamp':event['timestamp'],'metadata':evidence})
        manifest['provider_evidence']=provider_evidence
        manifest['provider_evidence_note']='Persisted call/session metadata; catalog verification is not proof of live inference. See actual_model/usage/calls and result provenance. No new provider check during export.'
        manifest['versions_observed']=[{'record_id':o['record_id'],'versions':o.get('versions',{})} for o in outcomes]
        (output/'outcomes.jsonl').write_text(''.join(json.dumps(o,ensure_ascii=False,allow_nan=False)+'\n' for o in outcomes),encoding='utf-8')
        (output/'decisions.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False,allow_nan=False,default=str)+'\n' for e in events),encoding='utf-8')
        # The common enrichment owns all metric definitions; this call only emits its files.
        enrich_metrics(metrics,outcomes,records,output=output,body_boundary=boundary)
        _write(output/'metrics.json',metrics)
        (output/'metrics.md').write_text('# Measured persisted run results\n\n```json\n'+json.dumps(metrics,indent=2,ensure_ascii=False)+'\n```\n',encoding='utf-8')
        write_reviews(output,outcomes)
        _write(output/'manifest.json',manifest)
        with store.connection() as db:
            for path in sorted(output.iterdir()):
                if path.is_file():db.execute('INSERT OR IGNORE INTO artifact_manifests(run_id,name,data) VALUES(?,?,?)',(run_id,path.name,json.dumps({'sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest(),'exported_at':manifest['exported_at'],'state':manifest['state']})))
        if not complete:store.fail_run(run_id,'Persisted outcome or stage evidence reconciliation incomplete')
        return manifest
    except BaseException as exc:
        # A directory created by another writer is not ours to modify or mark failed.
        if not output_created:raise
        manifest['state']='incomplete';manifest['export_error_class']=type(exc).__name__
        try:_write(output/'manifest.json',manifest)
        except Exception:pass
        try:store.fail_run(run_id,'Evidence export failed: '+type(exc).__name__)
        except Exception:manifest['audit_store_failure']=True
        raise
