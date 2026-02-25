import pytest

from kajovospend.integrations.openai_fallback import (
    _JSON_SCHEMA,
    _OPENAI_JSON_SCHEMA,
    OpenAIConfig,
    SchemaInvariantError,
    canonicalize_openai_schema,
    ensure_schema_defaults,
    extract_with_openai,
    validate_schema_invariants_or_raise,
)
from kajovospend.integrations import openai_fallback


def _walk_object_nodes(schema: dict, path: str = "$"):
    if not isinstance(schema, dict):
        return
    t = schema.get("type")
    has_object = t == "object" or (isinstance(t, list) and "object" in t)
    props = schema.get("properties")
    if has_object and isinstance(props, dict):
        yield path, schema
    for key, value in schema.items():
        if isinstance(value, dict):
            yield from _walk_object_nodes(value, f"{path}.{key}")
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    yield from _walk_object_nodes(item, f"{path}.{key}[{idx}]")


def test_schema_canonicalization_adds_required_everywhere():
    schema = canonicalize_openai_schema(_JSON_SCHEMA)

    for path, node in _walk_object_nodes(schema):
        props = node.get("properties", {})
        required = node.get("required")
        assert isinstance(required, list), f"missing required list at {path}"
        assert set(required) == set(props.keys()), f"required mismatch at {path}"

    line_item_required = schema["properties"]["line_items"]["items"]["required"]
    assert "description" in line_item_required


def test_fail_fast_schema_invariant():
    broken_schema = {
        "type": "object",
        "properties": {
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"description": {"type": ["string", "null"]}},
                    "required": [],
                },
            }
        },
        "required": ["line_items"],
    }

    with pytest.raises(SchemaInvariantError):
        validate_schema_invariants_or_raise(broken_schema)


def test_ensure_schema_defaults_adds_line_items_and_totals():
    out = ensure_schema_defaults({"document_type": None})
    assert out["line_items"] == []
    totals = out["totals"]
    assert isinstance(totals, dict)
    assert set(totals.keys()) == set(_OPENAI_JSON_SCHEMA["properties"]["totals"]["properties"].keys())
    assert all(v is None for v in totals.values())


class _FakeResp:
    def __init__(self, status_code, data: bytes, headers=None):
        self.status_code = status_code
        self.content = data
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        import json as json_module

        return json_module.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise openai_fallback.requests.HTTPError(response=self)


def test_invalid_json_schema_no_fallback(monkeypatch):
    calls = {"count": 0}

    def fake_post(url, headers, json=None, timeout=None):
        calls["count"] += 1
        return _FakeResp(
            400,
            b'{"error":{"message":"Invalid schema in context (\'properties\', \'line_items\', \'items\')","type":"invalid_json_schema","code":"invalid_json_schema"}}',
            headers={"x-request-id": "req-invalid-schema"},
        )

    monkeypatch.setattr(openai_fallback.requests, "post", fake_post)

    cfg = OpenAIConfig(api_key="sk-test", model="auto", use_json_schema=True)
    obj, raw, _model = extract_with_openai(cfg, ocr_text="test", images=None, pdf=None, timeout=1)

    assert obj is None
    assert "invalid_json_schema" in raw
    assert calls["count"] == 1
