from __future__ import annotations

import datetime as dt
from typing import Iterable

from sqlalchemy import text, select, func
from sqlalchemy.orm import Session

from .models import Supplier, DocumentFile, Document, LineItem, ImportJob, ServiceState


def upsert_supplier(session: Session, ico: str, name: str | None = None, dic: str | None = None,
                    address: str | None = None, is_vat_payer: bool | None = None,
                    ares_last_sync: dt.datetime | None = None) -> Supplier:
    ico = ico.strip()
    existing = session.execute(select(Supplier).where(Supplier.ico == ico)).scalar_one_or_none()
    if existing:
        if name is not None:
            existing.name = name
        if dic is not None:
            existing.dic = dic
        if address is not None:
            existing.address = address
        if is_vat_payer is not None:
            existing.is_vat_payer = is_vat_payer
        if ares_last_sync is not None:
            existing.ares_last_sync = ares_last_sync
        session.add(existing)
        session.flush()
        return existing
    s = Supplier(
        ico=ico,
        name=name,
        dic=dic,
        address=address,
        is_vat_payer=is_vat_payer,
        ares_last_sync=ares_last_sync,
    )
    session.add(s)
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
                 items: Iterable[dict]) -> Document:
    d = Document(
        file_id=file_id,
        supplier_id=supplier_id,
        supplier_ico=supplier_ico,
        doc_number=doc_number,
        bank_account=bank_account,
        issue_date=issue_date,
        total_with_vat=total_with_vat,
        currency=currency,
        extraction_confidence=confidence,
        extraction_method=method,
        requires_review=requires_review,
        review_reasons=review_reasons,
    )
    session.add(d)
    session.flush()
    line_no = 1
    for it in items:
        li = LineItem(
            document_id=d.id,
            line_no=line_no,
            name=str(it.get("name") or "").strip()[:512] or f"Položka {line_no}",
            quantity=float(it.get("quantity") or 1.0),
            vat_rate=float(it.get("vat_rate") or 0.0),
            line_total=float(it.get("line_total") or 0.0),
        )
        session.add(li)
        line_no += 1
    session.flush()
    return d


def rebuild_fts_for_document(session: Session, doc_id: int, full_text: str) -> None:
    # Remove existing
    session.execute(text("DELETE FROM documents_fts WHERE document_id = :id"), {"id": doc_id})
    # Insert
    row = session.execute(select(Document).where(Document.id == doc_id)).scalar_one()
    session.execute(
        text("INSERT INTO documents_fts(document_id, supplier_ico, doc_number, bank_account, text) VALUES(:id,:ico,:dn,:ba,:t)"),
        {"id": doc_id, "ico": row.supplier_ico or "", "dn": row.doc_number or "", "ba": row.bank_account or "", "t": full_text or ""},
    )
    session.execute(text("DELETE FROM items_fts WHERE document_id = :id"), {"id": doc_id})
    items = session.execute(select(LineItem).where(LineItem.document_id == doc_id)).scalars().all()
    for it in items:
        session.execute(text("INSERT INTO items_fts(document_id, item_name) VALUES(:id,:name)"), {"id": doc_id, "name": it.name})


def update_service_state(session: Session, **kwargs) -> None:
    s = session.get(ServiceState, 1)
    if not s:
        s = ServiceState(singleton=1)
    for k, v in kwargs.items():
        if hasattr(s, k):
            setattr(s, k, v)
    s.last_seen = dt.datetime.utcnow()
    session.add(s)


def queue_size(session: Session) -> int:
    return session.execute(select(func.count()).select_from(ImportJob).where(ImportJob.status == "QUEUED")).scalar_one()
