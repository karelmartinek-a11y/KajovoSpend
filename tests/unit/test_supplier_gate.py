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
    ok, missing_blockers, missing_soft = Processor._supplier_details_complete(_supplier_complete())
    assert ok is True
    assert missing_blockers == []
    assert missing_soft == []


def test_supplier_gate_rejects_missing_ico() -> None:
    s = _supplier_complete()
    s.ico = None

    ok, missing_blockers, missing_soft = Processor._supplier_details_complete(s)

    assert ok is False
    assert "IČO" in missing_blockers
    assert missing_soft == []


def test_supplier_gate_rejects_missing_name() -> None:
    s = _supplier_complete()
    s.name = " "

    ok, missing_blockers, _missing_soft = Processor._supplier_details_complete(s)

    assert ok is False
    assert "název" in missing_blockers


def test_supplier_gate_missing_ares_sync_is_soft_review_only() -> None:
    s = _supplier_complete()
    s.ares_last_sync = None

    ok, missing_blockers, missing_soft = Processor._supplier_details_complete(s)

    assert ok is True
    assert missing_blockers == []
    assert "ARES synchronizace" in missing_soft


def test_supplier_gate_missing_vat_status_is_soft_review_only() -> None:
    s = _supplier_complete()
    s.is_vat_payer = None

    ok, missing_blockers, missing_soft = Processor._supplier_details_complete(s)

    assert ok is True
    assert missing_blockers == []
    assert "status plátce DPH" in missing_soft
