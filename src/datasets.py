"""Fixed, fingerprinted development input for the local operator workflow.

This module never enumerates input directories or accepts caller-supplied paths.
Validation and hidden datasets are not discoverable through this interface.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from pydantic import ValidationError
from .contracts import TicketInput

DEVELOPMENT_COUNT = 500
DEVELOPMENT_FILENAME = "development_tickets.json"
MAX_BYTES = 10 * 1024 * 1024
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_CONTAINER_INPUT_ROOT = Path("/data/input")


class DatasetUnavailable(RuntimeError):
    """The fixed authorized development input is absent, changed or unreadable."""


def _limit(value):
    if value is not None and (
        type(value) is not int or not 1 <= value <= DEVELOPMENT_COUNT
    ):
        raise ValueError(
            "Development limit must be an integer from 1 to 500, or null for all records"
        )
    return value


def _candidates(settings):
    # A server-side directory override is exclusive. No HTTP body can supply it.
    configured = os.getenv("DEVELOPMENT_INPUT_DIR")
    if configured:
        return [Path(configured) / DEVELOPMENT_FILENAME]
    root = Path(settings.root)
    local = root / "data/input" / DEVELOPMENT_FILENAME
    # Explicit isolated Settings roots must not accidentally discover global app
    # input when tests or other local tooling use temporary storage/source roots.
    if root.resolve() != _PACKAGE_ROOT.resolve():
        return [local]
    return [_CONTAINER_INPUT_ROOT / DEVELOPMENT_FILENAME, local]


def _trusted_bytes(settings):
    try:
        manifest = json.loads(
            (Path(settings.classifier_dir) / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        expected = manifest.get("data_hash")
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise DatasetUnavailable(
                "Trusted development source fingerprint is unavailable"
            )
        for candidate in _candidates(settings):
            if candidate.is_symlink():
                raise DatasetUnavailable(
                    "Development dataset must be a regular authorized file"
                )
            if not candidate.exists():
                continue
            if not candidate.is_file():
                raise DatasetUnavailable(
                    "Development dataset must be a regular authorized file"
                )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
            with os.fdopen(descriptor, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise DatasetUnavailable(
                    "Development dataset exceeds the authorized input size"
                )
            if hashlib.sha256(raw).hexdigest() != expected:
                raise DatasetUnavailable(
                    "Development dataset does not match the trusted source fingerprint"
                )
            return raw
        raise DatasetUnavailable("Authorized development dataset is not mounted")
    except DatasetUnavailable:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        raise DatasetUnavailable(
            "Authorized development dataset or source fingerprint is unavailable"
        ) from None


def load_development(settings, limit=None):
    """Return original protected records only from the fixed verified development file.

    Labels/history stay attached for local Store/evaluation sidecars. The shared
    normalizer, not this launcher, controls predictive input allowlisting.
    """
    count = _limit(limit)
    raw = _trusted_bytes(settings)
    try:
        records = json.loads(raw)
        if not isinstance(records, list) or len(records) != DEVELOPMENT_COUNT:
            raise DatasetUnavailable(
                "Authorized development dataset must contain 500 ticket records"
            )
        for record in records:
            if not isinstance(record, dict):
                raise DatasetUnavailable(
                    "Authorized development dataset contains an invalid ticket"
                )
            TicketInput.model_validate(record)
    except DatasetUnavailable:
        raise
    except (UnicodeError, ValueError, TypeError, ValidationError):
        raise DatasetUnavailable(
            "Authorized development dataset contains invalid ticket JSON"
        ) from None
    return records if count is None else records[:count]


def list_datasets(settings):
    """Return metadata only; never return paths, original content or model requests."""
    try:
        load_development(settings)
        available = True
    except DatasetUnavailable:
        available = False
    return {
        "items": [
            {
                "id": "development",
                "name": "Supplied development tickets",
                "record_count": DEVELOPMENT_COUNT,
                "available": available,
                "format": "cloudserve-ticket-array",
                "description": "Fingerprint-verified development input for local support processing. Labels and history remain evaluation sidecars; supplied validation is not selectable.",
            }
        ],
        "template_url": "/api/v1/datasets/template",
    }


def ticket_template():
    """Safe examples of the accepted JSON-array input, with no source ticket data."""
    return [
        {
            "ticket_id": "example-email-001",
            "channel": "email",
            "subject": "API retry guidance",
            "body": "What does HTTP 429 mean, and how should I retry a request?",
        },
        {
            "ticket_id": "example-chat-001",
            "channel": "chat",
            "subject": "",
            "body": "Where should I start when setting up a new CloudServe project?",
        },
        {
            "ticket_id": "example-docs-001",
            "channel": "docs_comment",
            "subject": "Webhook documentation",
            "body": "How can I verify the signature of an incoming webhook?",
        },
        {
            "ticket_id": "example-forum-001",
            "channel": "forum",
            "subject": "Billing review request",
            "body": "I would like a support operator to review a disputed invoice.",
        },
    ]
