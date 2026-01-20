from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, text, func
from sqlalchemy.orm import Session

from kajovospend.db.models import Supplier, Document, DocumentFile, LineItem, ImportJob


def counts(session: Session) -> Dict[str, int]:
    unprocessed = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "NEW")).scalar_one()
    processed = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "PROCESSED")).scalar_one()
    quarantine = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "QUARANTINE")).scalar_one()
    suppliers = session.execute(select(func.count()).select_from(Supplier)).scalar_one()
    docs = session.execute(select(func.count()).select_from(Document)).scalar_one()
    return {
        "unprocessed": int(unprocessed),
        "processed": int(processed),
        "quarantine": int(quarantine),
        "suppliers": int(suppliers),
        "documents": int(docs),
    }


def list_suppliers(session: Session, q: str = "") -> List[Supplier]:
    stmt = select(Supplier)
    if q.strip():
        qq = f"%{q.strip()}%"
        stmt = stmt.where((Supplier.ico.like(qq)) | (Supplier.name.like(qq)))
    stmt = stmt.order_by(Supplier.name.is_(None), Supplier.name)
    return list(session.execute(stmt).scalars().all())


def list_documents(session: Session, q: str = "", date_from: Optional[dt.date] = None, date_to: Optional[dt.date] = None) -> List[Tuple[Document, DocumentFile]]:
    stmt = select(Document, DocumentFile).join(DocumentFile, DocumentFile.id == Document.file_id)
    if date_from:
        # Allow docs with unknown date to still show up (prevents "empty list" when OCR didn't extract date).
        stmt = stmt.where((Document.issue_date.is_(None)) | (Document.issue_date >= date_from))
    if date_to:
        stmt = stmt.where((Document.issue_date.is_(None)) | (Document.issue_date <= date_to))
    if q.strip():
        # FTS5 needs raw SQL to collect ids
        ids = set()
        for row in session.execute(text("SELECT document_id FROM documents_fts WHERE documents_fts MATCH :q"), {"q": q}).fetchall():
            ids.add(int(row[0]))
        for row in session.execute(text("SELECT document_id FROM items_fts WHERE items_fts MATCH :q"), {"q": q}).fetchall():
            ids.add(int(row[0]))
        if not ids:
            return []
        stmt = stmt.where(Document.id.in_(sorted(ids)))
    stmt = stmt.order_by(Document.issue_date.desc().nullslast(), Document.created_at.desc())
    return list(session.execute(stmt).all())


def get_document_detail(session: Session, doc_id: int) -> Dict[str, Any]:
    doc = session.get(Document, doc_id)
    if not doc:
        raise KeyError(doc_id)
    f = session.get(DocumentFile, doc.file_id)
    items = session.execute(select(LineItem).where(LineItem.document_id == doc_id).order_by(LineItem.line_no)).scalars().all()
    return {"doc": doc, "file": f, "items": items}


def list_quarantine(session: Session) -> List[Tuple[Document, DocumentFile]]:
    stmt = select(Document, DocumentFile).join(DocumentFile, DocumentFile.id == Document.file_id).where(DocumentFile.status == "QUARANTINE")
    stmt = stmt.order_by(Document.created_at.desc())
    return list(session.execute(stmt).all())


def service_jobs(session: Session, limit: int = 200) -> List[ImportJob]:
    stmt = select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
