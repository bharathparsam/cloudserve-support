"""Deterministic routing; model assessment never grants release authority."""
import hashlib,json,re
from .contracts import RouteDecision
SENSITIVE={'security_incident','compliance_request','data_residency','rollback_request','feature_request','unclear_request'}
INJECTION=re.compile(r'(?i)(ignore|disregard|override).{0,45}(instruction|prompt|policy|previous)|system\s*prompt|\bdeveloper\s*message|reveal.{0,20}(secret|password|key)|<\s*/?\s*(system|script)')
AUTHORITY=re.compile(r'(?i)\b(refund|reimburse|compromis\w*|breach|hacked|guarantee|contractual)\b|\b(please|can you|could you|we need you to)\b.{0,35}\b(delete|reset|unlock|deploy|rollback|increase|change|cancel)\b')
def route(ticket,classification,sources,assessment,control,policy):
    ph=hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()
    reason=None
    text=ticket.subject+'\n'+ticket.body
    intent=classification.intent.label if classification.intent else None
    floors=policy.get('thresholds',{})
    floor=policy.get('confidence_threshold',policy.get('intent_threshold',floors.get('intent',1.0)))
    if isinstance(floor,dict): floor=floor.get(intent,1.0)
    relevance=policy.get('relevance_floor',policy.get('retrieval_floor',floors.get('retrieval',1.0)))
    allowed=policy.get('eligible_intents',policy.get('supported_intents',[]))
    if not ticket.valid: reason='invalid_input'
    elif not control.get('enabled',False): reason='automation_stopped'
    elif classification.status!='ok' or not classification.intent or not classification.urgency: reason='classification_unavailable'
    elif INJECTION.search(text): reason='instruction_injection'
    elif AUTHORITY.search(text) or intent in SENSITIVE: reason='sensitive_or_unsupported'
    elif assessment.decision!='clear': reason='sensitivity_'+assessment.decision
    elif not policy.get('calibration_ready',False) or not policy.get('retrieval_ready',False): reason='policy_evidence_unavailable'
    elif intent not in allowed: reason='unsupported_intent'
    elif classification.calibration_status in ('unavailable','unmeasured'): reason='calibration_unavailable'
    elif classification.intent.calibrated_probability<float(floor): reason='confidence_below_floor'
    elif not sources or max(s.score for s in sources)<float(relevance): reason='retrieval_below_floor'
    return RouteDecision(action='escalate' if reason else 'auto_candidate',reason_codes=[reason or 'eligible'],reason=(reason or 'Eligible for checked answer').replace('_',' '),policy_hash=ph,thresholds={'intent':float(floor),'retrieval':float(relevance)})
