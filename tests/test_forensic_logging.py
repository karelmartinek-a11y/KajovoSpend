import json as json_module
import logging
from io import StringIO

import pytest

from kajovospend.utils.logging_setup import JsonLineFormatter, ForensicContextFilter, log_event
from kajovospend.utils.forensic_context import forensic_scope
from kajovospend.integrations.openai_fallback import (
    OpenAIConfig,
    _validate_against_schema,
    _JSON_SCHEMA,
    extract_with_openai,
)
from kajovospend.integrations import openai_fallback


def test_json_formatter_includes_contextvars():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLineFormatter())
    handler.addFilter(ForensicContextFilter())
    logger = logging.getLogger("kajovospend.test_forensic")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.addHandler(handler)

    with forensic_scope(correlation_id="corr-1"):
        log_event(logger, "test.event", "Test message", foo="bar")

    handler.flush()
    payload = json_module.loads(stream.getvalue().strip())
    assert payload["forensic"]["correlation_id"] == "corr-1"
    assert payload["event_name"] == "test.event"
    assert payload["extra"]["foo"] == "bar"


class _FakeResp:
    def __init__(self, status_code, data: bytes, headers=None):
        self.status_code = status_code
        self.content = data
        self.headers = headers or {}

    def json(self):
        return json_module.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise openai_fallback.requests.HTTPError(response=self)


def test_openai_wrapper_logs_retry(monkeypatch, caplog):
    success_body = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"line_items": [], "totals": {"total_gross": 0}}',
                    }
                ],
            }
        ]
    }
    calls = {"idx": 0}

    def fake_post(url, headers, json=None, timeout=None):
        idx = calls["idx"]
        calls["idx"] += 1
        if idx == 0:
            return _FakeResp(400, b'{"error":{"message":"bad","type":"invalid_request_error"}}')
        return _FakeResp(200, json_module.dumps(success_body).encode("utf-8"), headers={"x-request-id": "req-123"})

    monkeypatch.setattr(openai_fallback.requests, "post", fake_post)

    cfg = OpenAIConfig(api_key="sk-test", model="auto", use_json_schema=True)
    caplog.set_level(logging.INFO, logger="kajovospend.integrations.openai_fallback")
    obj, raw, model = extract_with_openai(cfg, ocr_text="Hello", images=None, pdf=None, timeout=1)

    assert isinstance(obj, dict)
    event_names = [getattr(rec, "event_name", "") for rec in caplog.records]
    assert "openai.error" in event_names
    assert "openai.retry" in event_names
    assert "openai.response" in event_names


def test_schema_validator_detects_missing_and_pass():
    obj_bad = {"totals": {"total_gross": 0}}
    errors = _validate_against_schema(obj_bad, _JSON_SCHEMA)
    assert any("line_items" in e for e in errors)

    obj_ok = {
        "document_type": None,
        "invoice_number": None,
        "issue_date": None,
        "due_date": None,
        "currency": None,
        "supplier": {k: None for k in _JSON_SCHEMA["properties"]["supplier"]["properties"].keys()},
        "buyer": {k: None for k in _JSON_SCHEMA["properties"]["buyer"]["properties"].keys()},
        "line_items": [],
        "totals": {"total_gross": 0, "subtotal_net": 0, "vat_total": 0},
        "payment": {k: None for k in _JSON_SCHEMA["properties"]["payment"]["properties"].keys()},
    }
    errors_ok = _validate_against_schema(obj_ok, _JSON_SCHEMA)
    assert errors_ok == []
