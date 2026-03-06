from __future__ import annotations

from unittest.mock import Mock, patch

from kajovospend.integrations.openai_fallback import OpenAIConfig, extract_with_openai


def _resp(status: int, body: dict, *, req_id: str) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.headers = {"x-request-id": req_id}
    resp.content = b"{}"
    resp.json.return_value = body
    resp.text = str(body)
    resp.raise_for_status.return_value = None
    return resp


def test_parse_fail_on_output_cap_retries_with_higher_token_limit() -> None:
    cfg = OpenAIConfig(
        api_key="sk-test-key-abcdefghijklmnopqrstuvwxyz",
        model="gpt-4o",
        use_json_schema=False,
        max_output_tokens=100,
    )
    first = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"invoice_number":"A-1","supplier":{}'}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 100, "total_tokens": 110},
    }
    second = {
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
        "usage": {"input_tokens": 10, "output_tokens": 120, "total_tokens": 130},
    }

    with patch("kajovospend.integrations.openai_fallback.requests.post") as mock_post, patch(
        "kajovospend.integrations.openai_fallback.time.sleep"
    ) as _sleep:
        mock_post.side_effect = [
            _resp(200, first, req_id="req-1"),
            _resp(200, second, req_id="req-2"),
        ]
        out, _raw, _model = extract_with_openai(cfg, "ocr text", timeout=3)

    assert mock_post.call_count == 2
    second_payload = mock_post.call_args_list[1].kwargs.get("json") or {}
    assert int(second_payload.get("max_output_tokens") or 0) > 100
    assert isinstance(out, dict)
    assert out.get("doc_number") == "A-1"
