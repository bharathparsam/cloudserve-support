from .contracts import AnswerDraft

def draft(provider,ticket,sources):
    return provider.complete('draft',{'ticket':{'subject':ticket.subject,'body':ticket.body},'sources':[s.model_dump() for s in sources]},AnswerDraft,700)
