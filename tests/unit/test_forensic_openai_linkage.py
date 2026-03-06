from __future__ import annotations

import logging
from unittest.mock import Mock

from kajovospend.integrations import openai_fallback
from kajovospend.integrations.openai_fallback import OpenAIConfig, extract_with_openai


def _ok_response(body: dict) -> Mock:
    resp = Mock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.headers = {"x-request-id": "req-linkage"}
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def test_openai_request_response_include_linkage_fields(monkeypatch, caplog) -> None:
    body = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"invoice_number":"A-1","supplier":{},"buyer":{},"line_items":[],"totals":{},"payment":{}}',
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 44, "total_tokens": 55},
    }
    monkeypatch.setattr(openai_fallback.requests, "post", lambda *args, **kwargs: _ok_response(body))

    cfg = OpenAIConfig(
        api_key="sk-test-key-abcdefghijklmnopqrstuvwxyz",
        model="gpt-4o",
        use_json_schema=False,
        forensic_fields={
            "correlation_id": "corr-123",
            "file_sha256": "sha-456",
            "job_id": "job-789",
            "document_id": 42,
        },
    )
    caplog.set_level(logging.INFO, logger="kajovospend.integrations.openai_fallback")
    out, _raw, _model = extract_with_openai(cfg, "ocr text", timeout=3)

    assert isinstance(out, dict)
    request_payloads = [
        getattr(rec, "extra_payload", {})
        for rec in caplog.records
        if getattr(rec, "event_name", "") == "openai.request"
    ]
    response_payloads = [
        getattr(rec, "extra_payload", {})
        for rec in caplog.records
        if getattr(rec, "event_name", "") == "openai.response"
    ]
    assert request_payloads, "missing openai.request log record"
    assert response_payloads, "missing openai.response log record"
    for payload in (request_payloads[-1], response_payloads[-1]):
        assert payload.get("correlation_id") == "corr-123"
        assert payload.get("file_sha256") == "sha-456"
        assert payload.get("job_id") == "job-789"
        assert payload.get("document_id") == 42
