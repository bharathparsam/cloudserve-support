"""Structured operational metadata; customer text and exception messages stay out."""

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": (
                "http_request" if record.name == "uvicorn.access" else "service_log"
            ),
        }
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            _, method, target, _, status = record.args
            path = urlsplit(str(target)).path
            path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,}(?=/|$)", "/{id}", path)
            # Unmatched user-controlled URLs and queries never enter the log.
            if re.fullmatch(
                r"/(?:health/(?:live|ready)|metrics|api/v1/(?:status|settings|controls/automation|tickets(?:/\{id\}(?:/original|/events|/review)?)?|runs(?:/\{id\}(?:/metrics|/artifacts/[a-z_.]+)?)?))",
                path,
            ):
                data["route"] = path
            else:
                data["route"] = "unmatched_or_static"
            data["method"] = (
                method
                if method
                in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
                else "OTHER"
            )
            data["status"] = status if type(status) is int else None
        if record.exc_info and record.exc_info[0]:
            data["error_class"] = record.exc_info[0].__name__
        return json.dumps(data, allow_nan=False)
