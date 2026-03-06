from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from kajovospend.utils.time import utc_now_naive
from .working_models import Supplier, DocumentFile, Document, LineItem, ImportJob, ServiceState

_ICO_DIGITS_RE = re.compile(r"\D+")


def _normalize_ico_soft(ico: Optional[str]) -> Optional[str]:
    if ico is None:
        return None
    raw = str(ico).strip()
    if not raw:
        return None
    digits = _ICO_DIGITS_RE.sub("", raw)
    if not digits:
        return None
    if len(digits) > 8:
        return digits
    return digits.zfill(8)


def upsert_supplier(session: Session, ico: str, **fields) -> Supplier:
    ico_norm = _normalize_ico_soft(ico) or str(ico).strip()
    s = session.execute(
        select(Supplier).where(
            (Supplier.ico_norm == ico_norm) | (Supplier.ico == ico_norm)
        )
    ).scalar_one_or_none()
    if not s:
        s = Supplier(ico=ico_norm, ico_norm=ico_norm)
        session.add(s)
    else:
        s.ico = ico_norm
        s.ico_norm = ico_norm
    for k, v in fields.items():
        if v is not None:
            setattr(s, k, v)
    session.flush()
    return s


def create_file_record(session: Session, sha256: str, original_name: str, path: str, pages: int, status: str,
                       mime_type: str | None = None) -> DocumentFile:
    f = DocumentFile(
        sha256=sha256,
        original_name=original_name,
        current_path=path,
        pages=pages,
        status=status,
        mime_type=mime_type,
    )
    session.add(f)
    session.flush()
    return f


def add_document(session: Session, file_id: int, supplier_id: int | None, supplier_ico: str | None,
                 doc_number: str | None, bank_account: str | None, issue_date, total_with_vat: float | None,
                 currency: str, confidence: float, method: str, requires_review: bool, review_reasons: str | None,
                 items: Iterable[dict],
                 *,
                 page_from: int = 1,
                 page_to: int | None = None,
                 total_without_vat: float | None = None,
                 total_vat_amount: float | None = None,
                 vat_breakdown_json: str | None = None,
                 processing_profile: str | None = None) -> Document:
    d = Document(
        file_id=file_id,
        supplier_id=supplier_id,
        supplier_ico=supplier_ico,
        doc_number=doc_number,
        bank_account=bank_account,
        issue_date=issue_date,
        total_with_vat=total_with_vat,
        total_without_vat=total_without_vat,
        total_vat_amount=total_vat_amount,
        vat_breakdown_json=vat_breakdown_json,
        currency=currency,
        extraction_confidence=confidence,
        extraction_method=method,
        requires_review=requires_review,
        review_reasons=review_reasons,
        page_from=int(page_from or 1),
        page_to=(int(page_to) if page_to is not None else None),
        doc_type="invoice" if doc_number else "receipt",
        processing_profile=processing_profile,
    )
    session.add(d)
    session.flush()
    line_no = 1
    for it in items:
        qty = float(it.get("quantity") or 1.0)
        if qty == 0.0:
            qty = 1.0
        vat_rate = float(it.get("vat_rate") or 0.0)
        line_total = float(it.get("line_total") or 0.0)
        li = LineItem(
            document_id=d.id,
            line_no=line_no,
            name=str(it.get("name") or "")[:512],
            quantity=qty,
            unit_price=it.get("unit_price"),
            unit_price_net=it.get("unit_price_net"),
            unit_price_gross=it.get("unit_price_gross"),
            vat_rate=vat_rate,
            line_total=line_total,
            line_total_net=it.get("line_total_net"),
            line_total_gross=it.get("line_total_gross"),
            vat_amount=it.get("vat_amount"),
            vat_code=it.get("vat_code"),
            ean=it.get("ean"),
            item_code=it.get("item_code"),
        )
        session.add(li)
        line_no += 1
    session.flush()
    return d


def update_service_state(session: Session, **kwargs) -> ServiceState:
    st = session.get(ServiceState, 1)
    if not st:
        st = ServiceState(singleton=1)
        session.add(st)
    for k, v in kwargs.items():
        setattr(st, k, v)
    session.flush()
    return st


def queue_size(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(ImportJob).where(ImportJob.status == "QUEUED")).scalar_one())


def rebuild_fts_for_document(session: Session, doc_id: int, text_content: str | None = None) -> None:
    # Working DB does not maintain FTS; no-op placeholder for compatibility.
    return
