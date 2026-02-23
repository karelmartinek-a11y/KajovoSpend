from __future__ import annotations

from kajovospend.extract.vat_math import compute_document_totals, compute_item_derivations


def test_compute_item_derivations_from_legacy_fields() -> None:
    item = {
        "name": "A",
        "quantity": 2,
        "unit_price": 100.0,
        "vat_rate": 21.0,
        "line_total": 242.0,
    }
    out = compute_item_derivations(item)
    assert out["unit_price_net"] == 100.0
    assert out["unit_price_gross"] == 121.0
    assert out["line_total_net"] == 200.0
    assert out["line_total_gross"] == 242.0
    assert out["vat_amount"] == 42.0
    assert out["vat_code"] == "STANDARD"


def test_compute_document_totals_with_breakdown_and_flags() -> None:
    items = [
        {"quantity": 1, "unit_price": 100.0, "vat_rate": 21.0, "line_total": 121.0},
        {"quantity": 1, "unit_price": 50.0, "vat_rate": 0.0, "line_total": 50.0},
    ]
    net, vat, gross, breakdown, flags = compute_document_totals(items, total_with_vat=171.0)
    assert net == 150.0
    assert vat == 21.0
    assert gross == 171.0
    assert flags["sum_ok_gross"] is True
    assert flags["sum_ok"] is True
    assert len(breakdown) == 2
