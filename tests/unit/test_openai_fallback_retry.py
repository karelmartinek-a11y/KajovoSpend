from __future__ import annotations

from unittest.mock import Mock, patch

from kajovospend.integrations.openai_fallback import (
    OpenAIConfig,
    extract_with_openai,
)


def _ok_response(body: dict) -> Mock:
    resp = Mock()
    resp.status_code = 200
    resp.content = b'{}'
    resp.headers = {"x-request-id": "rid-ok"}
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def _err_response(status: int) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.content = b'{"error":{"message":"temporary"}}'
    resp.headers = {"x-request-id": "rid-err"}
    resp.json.return_value = {"error": {"message": "temporary"}}
    resp.text = '{"error":{"message":"temporary"}}'

    def _raise() -> None:
        raise RuntimeError(f"http {status}")

    resp.raise_for_status.side_effect = _raise
    return resp


def test_extract_with_openai_retry_on_429_then_success() -> None:
    cfg = OpenAIConfig(api_key="sk-test-key", model="gpt-4o", use_json_schema=False)
    ok_body = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": '{"invoice_number":"A-1","supplier":{},"buyer":{},"line_items":[],"totals":{},"payment":{}}'}
                ],
            }
        ]
    }

    with patch("kajovospend.integrations.openai_fallback.requests.post") as mock_post, patch(
        "kajovospend.integrations.openai_fallback.time.sleep"
    ) as _sleep:
        mock_post.side_effect = [_err_response(429), _ok_response(ok_body)]

        out, _raw, _model = extract_with_openai(cfg, "ocr text", timeout=3)

    assert mock_post.call_count == 2
    assert isinstance(out, dict)
    assert out.get("doc_number") == "A-1"
