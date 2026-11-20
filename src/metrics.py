"""Measured counts and rates. Unknown closure, reviews and privacy are never zero."""
import math
import numpy as np

def calculate(outcomes,expected):
    answered=sum(x['action']=='auto_respond' for x in outcomes);escalated=sum(x['action']=='escalate' for x in outcomes);blocked=sum(bool(x['blocked']) for x in outcomes)
    def values(key,subset=outcomes):return [float(x['timings'][key]) for x in subset if isinstance(x.get('timings',{}).get(key),(int,float)) and math.isfinite(x['timings'][key]) and x['timings'][key]>=0]
    times=values('processing_seconds');answers=[float(x.get('timings',{}).get('end_to_end_seconds',x.get('timings',{}).get('processing_seconds'))) for x in outcomes if x['action']=='auto_respond' and x.get('timings',{}).get('processing_seconds') is not None];end_to_end=values('end_to_end_seconds');queues=values('queue_seconds')
    failures=sum(x.get('versions',{}).get('model',{}).get('last_call_status')=='failed' or any(code in str(x.get('reason','')).lower() for code in ('provider','generation unavailable','sensitivity unknown')) for x in outcomes)
    invalid=sum('invalid input' in str(x.get('reason','')).lower() for x in outcomes)
    unique=len({x.get('record_id',i) for i,x in enumerate(outcomes)})==len(outcomes)
    reconcile=unique and len(outcomes)==expected and answered+escalated==len(outcomes) and blocked<=escalated and all(not x.get('blocked') or x['action']=='escalate' for x in outcomes)
    return {'counts':{'input':expected,'processed':len(outcomes),'answered':answered,'escalated':escalated,'blocked':blocked,'input_errors':invalid,'provider_failure_proxy':failures,'incomplete':max(0,expected-len(outcomes))},
      'business':{'auto_answer_rate':answered/expected if expected else None,'escalation_rate':escalated/expected if expected else None,'confirmed_fcr':None,'satisfaction_proxy':None,'repeat_contact_improvement':None,'replay_substantive_reply_mean_seconds':float(np.mean(answers)) if answers else None,'replay_substantive_reply_median_seconds':float(np.median(answers)) if answers else None,'replay_substantive_reply_p95_seconds':float(np.percentile(answers,95,method='linear')) if answers else None,'substantive_reply_count':len(answers),'nonresponse_or_handoff_count':expected-answered,'timing_definition':'Processing-admission replay timing; original historical received_at never subtracted. Queue delay reported separately where observed.','unavailable_reason':'Independent closure, customer follow-up and human reviews not supplied'},
      'technical':{'processing_median_seconds':float(np.median(times)) if times else None,'processing_p95_seconds':float(np.percentile(times,95,method='linear')) if times else None,'end_to_end_median_seconds':float(np.median(end_to_end)) if end_to_end else None,'end_to_end_p95_seconds':float(np.percentile(end_to_end,95,method='linear')) if end_to_end else None,'end_to_end_timing_count':len(end_to_end),'queue_median_seconds':float(np.median(queues)) if queues else None,'queue_timing_count':len(queues),'timing_count':len(times),'quantile_method':'numpy linear','classification':None,'semantic_citation_accuracy':None,'human_review_reason':'Independent claim-source review required','cache_counts':{p:sum(x.get('provenance')==p for x in outcomes) for p in ('Live','Cached','Fixture replay')},'latency_by_provenance':{p:{'count':len(v),'median_seconds':float(np.median(v)) if v else None,'p95_seconds':float(np.percentile(v,95,method='linear')) if v else None} for p in ('Live','Cached','Fixture replay') for v in [values('processing_seconds',[x for x in outcomes if x.get('provenance')==p])]}},
      'governance':{'outcome_coverage':len(outcomes)/expected if expected else None,'reconciled':reconcile,'fairness_quality':None,'availability':None,'released_privacy_incidents':None,'guardrail_activations':{n:sum(c.get('name')==n and c.get('status') in ('fail','unknown') for x in outcomes for c in x.get('checks',[])) for n in sorted({c.get('name') for x in outcomes for c in x.get('checks',[]) if c.get('name')})},'unknown_reason':'Independent review and timed observation required'},
      'targets':{'auto_answer_target_is_not_fcr':True,'fcr':.60,'substantive_reply_mean_max_seconds':300,'satisfaction_proxy':4,'repeat_contacts_halved':True,'escalation_max':.30,'per_class_precision':.85,'hallucination_max':.05,'citation_accuracy':.95,'p95_seconds':3,'availability':.995,'released_privacy_incidents':0,'group_quality_difference_max':.05,'brief_aspirations_separate':{'fcr':.65,'satisfaction':4.2}}}

