"""Log formatting and request-correlation behaviour.

Correlation is not cosmetic here: tracing a booking through its notification jobs and LLM
calls in production depends on every record carrying the originating request id.
"""

from __future__ import annotations

import json
import logging

from httpx import AsyncClient

from app.core.logging import JsonFormatter, RequestIdFilter, request_id_var


class CapturingHandler(logging.Handler):
    """Collects records, with the same filter production handlers use."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.addFilter(RequestIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="booking confirmed",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_single_line_object() -> None:
    record = _make_record(request_id="abc123")

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "booking confirmed"
    assert payload["request_id"] == "abc123"


def test_json_formatter_forwards_caller_supplied_fields() -> None:
    """Fields passed via `extra=` must survive into the structured payload."""
    record = _make_record(request_id="abc123", appointment_id="appt-7", status_code=201)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["appointment_id"] == "appt-7"
    assert payload["status_code"] == 201


def test_json_formatter_handles_missing_request_id() -> None:
    payload = json.loads(JsonFormatter().format(_make_record()))

    assert payload["request_id"] is None


def test_request_id_filter_reads_the_ambient_context() -> None:
    token = request_id_var.set("ctx-99")
    try:
        record = _make_record()
        RequestIdFilter().filter(record)
        assert record.request_id == "ctx-99"
    finally:
        request_id_var.reset(token)


async def test_completion_log_carries_the_request_id(client: AsyncClient) -> None:
    """Regression: the contextvar must still be set when the completion record is emitted.

    Resetting it before logging silently produced `request_id: null` on every access log.
    """
    handler = CapturingHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        await client.get("/healthz", headers={"X-Request-ID": "corr-42"})
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    completed = [r for r in handler.records if r.getMessage() == "request completed"]
    assert completed, "middleware emitted no completion record"
    assert completed[0].request_id == "corr-42"
    assert completed[0].status_code == 200
