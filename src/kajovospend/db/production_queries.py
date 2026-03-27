from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from kajovospend.utils.time import utc_now_naive
from .production_models import Supplier, Document, LineItem, StandardReceiptTemplate, DocumentPageAudit

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


def _find_existing_doc(session: Session, supplier_ico: str | None, doc_number: str | None, issue_date, total_with_vat) -> Document | None:
    q = select(Document)
    if supplier_ico:
        q = q.where(Document.supplier_ico == supplier_ico)
    if doc_number:
        q = q.where(Document.doc_number == doc_number)
    if issue_date:
        q = q.where(Document.issue_date == issue_date)
    if total_with_vat is not None:
        q = q.where(Document.total_with_vat == total_with_vat)
    return session.execute(q.limit(1)).scalar_one_or_none()


def insert_document_from_working(
    session: Session,
    supplier: Supplier | None,
    work_doc,
    work_items: Iterable,
    *,
    force: bool = False,
) -> Document:
    existing = _find_existing_doc(session, work_doc.supplier_ico, work_doc.doc_number, work_doc.issue_date, work_doc.total_with_vat)
    if existing and not force:
        # Zachovej a oprav referenci na zdrojový working soubor, pokud chybí.
        source_file_id = getattr(work_doc, "file_id", None)
        if existing.file_id is None and source_file_id is not None:
            existing.file_id = int(source_file_id)
            session.flush()
        return existing
    d = Document(
        # Production dokument nese rekonstruovatelnou vazbu na working file.
        file_id=int(work_doc.file_id) if getattr(work_doc, "file_id", None) is not None else None,
        supplier_id=supplier.id if supplier else None,
        supplier_ico=work_doc.supplier_ico,
        doc_number=work_doc.doc_number,
        bank_account=work_doc.bank_account,
        issue_date=work_doc.issue_date,
        total_with_vat=work_doc.total_with_vat,
        total_without_vat=work_doc.total_without_vat,
        total_vat_amount=work_doc.total_vat_amount,
        vat_breakdown_json=work_doc.vat_breakdown_json,
        currency=work_doc.currency,
        extraction_confidence=work_doc.extraction_confidence,
        extraction_method=work_doc.extraction_method,
        document_text_quality=work_doc.document_text_quality,
        openai_model=work_doc.openai_model,
        openai_raw_response=work_doc.openai_raw_response,
        requires_review=work_doc.requires_review,
        review_reasons=work_doc.review_reasons,
        page_from=work_doc.page_from,
        page_to=work_doc.page_to,
        doc_type=work_doc.doc_type,
        processing_profile=work_doc.processing_profile,
        created_at=work_doc.created_at or utc_now_naive(),
        updated_at=utc_now_naive(),
    )
    session.add(d)
    session.flush()
    for wi in work_items:
        li = LineItem(
            document_id=d.id,
            line_no=wi.line_no,
            name=wi.name,
            quantity=wi.quantity,
            unit_price=wi.unit_price,
            unit_price_net=wi.unit_price_net,
            unit_price_gross=wi.unit_price_gross,
            vat_rate=wi.vat_rate,
            line_total=wi.line_total,
            line_total_net=wi.line_total_net,
            line_total_gross=wi.line_total_gross,
            vat_amount=wi.vat_amount,
            vat_code=wi.vat_code,
            ean=wi.ean,
            item_code=wi.item_code,
            id_item=wi.id_item,
        )
        session.add(li)
    session.flush()
    return d
