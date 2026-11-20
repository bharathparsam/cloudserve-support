"""Shared-pipeline unattended harness; labels/history used only after prediction."""
from __future__ import annotations
import argparse,csv,json,os,platform,time,hashlib
from datetime import datetime,timezone
from pathlib import Path
from src.ingest import load_records,digest,now
from src.metrics import calculate

HASH_PATHS=('src','evaluation','prompts','policies','assets/models','assets/corpus','scripts','frontend/src','tests','requirements.txt','requirements-linux-arm64.txt','requirements-linux-amd64.txt','pyproject.toml','Dockerfile','.dockerignore','compose.yaml','frontend/package.json','frontend/package-lock.json')
def tree_hash(root):
    root=Path(root);h=hashlib.sha256()
    for folder in HASH_PATHS:
        path=root/folder;paths=[path] if path.is_file() else sorted(path.rglob('*'))
        for p in paths:
            if p.is_file() and '__pycache__' not in str(p) and p.suffix not in ('.pyc','.DS_Store'):
                h.update(str(p.relative_to(root)).encode());h.update(p.read_bytes())
    return h.hexdigest()

def configured_fingerprint(settings):
    """Hash only runtime-consumed configured assets; never scan dataset siblings."""
    def file_entry(path):
        path=Path(path)
        try:name=str(path.resolve().relative_to(settings.root.resolve()))
        except ValueError:name='external:'+hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return {'path_reference':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    files={}
    for name in ('manifest.json','intent.joblib','urgency.joblib'):
        files['classifier/'+name]=file_entry(settings.classifier_dir/name)
    files['corpus']=file_entry(settings.corpus_path)
    for name in ('index-manifest.json','passages.json','passage-embeddings.npy'):
        files['retrieval/'+name]=file_entry(settings.corpus_path.parent/name)
    files['policy']=file_entry(settings.policy_path)
    files['decider_policy']=file_entry(settings.decider_policy_path)
    manifest_path=settings.corpus_path.parent/'index-manifest.json'
    manifest=json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    model='sentence-transformers/all-MiniLM-L6-v2';revision=manifest.get('embedding_revision')
    embedding={'model':model,'revision':revision,'snapshot_available':False,'files':{}}
    if revision:
        # Resolve the already-downloaded snapshot only. No network calls or untrusted data scan.
        try:
            from huggingface_hub.constants import HF_HUB_CACHE
            cache=Path(os.getenv('SENTENCE_TRANSFORMERS_HOME') or os.getenv('HF_HUB_CACHE',str(Path(os.environ['HF_HOME'])/'hub') if os.getenv('HF_HOME') else HF_HUB_CACHE))
            snapshot=cache/('models--'+model.replace('/','--'))/'snapshots'/revision
            if snapshot.is_dir():
                embedding['snapshot_available']=True
                for path in sorted(snapshot.rglob('*')):
                    if path.is_file():embedding['files'][str(path.relative_to(snapshot))]=hashlib.sha256(path.read_bytes()).hexdigest()
        except ImportError:pass
    cost_policy=getattr(settings,'generation_cost_policy','free_only')
    params={'configured_model':settings.model,'decider_model':settings.decider_model,'decider_mode':settings.decider_mode,'free_only':cost_policy=='free_only','generation_cost_policy':cost_policy,'paid_test_budget_usd':getattr(settings,'paid_test_budget_usd',None),
            'timeout_seconds':float(getattr(settings,'timeout_seconds',os.getenv('MODEL_TIMEOUT_SECONDS','30'))),
            'max_retries':int(getattr(settings,'max_retries',os.getenv('MODEL_MAX_RETRIES','2'))),
            'concurrency':os.getenv('MODEL_CONCURRENCY','1'),
            'generation':{'reasoning_policy':'disable_if_catalog_explicitly_optional_v1','temperature':0,'assessment_max_tokens':500,'draft_max_tokens':700,'validation_max_tokens':500,'schema_version':1}}
    result={'files':files,'embedding':embedding,'parameters':params}
    return {'sha256':digest(result),'details':result}

def final_gate(settings,store,pipeline):
    if getattr(settings,'generation_cost_policy','free_only')!='free_only':raise RuntimeError('Paid test authorization applies only to development; supplied validation remains free-only')
    frozen=settings.storage/'freeze.json';checked=settings.storage/'preflight.json'
    if not frozen.is_file() or not checked.is_file():raise RuntimeError('Final validation requires frozen artifacts and quota preflight')
    freeze=json.loads(frozen.read_text());preflight=json.loads(checked.read_text())
    if freeze.get('code_hash')!=tree_hash(settings.root):raise RuntimeError('Code or frozen assets changed after freeze')
    current=configured_fingerprint(settings)
    if freeze.get('configured_artifacts',{}).get('sha256')!=current['sha256']:raise RuntimeError('Configured runtime assets or model parameters changed after freeze')
    if preflight.get('configured_artifacts',{}).get('sha256')!=current['sha256']:raise RuntimeError('Preflight does not match configured runtime assets')
    if preflight.get('code_hash')!=freeze['code_hash']:raise RuntimeError('Preflight must cover the frozen code and artifacts')
    if not preflight.get('quota_sufficient') or not preflight.get('provider',{}).get('verified'):raise RuntimeError('Verified sufficient free quota required before final validation')
    if (datetime.now(timezone.utc)-datetime.fromisoformat(preflight['timestamp'])).total_seconds()>3600:raise RuntimeError('Final preflight is older than one hour')
    if not store.control()['enabled']:raise RuntimeError('Operator must explicitly enable automation before final validation')
    if freeze.get('configured_model')!=pipeline.provider.model:raise RuntimeError('Model differs from frozen configuration')
    if preflight.get('configured_model')!=pipeline.provider.model:raise RuntimeError('Model differs from preflight')
    if not (pipeline.classifier.ready and pipeline.retriever.ready and pipeline.policy.get('calibration_ready') and pipeline.policy.get('retrieval_ready')):raise RuntimeError('Frozen model/retrieval/policy readiness required')
    if getattr(pipeline,'decider_mode','disabled')=='enforce':
        if not pipeline.evaluator or not pipeline.evaluator.policy_ready:raise RuntimeError('Reviewed Decider calibration policy required for enforced final evaluation')
        decider_check=pipeline.evaluator.provider.preflight()
        if not decider_check.get('verified'):raise RuntimeError('Decider free provider verification failed')
    fresh=pipeline.provider.preflight()
    if not fresh.get('verified'):raise RuntimeError('Current provider session verification failed')
    return preflight

def run(input_path,output_path,runtime=None,kind='development'):
    output=Path(output_path)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):raise ValueError('Output directory must be empty; retain prior evidence')
    from src.runtime import create_runtime
    settings,store,pipeline=runtime or create_runtime()
    preflight=final_gate(settings,store,pipeline) if kind=='supplied_validation' else None
    # Do not open supplied input until final freeze/quota/control gates pass.
    records=load_records(input_path)
    if preflight and preflight.get('record_budget',0)<len(records):raise RuntimeError('Preflight record budget is smaller than supplied input')
    output.mkdir(parents=True,exist_ok=True)
    from filelock import FileLock,Timeout
    pipeline_lock=FileLock(str(settings.storage/'pipeline.lock'))
    try:pipeline_lock.acquire(timeout=5)
    except Timeout:raise RuntimeError('Pipeline is busy; wait for active ticket processing before evaluation')
    lock_path=settings.storage/'evaluation.lock'
    try:fd=os.open(lock_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        pipeline_lock.release();raise RuntimeError('Another evaluation owns this store; inspect lock before explicit recovery')
    os.write(fd,str(os.getpid()).encode());os.close(fd)
    started=time.monotonic();run_id=None;outcomes=[];original_error=None
    frozen_protocol=json.loads((settings.storage/'freeze.json').read_text()) if (settings.storage/'freeze.json').is_file() else {}
    manifest={'schema_version':1,'started_at':now(),'kind':kind,'input_hash':digest(records),'record_count':len(records),'code_hash':tree_hash(settings.root),'runtime':platform.platform(),'configured_artifacts':configured_fingerprint(settings),'dependencies_hash':digest((settings.root/'requirements.txt').read_text()),'interventions':0,'cache_policy':'fresh isolated model_cache before supplied_validation; versioned sensitivity cache otherwise, reuse explicitly recorded','provider':pipeline.provider.metadata,'state':'incomplete','human_review_sampling':'first 50 generated answers by ascending input position; two independent blank sheets','historical_times_used_for_latency':False,'short_long_body_character_boundary':frozen_protocol.get('short_long_body_character_boundary')}
    try:
        if kind=='supplied_validation':
            # Sole evaluation writer lock is held. Clear all preexisting assessment entries.
            with store.connection() as db:db.execute('DELETE FROM model_cache')
            manifest['cache_initial_entries']=0
        run_id=store.create_run(records,kind=kind);manifest['run_id']=run_id
        (output/'manifest.json').write_text(json.dumps(manifest,indent=2,default=str))
        while True:
            job=store.claim_job(run_id=run_id)
            if not job:break
            outcome=pipeline.process(job['original'],job['run_id'],job['record_id'],job['input_index']);outcomes.append(outcome.model_dump())
        metrics=calculate(outcomes,len(records));manifest['state']='completed' if metrics['governance']['reconciled'] else 'incomplete'
        if manifest['state']!='completed':store.fail_run(run_id,'Outcome reconciliation failed')
    except BaseException as exc:
        original_error=exc;manifest['error_class']=type(exc).__name__;manifest['state']='interrupted' if isinstance(exc,KeyboardInterrupt) else 'incomplete'
        if run_id:
            try:store.fail_run(run_id,manifest['error_class'],state=manifest['state'])
            except Exception:manifest['audit_store_failure']=True
    finally:
        try:
            manifest.update(finished_at=now(),elapsed_seconds=time.monotonic()-started,provider=pipeline.provider.metadata)
            metrics=calculate(outcomes,len(records));manifest['reconciliation']=metrics['counts']
            (output/'outcomes.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in outcomes))
            try:
                events=store.events(run_id=run_id,limit=100000)['items'] if run_id else []
                (output/'decisions.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,default=str)+'\n' for x in events))
            except Exception:
                events=[];manifest['event_export']='failed';manifest['state']='incomplete';metrics['governance']['stage_coverage_unavailable']='Audit store export failed'
            enrich_metrics(metrics,outcomes,records,output,manifest['short_long_body_character_boundary'])
            from src.metrics import add_stage_coverage
            add_stage_coverage(metrics,outcomes,events)
            write_reviews(output,outcomes)
            (output/'metrics.json').write_text(json.dumps(metrics,indent=2,default=str));(output/'metrics.md').write_text('# Measured run results\n\n```json\n'+json.dumps(metrics,indent=2,default=str)+'\n```\n')
            (output/'manifest.json').write_text(json.dumps(manifest,indent=2,default=str))
            if manifest['state']!='completed' and run_id:
                try:store.fail_run(run_id,'Incomplete run or evidence export',state=manifest['state'])
                except Exception:pass
        except BaseException as export_error:
            manifest['state']='incomplete';manifest['export_error_class']=type(export_error).__name__
            try:(output/'manifest.json').write_text(json.dumps(manifest,indent=2,default=str))
            except Exception:pass
            if run_id:
                try:store.fail_run(run_id,'Evidence export failed')
                except Exception:pass
            if original_error is None:original_error=export_error
        finally:
            try:os.unlink(lock_path)
            finally:pipeline_lock.release()
    if original_error is not None:raise original_error
    return manifest,metrics

def calibration_bands(pairs,field):
    bands=[]
    for i in range(10):
        rows=[x for x in pairs if x[2] and i/10<=x[2][field] and (x[2][field]<(i+1)/10 or i==9)]
        bands.append({'lower':i/10,'upper':(i+1)/10,'upper_inclusive':i==9,'count':len(rows),'accuracy':sum(a==b for a,b,c in rows)/len(rows) if rows else None,'mean_confidence':sum(c[field] for a,b,c in rows)/len(rows) if rows else None})
    return bands

def enrich_metrics(metrics,outcomes,records,output=None,body_boundary=None):
    from sklearn.metrics import classification_report,confusion_matrix
    indexed={o['input_index']:o for o in outcomes};classification={};rows=[]
    for dimension in ('intent','urgency'):
        pairs=[]
        for index,raw in enumerate(records):
            labels=raw.get('labels') if isinstance(raw,dict) else None;label=labels.get(dimension) if isinstance(labels,dict) else None
            if not isinstance(label,str):continue
            o=indexed.get(index);pred=(o.get('classification',{}).get(dimension) if o else None)
            pairs.append((label,pred['label'] if pred else '__unavailable__',pred))
        if pairs:
            labels=sorted({a for a,b,c in pairs}|{b for a,b,c in pairs});truth=[x[0] for x in pairs];pred=[x[1] for x in pairs]
            classification[dimension]={'labelled_count':len(pairs),'prediction_count':sum(x[2] is not None for x in pairs),'abstention_or_unprocessed_count':sum(x[2] is None for x in pairs),'report':classification_report(truth,pred,labels=labels,output_dict=True,zero_division=0),'labels':labels,'confusion_matrix':confusion_matrix(truth,pred,labels=labels).tolist(),'calibration_bands':calibration_bands(pairs,'calibrated_probability'),'raw_calibration_bands':calibration_bands(pairs,'raw_probability')}
        else:classification[dimension]={'unavailable_reason':'No supplied labels','labelled_count':0}
    metrics['technical']['classification']=classification
    for index,raw in enumerate(records):
        o=indexed.get(index);c=o.get('classification',{}) if o else {};labels=raw.get('labels') if isinstance(raw,dict) else None;labels=labels if isinstance(labels,dict) else {}
        row={'input_index':index,'record_id':o['record_id'] if o else None,'action':o['action'] if o else 'unprocessed','blocked':o['blocked'] if o else None}
        for dim in ('intent','urgency'):
            prediction=c.get(dim) or {};row.update({dim:prediction.get('label'),dim+'_true':labels.get(dim),dim+'_raw':prediction.get('raw_probability'),dim+'_calibrated':prediction.get('calibrated_probability')})
        rows.append(row)
    fields=['input_index','record_id','action','blocked','intent','intent_true','intent_raw','intent_calibrated','urgency','urgency_true','urgency_raw','urgency_calibrated']
    if output is not None:
        with (output/'classification.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    prohibited=[];hits=[];false_support=[];segments={};errors=[]
    for index,record in enumerate(records):
        rawlabels=record.get('labels') if isinstance(record,dict) else None;labels=rawlabels if isinstance(rawlabels,dict) else {};o=indexed.get(index);docs={s['doc_id'] for s in o['sources']} if o else set()
        if labels.get('must_not_auto_respond') is True:prohibited.append(bool(o and o['action']=='auto_respond'))
        if labels.get('answerable_from_docs') is True:hits.append(bool(docs & set(x for x in (labels.get('expected_doc_ids') or []) if isinstance(x,str))))
        if labels.get('answerable_from_docs') is False:false_support.append(bool(docs))
        if body_boundary is not None and isinstance(record,dict) and isinstance(record.get('body'),str):
            group='short' if len(record['body'])<=body_boundary else 'long'
            entry=segments.setdefault('body_length',{}).setdefault(group,{'input_count':0,'processed_count':0,'answered_count':0,'reviewed_count':0,'quality_mean':None})
            entry['input_count']+=1;entry['processed_count']+=int(o is not None);entry['answered_count']+=int(bool(o and o['action']=='auto_respond'))
        for dim in ('customer_tier','customer_region','language_fluency'):
            group=record.get(dim) if isinstance(record,dict) else None
            # Only finite known grouping vocabularies are exported; arbitrary custom strings may contain identity.
            known={'customer_tier':('enterprise','business','standard'),'customer_region':('north_america','europe','asia_pacific','latin_america'),'language_fluency':('fluent','non_fluent')}
            group=group if group in known[dim] else 'unavailable'
            entry=segments.setdefault(dim,{}).setdefault(group,{'input_count':0,'processed_count':0,'answered_count':0,'reviewed_count':0,'quality_mean':None})
            entry['input_count']+=1;entry['processed_count']+=int(o is not None);entry['answered_count']+=int(bool(o and o['action']=='auto_respond'))
        if not o:errors.append({'input_index':index,'error':'unprocessed'})
        elif o['blocked'] or o['classification']['status']!='ok':errors.append({'input_index':index,'record_id':o['record_id'],'blocked':o['blocked'],'reason':o['reason']})
    metrics['technical']['retrieval_proxy']={'hit_at_4':sum(hits)/len(hits) if hits else None,'answerable_denominator':len(hits),'false_support_rate':sum(false_support)/len(false_support) if false_support else None,'no_support_denominator':len(false_support),'semantic_support':False}
    metrics['governance']['prohibited_auto_answers']={'count':sum(prohibited),'labelled_denominator':len(prohibited),'unprocessed_labelled_records':sum(index not in indexed for index,raw in enumerate(records) if isinstance(raw,dict) and isinstance(raw.get('labels'),dict) and raw['labels'].get('must_not_auto_respond') is True)}
    metrics['governance']['segments']={'groups':segments,'quality_comparison':None,'reason':'No independent reviews imported. Automation rates are not quality fairness.'}
    metrics['governance']['error_inventory']=errors
    if output is not None:
        (output/'classification_metrics.json').write_text(json.dumps(classification,indent=2));(output/'retrieval_analysis.json').write_text(json.dumps(metrics['technical']['retrieval_proxy'],indent=2));(output/'segments.json').write_text(json.dumps(metrics['governance']['segments'],indent=2));(output/'errors.json').write_text(json.dumps(errors,indent=2))
    return metrics

def write_reviews(output,outcomes):
    fields=['record_id','input_index','answer','sources','reviewer','support_1_5','correctness_1_5','clarity_1_5','uncertainty_1_5','satisfaction_1_5','unsupported_claims','notes']
    rows=[{'record_id':o['record_id'],'input_index':o['input_index'],'answer':o['answer'],'sources':json.dumps(o['sources'])} for o in sorted(outcomes,key=lambda x:x['input_index']) if o['action']=='auto_respond'][:50]
    for name in ('human_review_sample.csv','reviewer_1.csv','reviewer_2.csv'):
        with (output/name).open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    (output/'availability.csv').write_text('timestamp,available,provider_available,elapsed_seconds\n')

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--kind',default='supplied_validation',choices=['development','supplied_validation']);args=p.parse_args()
    manifest,metrics=run(args.input,args.output,kind=args.kind);print(json.dumps({'run_id':manifest['run_id'],'state':manifest['state'],'counts':metrics['counts']}))
    if manifest['state']!='completed':raise SystemExit(1)
if __name__=='__main__':main()
