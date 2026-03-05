from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from kajovospend.extract.standard_receipts import TemplateSchemaError, match_template, parse_template_schema_text


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
    def test_schema_valid_when_all_fields_present(self) -> None:
        schema_json = json.dumps(VALID_SCHEMA)
        parsed = parse_template_schema_text(schema_json)
        self.assertEqual(parsed.version, 1)
        self.assertIn("items_region", parsed.fields)
        self.assertEqual(parsed.fields["total_with_vat"].page, 1)

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


class TestStandardReceiptTemplateMatching(unittest.TestCase):
    def _mk_template(self, **overrides) -> SimpleNamespace:
        data = {
            "match_supplier_ico_norm": overrides.get("match_supplier_ico_norm"),
            "match_texts_json": overrides.get("match_texts_json"),
        }
        return SimpleNamespace(**data)

    def test_match_by_ico_and_text(self) -> None:
        tpl = self._mk_template(
            match_supplier_ico_norm="12345678",
            match_texts_json=json.dumps(["Spar", "Faktura"]),
        )
        text = "Dodavatel: SPAR CZ s.r.o. IČO: 12345678\nFaktura číslo 1001"
        self.assertTrue(match_template(tpl, text))

        text2 = "SPAR.cz faktura 1234 IČO 12345678"
        self.assertTrue(match_template(tpl, text2))

    def test_match_false_without_rules(self) -> None:
        tpl = self._mk_template()
        self.assertFalse(match_template(tpl, "anything"))
