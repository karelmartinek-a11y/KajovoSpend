from __future__ import annotations

from types import SimpleNamespace

from kajovospend.extract.standard_receipts import (
    build_seed_template_scaffolds,
    export_templates_payload,
    import_templates_payload,
)


def test_seed_template_scaffolds_include_top_families() -> None:
    seeds = build_seed_template_scaffolds()
    names = {row["name"] for row in seeds}
    assert "rhl_invoice_scaffold" in names
    assert "wolt_market_scaffold" in names
    assert "saac_scaffold" in names
    assert all(isinstance(row.get("schema_json"), str) and row["schema_json"] for row in seeds)


def test_export_import_templates_payload_roundtrip() -> None:
    rows = [
        SimpleNamespace(
            name="tpl-a",
            enabled=True,
            match_supplier_ico_norm="12345678",
            match_texts_json='["token-a"]',
            schema_json='{"version":1,"fields":{"supplier_ico":{"page":1,"box":[0.1,0.1,0.3,0.2]},"doc_number":{"page":1,"box":[0.3,0.1,0.5,0.2]},"issue_date":{"page":1,"box":[0.5,0.1,0.7,0.2]},"total_with_vat":{"page":1,"box":[0.7,0.1,0.9,0.2]}}}',
        )
    ]
    payload = export_templates_payload(rows)
    imported = import_templates_payload(payload)
    assert len(imported) == 1
    assert imported[0]["name"] == "tpl-a"
    assert imported[0]["match_supplier_ico_norm"] == "12345678"
