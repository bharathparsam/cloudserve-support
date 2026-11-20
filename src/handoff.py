"""Local source-faithful excerpts; dataset history is never a troubleshooting conversation."""
import re
from .contracts import Handoff

def build(ticket,classification,sources,reason):
    from .guardrails import sanitize_text
    request=sanitize_text((ticket.subject+' '+ticket.body).strip()) if ticket.valid else 'Input could not be normalized; operator inspection required.'
    summary=request[:700]+(' [Request excerpt; see original for full text.]' if len(request)>700 else '')
    reported=[]
    for sentence in re.split(r'(?<=[.!?])\s+|\n+',ticket.body):
        if re.search(r"(?i)\b(?:i|we)\s+(?:have\s+|already\s+)?(?:tried|retried|restarted|checked|cleared|tested|attempted)\b|\balready\s+(?:tried|retried|restarted|checked|cleared|tested)\b",sentence):
            reported.append(sanitize_text(sentence.strip()))
    values={}
    if reported:
        values={'troubleshooting':'Customer-reported steps (not independently verified): '+' '.join(reported),'missing_context':['No separate conversation history supplied; only steps stated in this request are included.']}
    return Handoff(summary=summary,reason=reason,original_reference='/api/v1/tickets/'+ticket.record_id+'/original',classification=classification,sources=sources,**values)
