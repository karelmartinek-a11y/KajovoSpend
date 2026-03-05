from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from kajovospend.extract.standard_receipts import (
    TemplateSchemaError,
    match_template,
    normalized_box_to_pixel_box,
    parse_template_schema_text,
    serialize_template_schema,
)


VALID_SCHEMA = {
    "version": 1,
    "fields": {
        "supplier_ico": {"page": 1, "box": [0.1, 0.1, 0.3, 0.3]},
        "doc_number": {"page": 1, "box": [0.3, 0.1, 0.6, 0.3]},
        "issue_date": {"page": 1, "box": [0.1, 0.3, 0.4, 0.5]},
        "total_with_vat": {"page": 1, "box": [0.5, 0.3, 0.9, 0.5]},
        "items_region": {"page": 1, "box": [0.0, 0.5, 1.0, 1.0]},
    },
}


class TestStandardReceiptTemplateSchema(unittest.TestCase):
    def test_schema_roundtrip_v1(self) -> None:
        parsed = parse_template_schema_text(json.dumps(VALID_SCHEMA))
        dumped = serialize_template_schema(parsed)
        parsed2 = parse_template_schema_text(dumped)
        self.assertEqual(parsed2.version, 1)
        self.assertEqual(set(parsed.fields.keys()), set(parsed2.fields.keys()))
        self.assertEqual(parsed2.fields["total_with_vat"].box, parsed.fields["total_with_vat"].box)

    def test_schema_rejects_missing_required_field(self) -> None:
        payload = json.loads(json.dumps(VALID_SCHEMA))
        payload["fields"].pop("total_with_vat")
        with self.assertRaises(TemplateSchemaError):
            parse_template_schema_text(json.dumps(payload))

    def test_schema_rejects_invalid_box(self) -> None:
        payload = json.loads(json.dumps(VALID_SCHEMA))
        payload["fields"]["doc_number"]["box"] = [0.5, 0.5, 0.4, 0.6]
        with self.assertRaises(TemplateSchemaError):
            parse_template_schema_text(json.dumps(payload))


class TestNormalizedBoxToPixelBox(unittest.TestCase):
    def test_clamps_and_converts(self) -> None:
        x0, y0, x1, y1 = normalized_box_to_pixel_box((0.001, 0.002, 0.999, 1.0), 1000, 2000)
        self.assertEqual((x0, y0), (1, 4))
        self.assertEqual((x1, y1), (999, 2000))

    def test_rejects_invalid_box(self) -> None:
        with self.assertRaises(TemplateSchemaError):
            normalized_box_to_pixel_box((0.8, 0.2, 0.3, 0.9), 1200, 900)


class TestStandardReceiptTemplateMatching(unittest.TestCase):
    def _mk_template(self, **overrides) -> SimpleNamespace:
        data = {
            "match_supplier_ico_norm": overrides.get("match_supplier_ico_norm"),
            "match_texts_json": overrides.get("match_texts_json"),
        }
        return SimpleNamespace(**data)

    def test_match_by_ico_true(self) -> None:
        tpl = self._mk_template(match_supplier_ico_norm="12345678")
        text = "Dodavatel IČO: 12345678"
        self.assertTrue(match_template(tpl, text))

    def test_match_by_anchor_tokens_true(self) -> None:
        tpl = self._mk_template(match_texts_json=json.dumps(["Spar", "Faktura"]))
        text = "SPAR market\nFaktura číslo 2025/04"
        self.assertTrue(match_template(tpl, text))

    def test_match_false_without_rules(self) -> None:
        tpl = self._mk_template()
        self.assertFalse(match_template(tpl, "anything"))
