from __future__ import annotations

import datetime as dt

from kajovospend.db.models import Supplier
from kajovospend.service.processor import Processor


def _supplier_complete() -> Supplier:
    s = Supplier(
        ico="12345678",
        ico_norm="12345678",
        name="ACME s.r.o.",
        legal_form="společnost s ručením omezeným",
        address="Ulice 10, Praha, 11000",
        street="Ulice",
        street_number="10",
        city="Praha",
        zip_code="11000",
        is_vat_payer=True,
        ares_last_sync=dt.datetime(2026, 1, 1),
    )
    return s


def test_supplier_gate_accepts_complete_supplier() -> None:
    ok, missing = Processor._supplier_details_complete(_supplier_complete())
    assert ok is True
    assert missing == []


def test_supplier_gate_rejects_missing_supplier_details() -> None:
    s = _supplier_complete()
    s.city = None
    s.legal_form = None
    s.is_vat_payer = None

    ok, missing = Processor._supplier_details_complete(s)

    assert ok is False
    assert "město" in missing
    assert "právní forma" in missing
    assert "status plátce DPH" in missing
