import json

import pytest

from kajovospend.integrations.openai_fallback import (
    _JSON_SCHEMA,
    _build_text_format,
    ensure_schema_defaults,
)


def _check_required_subset(schema: dict):
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    req = schema.get("required", []) if isinstance(schema, dict) else []
    for r in req:
        assert r in props, f"required key {r} missing in properties"
    if schema.get("additionalProperties") is False:
        assert props, "additionalProperties false but no properties"
    for v in props.values():
        if isinstance(v, dict):
            _check_required_subset(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _check_required_subset(item)
    if isinstance(schema.get("items"), dict):
        _check_required_subset(schema["items"])


def test_json_schema_is_serializable_and_consistent():
    fmt = _build_text_format(True)
    # must be JSON serializable
    dumped = json.dumps(fmt)
    assert dumped
    schema = fmt["schema"]
    _check_required_subset(schema)


def test_ensure_schema_defaults_adds_missing_required():
    partial = {"line_items": [], "totals": {}}
    full = ensure_schema_defaults(partial)
    for key in _JSON_SCHEMA["required"]:
        assert key in full, f"missing key {key}"
    assert full["line_items"] == []
    assert isinstance(full["supplier"], dict)
    assert set(full["supplier"].keys()) == set(_JSON_SCHEMA["properties"]["supplier"]["required"])


def test_ensure_schema_defaults_nested_objects():
    partial = {"line_items": [], "totals": {"total_gross": None}}
    full = ensure_schema_defaults(partial)
    totals = full["totals"]
    for k in _JSON_SCHEMA["properties"]["totals"]["required"]:
        assert k in totals
    assert totals["total_gross"] is None