def prometheus_text(store):
    tickets=store.list_tickets(limit=100000)['items'];outcomes=[t['outcome'] for t in tickets if t.get('outcome')]
    lines=['# HELP cloudserve_outcomes Total persisted terminal outcomes','# TYPE cloudserve_outcomes counter']
    for action in ('auto_respond','escalate'):
        for channel in ('email','chat','docs_comment','forum','invalid'):
            count=sum(t['outcome']['action']==action and (t.get('ticket',{}).get('channel') or 'invalid')==channel for t in tickets if t.get('outcome'))
            lines.append(f'cloudserve_outcomes{{action="{action}",channel="{channel}"}} {count}')
    lines+=['# TYPE cloudserve_blocked counter',f'cloudserve_blocked {sum(x["blocked"] for x in outcomes)}','# TYPE cloudserve_automation_enabled gauge',f'cloudserve_automation_enabled {int(store.control()["enabled"])}','# TYPE cloudserve_inflight_jobs gauge',f'cloudserve_inflight_jobs {sum(t.get("status")=="running" for t in tickets)}','# TYPE cloudserve_processing_seconds histogram']
    values=[x['timings']['processing_seconds'] for x in outcomes if x.get('timings',{}).get('processing_seconds') is not None]
    for le in (.1,.5,1,3,10,30,120):lines.append(f'cloudserve_processing_seconds_bucket{{le="{le}"}} {sum(x<=le for x in values)}')
    lines.extend([f'cloudserve_processing_seconds_bucket{{le="+Inf"}} {len(values)}',f'cloudserve_processing_seconds_count {len(values)}',f'cloudserve_processing_seconds_sum {sum(values)}'])
    for name in sorted({c['name'] for x in outcomes for c in x.get('checks',[])}):
        safe=''.join(c for c in name if c.isalnum() or c=='_')
        lines.append(f'cloudserve_guardrail_blocks{{type="{safe}"}} '+str(sum(c['name']==name and c['status'] in ('fail','unknown') for x in outcomes for c in x.get('checks',[]))))
    for provenance in ('Live','Cached','Fixture replay'):
        lines.append(f'cloudserve_provenance{{kind="{provenance}"}} '+str(sum(x.get('provenance')==provenance for x in outcomes)))
    for lo in range(10):
        count=sum(lo/10<=(x.get('classification',{}).get('intent') or {}).get('calibrated_probability',-1) and ((x.get('classification',{}).get('intent') or {}).get('calibrated_probability',-1)<(lo+1)/10 or lo==9) for x in outcomes)
        lines.append(f'cloudserve_confidence_band{{lower="{lo/10:.1f}",upper="{(lo+1)/10:.1f}"}} {count}')
    calls={}
    for event in store.events(limit=100000)['items']:
        metadata=event.get('data',{}).get('provider') or event.get('data',{}).get('model')
        if isinstance(metadata,dict) and metadata.get('last_call_id'):calls[metadata['last_call_id']]=metadata
    lines+=['# TYPE cloudserve_provider_retries counter',f'cloudserve_provider_retries {sum(int(m.get("last_retry_count",0)) for m in calls.values())}']
    for error in ('network_timeout','401','402','403','429','500','503'):
        lines.append(f'cloudserve_provider_errors{{class="{error}"}} {sum(str(m.get("last_error"))==error or str(m.get("last_http_status"))==error for m in calls.values())}')
    return '\n'.join(lines)+'\n'


def add_stage_coverage(metrics,outcomes,events):
    stages=('ingest','classification','retrieval','sensitivity','routing','generation','validation','final')
    if events is None:
        metrics['governance']['stage_coverage']={'unavailable_reason':'Decision events not supplied'}
        return metrics
    applicable={o['record_id']:{e['stage'] for e in events if e['record_id']==o['record_id']} for o in outcomes}
    metrics['governance']['stage_coverage']={'required_stages':list(stages),'records_with_complete_stage_accounting':sum(set(stages)<=v for v in applicable.values()),'terminal_denominator':len(outcomes),'skipped_stage_rows':sum(e.get('data',{}).get('status')=='skipped' for e in events),'skipped_stage_rows_with_reason':sum(e.get('data',{}).get('status')=='skipped' and bool(e.get('data',{}).get('reason')) for e in events),'note':'Generation/validation rows may explicitly record skipped; skipped does not mean executed.'}
    return metrics

def compute_metrics(outcomes,records,events=None,body_boundary=None):
    """Pure shared API/CLI report. Protected sidecars never enter prediction state."""
    from evaluation.harness import enrich_metrics
    metrics=calculate(outcomes,len(records))
    enrich_metrics(metrics,outcomes,records,output=None,body_boundary=body_boundary)
    return add_stage_coverage(metrics,outcomes,events)
