"""Outbound blocking controls. Unknown semantic evidence fails closed."""
import re
from .contracts import Assessment,SemanticCheck,GuardrailResult
from .route import INJECTION,AUTHORITY
PII=re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:sk-[\w-]{8,}|CUST-\w+|DEV-\d+|VAL-\d+)\b|(?i:bearer\s+\S+|(?:password|api[_ -]?key|token)\s*[:=]\s*\S+)|(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)')
ADDRESS=re.compile(r'(?i)\b\d{1,6}\s+(?:[\w.-]+\s+){1,4}(?:street|st\.|road|rd\.|avenue|ave\.|lane|drive|boulevard)\b')
NAME=re.compile(r'(?i)(?:my name is|i am|i\x27m|customer name\s*[:=])\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})')
def sanitize_text(text,known=None):
    for value in sorted((str(x) for x in (known or []) if x),key=len,reverse=True): text=re.sub(re.escape(value),'[IDENTITY]',text,flags=re.I)
    text=PII.sub('[PRIVATE]',text)
    text=ADDRESS.sub('[ADDRESS]',text)
    text=NAME.sub('[PERSON]',text)
    return text

def assess(provider,ticket):
    text=ticket.subject+'\n'+ticket.body
    if INJECTION.search(text): return Assessment(decision='escalate',reasons=['instruction_injection'],evidence=[])
    if AUTHORITY.search(text): return Assessment(decision='escalate',reasons=['sensitive_authority_request'],evidence=[])
    result=provider.complete('assess',{'ticket':{'subject':ticket.subject,'body':ticket.body}},Assessment,500)
    if any(quote not in text for quote in result.evidence): return Assessment(decision='unknown',reasons=['assessment_evidence_not_in_input'])
    return result

def render(draft):
    # Uncertainties are not rendered: factual output can only originate in cited claims.
    return 'This automated response is based on CloudServe documentation.'+'\n\n'+'\n\n'.join(c.text+' '+ ' '.join('['+x.doc_id+'/'+x.chunk_id+']' for x in c.citations) for c in draft.claims)

def validate(provider,ticket,draft,sources,classification,route):
    checks=[]
    def add(name,ok,reason): checks.append(GuardrailResult(name=name,status='pass' if ok else 'fail',reasons=[] if ok else [reason]))
    add('schema',draft.status=='draft' and bool(draft.claims) and len({c.claim_id for c in draft.claims})==len(draft.claims),'invalid_or_empty_claims')
    index={(s.doc_id,s.chunk_id):s for s in sources}
    citation_ok=all((x.doc_id,x.chunk_id) in index for c in draft.claims for x in c.citations)
    add('source_resolution',citation_ok,'citation_not_retrieved')
    text=render(draft)
    add('privacy',not PII.search(text) and not ADDRESS.search(text) and not NAME.search(text) and not re.search(r'\[(PRIVATE|IDENTITY|PERSON|SECRET|EMAIL|ADDRESS)\]',text),'outbound_private_data')
    add('instruction_integrity',not INJECTION.search(text+'\n'+ticket.subject+'\n'+ticket.body+'\n'+'\n'.join(s.content for s in sources)),'instruction_override_detected')
    add('tone_scope',bool(re.search(r'(?i)automat',draft.automation_disclosure)) and not re.search(r'(?i)\b(i|we)\s+(have\s+)?(reset|unlocked|deleted|refunded|deployed|guarantee)|\b(stupid|idiot)\b|\b(refund|guarantee|contractually)\b',text),'disclosure_or_scope_violation')
    floor=route.thresholds.get('intent',1.0); relevance=route.thresholds.get('retrieval',1.0)
    add('confidence_policy',route.action=='auto_candidate' and classification.status=='ok' and classification.intent is not None and classification.intent.calibrated_probability>=floor and bool(sources) and max(s.score for s in sources)>=relevance,'confidence_or_policy_failed')
    try:
        # Exact full cited text; no unrelated retrieved passages can stand in for support.
        payload={'ticket':{'subject':ticket.subject,'body':ticket.body},'claims':[{'claim_id':c.claim_id,'text':c.text,'sources':[index[(x.doc_id,x.chunk_id)].model_dump() for x in c.citations if (x.doc_id,x.chunk_id) in index]} for c in draft.claims]}
        result=provider.complete('validate',payload,SemanticCheck,500)
        covered={cid for check in result.claims if check.status=='pass' for cid in check.claim_ids}
        grounded=covered=={c.claim_id for c in draft.claims} and bool(draft.claims) and all(c.status=='pass' and bool(c.claim_ids) for c in result.claims)
        add('grounding',citation_ok and grounded,'claim_support_not_confirmed')
        checks.append(GuardrailResult(name='semantic_instruction_integrity',status=result.instruction_integrity,reasons=[]))
        checks.append(GuardrailResult(name='semantic_tone_scope',status=result.tone_scope,reasons=[]))
    except Exception:
        checks.append(GuardrailResult(name='grounding',status='unknown',reasons=['semantic_validator_unavailable']))
    return checks
