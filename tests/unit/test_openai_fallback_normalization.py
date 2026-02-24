from kajovospend.integrations.openai_fallback import _normalize_extracted_payload


def test_normalize_schema_payload_to_processor_shape() -> None:
    payload = {
        "invoice_number": "2026-001",
        "supplier": {"company_id": "12345678", "iban": "CZ6508000000192000145399"},
        "line_items": [
            {
                "description": "Polozka A",
                "quantity": 2,
                "unit": "ks",
                "unit_price_net": 100.0,
                "vat_rate": 21,
                "vat_amount": 42.0,
                "total_gross": 242.0,
            }
        ],
        "totals": {"subtotal_net": 200.0, "vat_total": 42.0, "total_gross": 242.0},
    }

    out = _normalize_extracted_payload(payload)

    assert out["doc_number"] == "2026-001"
    assert out["supplier_ico"] == "12345678"
    assert out["bank_account"] == "CZ6508000000192000145399"
    assert out["total_with_vat"] == 242.0
    assert out["total_without_vat"] == 200.0
    assert out["total_vat_amount"] == 42.0
    assert out["items"] == [
        {
            "name": "Polozka A",
            "quantity": 2,
            "unit": "ks",
            "unit_price": 100.0,
            "vat_rate": 21,
            "vat_amount": 42.0,
            "line_total": 242.0,
        }
    ]
