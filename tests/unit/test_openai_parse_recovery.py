from __future__ import annotations

from unittest.mock import Mock, patch

from kajovospend.integrations.openai_fallback import OpenAIConfig, extract_with_openai


def _resp(body: dict) -> Mock:
    resp = Mock()
    resp.status_code = 200
    resp.headers = {"x-request-id": "req-parse-recover"}
    resp.content = b"{}"
    resp.json.return_value = body
    resp.text = str(body)
    resp.raise_for_status.return_value = None
    return resp


def test_balanced_object_parse_recovers_when_first_last_brace_fails() -> None:
    cfg = OpenAIConfig(
        api_key="sk-test-key-abcdefghijklmnopqrstuvwxyz",
        model="gpt-4o",
        use_json_schema=False,
    )
    noisy_text = (
        'prefix {"small":1} separator '
        '{"invoice_number":"B-2","supplier":{},"buyer":{},"line_items":[],"totals":{},"payment":{}} '
        "suffix"
    )
    body = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": noisy_text}],
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 50, "total_tokens": 70},
    }
    with patch("kajovospend.integrations.openai_fallback.requests.post") as mock_post:
        mock_post.return_value = _resp(body)
        out, _raw, _model = extract_with_openai(cfg, "ocr text", timeout=3)

    assert mock_post.call_count == 1
    assert isinstance(out, dict)
    assert out.get("doc_number") == "B-2"
