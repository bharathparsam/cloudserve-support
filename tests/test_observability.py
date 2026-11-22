import json
import logging
from src.observability import SafeJsonFormatter


def test_http_metadata_excludes_query_identity_and_arbitrary_path():
    formatter = SafeJsonFormatter()
    record = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        0,
        "%s %s %s %s %s",
        (
            "private-ip",
            "GET",
            "/api/v1/tickets?search=private-person@example.com",
            "1.1",
            200,
        ),
        None,
    )
    value = formatter.format(record)
    assert "private" not in value
    assert json.loads(value)["route"] == "/api/v1/tickets"
    record.args = ("private-ip", "GET", "/private-person@example.com", "1.1", 404)
    assert "private" not in formatter.format(record)


def test_operational_errors_never_include_exception_message_or_freeform_arguments():
    try:
        raise ValueError("private-secret-do-not-log")
    except ValueError as error:
        record = logging.LogRecord(
            "uvicorn.error",
            40,
            "",
            0,
            "private request %s",
            ("private-token",),
            (type(error), error, error.__traceback__),
        )
    value = SafeJsonFormatter().format(record)
    assert "private" not in value
    assert json.loads(value)["error_class"] == "ValueError"
