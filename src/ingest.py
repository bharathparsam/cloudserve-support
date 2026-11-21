from __future__ import annotations
import hashlib, json, re, uuid
from datetime import datetime, timezone
from pydantic import ValidationError
from .contracts import TicketInput, NormalizedTicket


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def sanitize_text(text: str, known: list[str] | None = None) -> str:
    try:
        from .guardrails import sanitize_text as sanitize

        return sanitize(text, known or [])
    except ImportError:
        for item in sorted(filter(None, known or []), key=len, reverse=True):
            text = re.sub(re.escape(item), "[IDENTITY]", text, flags=re.I)
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
        text = re.sub(
            r"(?i)\b(?:sk-[\w-]+|bearer\s+\S+|(?:password|api[_ -]?key|token)\s*[:=]\s*\S+)",
            "[SECRET]",
            text,
        )
        return text


def normalize(original, run_id, record_id=None, input_index=0):
    errors = []
    try:
        if not isinstance(original, dict):
            raise ValueError("Record must be an object")
        value = TicketInput.model_validate(original)
        if not value.body.strip():
            raise ValueError("Body must contain text")
        if value.received_at:
            datetime.fromisoformat(value.received_at.replace("Z", "+00:00"))
    except (ValidationError, ValueError, TypeError):
        errors = [
            "Invalid ticket fields; channel and nonempty body are required within documented limits."
        ]
        value = None
    known = (
        [str(original.get(k, "")) for k in ("customer_name", "customer_id")]
        if isinstance(original, dict)
        else []
    )
    return NormalizedTicket(
        record_id=record_id or str(uuid.uuid4()),
        run_id=run_id,
        input_index=input_index,
        ticket_id=value.ticket_id if value else None,
        channel=value.channel if value else None,
        subject=sanitize_text(value.subject, known) if value else "",
        body=sanitize_text(value.body, known) if value else "",
        received_at=value.received_at if value else None,
        ingested_at=now(),
        input_hash=digest(original),
        valid=value is not None,
        errors=errors,
    )


def load_records(path):
    from pathlib import Path

    p = Path(path)
    if p.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Input exceeds 10 MiB")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Input root must be an array")
    if len(data) > 10000:
        raise ValueError("Input exceeds 10000 records")
    return data
