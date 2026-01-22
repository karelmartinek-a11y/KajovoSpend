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
        stmt = stmt.where(
            (Supplier.ico.like(qq))
            | (Supplier.name.like(qq))
            | (Supplier.dic.like(qq))
            | (Supplier.address.like(qq))
            | (Supplier.city.like(qq))
            | (Supplier.legal_form.like(qq))
            | (Supplier.street.like(qq))
        )
    stmt = stmt.order_by(Supplier.name.is_(None), Supplier.name)
    return list(session.execute(stmt).scalars().all())


def merge_suppliers(session: Session, keep_id: int, merge_ids: List[int]) -> None:
    merge_ids = [i for i in merge_ids if i != keep_id]
    if not merge_ids:
        return
    keep = session.get(Supplier, keep_id)
    if not keep:
        raise KeyError(keep_id)

    docs = session.execute(select(Document).where(Document.supplier_id.in_(merge_ids))).scalars().all()
    for d in docs:
        d.supplier_id = keep_id
        d.supplier_ico = keep.ico
        session.add(d)
        # keep FTS consistent
        try:
            session.execute(
                text("UPDATE documents_fts SET supplier_ico=:ico WHERE document_id=:id"),
                {"ico": keep.ico or "", "id": int(d.id)},
            )
        except Exception:
            pass

    for sid in merge_ids:
        sup = session.get(Supplier, sid)
        if sup:
            session.delete(sup)
    session.flush()

def _apply_date_filters(stmt, date_from=None, date_to=None):
    if date_from:
        stmt = stmt.where(Document.issue_date >= date_from)
    if date_to:
        stmt = stmt.where(Document.issue_date <= date_to)
    return stmt


def _fts_doc_ids_ranked(
    session: Session,
    q: str,
    *,
    date_from=None,
    date_to=None,
    limit: int | None = None,
    offset: int = 0,
) -> List[int]:
    qfts = (q or "").strip()

    # Rank with bm25 and deduplicate by picking best rank per doc_id.
    # Note: keep SQL small and deterministic; apply date filters on documents join.
    where_date = ""
    params: Dict[str, Any] = {"q": qfts}
    if date_from is not None:
        where_date += " AND d.issue_date >= :df"
        params["df"] = date_from
    if date_to is not None:
        where_date += " AND d.issue_date <= :dt"
        params["dt"] = date_to

    lim_sql = ""
    if limit is not None:
        lim_sql = " LIMIT :lim OFFSET :off"
        params["lim"] = int(limit)
        params["off"] = int(offset or 0)

    sql = f"""
    WITH hits AS (
      SELECT d.id AS doc_id, bm25(documents_fts) AS rank
      FROM documents_fts
      JOIN documents d ON d.id = documents_fts.document_id
      WHERE documents_fts MATCH :q {where_date}

      UNION ALL

      SELECT d.id AS doc_id, bm25(items_fts) AS rank
      FROM items_fts
      JOIN line_items li ON li.id = items_fts.line_item_id
      JOIN documents d ON d.id = li.document_id
      WHERE items_fts MATCH :q {where_date}
    ),
    dedup AS (
      SELECT doc_id, MIN(rank) AS rank
      FROM hits
      GROUP BY doc_id
    )
    SELECT doc_id
    FROM dedup
    ORDER BY rank ASC
    {lim_sql}
    """
    rows = session.execute(text(sql), params).fetchall()
    return [int(r.doc_id) for r in rows]


def count_documents(session: Session, q: str = "", date_from=None, date_to=None) -> int:
    q = (q or "").strip()
    if q:
        where_date = ""
        params: Dict[str, Any] = {"q": q}
        if date_from is not None:
            where_date += " AND d.issue_date >= :df"
            params["df"] = date_from
        if date_to is not None:
            where_date += " AND d.issue_date <= :dt"
            params["dt"] = date_to

        sql = f"""
        WITH hits AS (
          SELECT d.id AS doc_id
          FROM documents_fts
          JOIN documents d ON d.id = documents_fts.document_id
          WHERE documents_fts MATCH :q {where_date}

          UNION

          SELECT d.id AS doc_id
          FROM items_fts
          JOIN line_items li ON li.id = items_fts.line_item_id
          JOIN documents d ON d.id = li.document_id
          WHERE items_fts MATCH :q {where_date}
        )
        SELECT COUNT(*) AS c FROM hits
        """
        return int(session.execute(text(sql), params).scalar_one() or 0)

    stmt = select(func.count(Document.id))
    stmt = _apply_date_filters(stmt, date_from=date_from, date_to=date_to)
    return int(session.execute(stmt).scalar_one() or 0)


def list_documents(
    session: Session,
    q: str = "",
    date_from=None,
    date_to=None,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> List[Tuple[Document, DocumentFile]]:
    """
    Fast document listing suitable for tens of thousands of rows:
    - supports pagination (limit/offset)
    - FTS search returns ranked docs (bm25) and only loads the requested page.
    """
    q = (q or "").strip()
    if q:
        ids = _fts_doc_ids_ranked(
            session,
            q,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        if not ids:
            return []

        stmt = (
            select(Document, DocumentFile)
            .join(DocumentFile, Document.file_id == DocumentFile.id)
            .where(Document.id.in_(ids))
        )
        # extra safety on date range (even if already applied in FTS)
        stmt = _apply_date_filters(stmt, date_from=date_from, date_to=date_to)
        rows = session.execute(stmt).all()
        by_id: Dict[int, Tuple[Document, DocumentFile]] = {int(d.id): (d, f) for d, f in rows}
        # keep FTS order
        return [by_id[i] for i in ids if i in by_id]

    stmt = (
        select(Document, DocumentFile)
        .join(DocumentFile, Document.file_id == DocumentFile.id)
        .order_by(Document.issue_date.desc().nullslast(), Document.id.desc())
    )
    stmt = _apply_date_filters(stmt, date_from=date_from, date_to=date_to)
    if limit is not None:
        stmt = stmt.limit(int(limit))
    if offset:
        stmt = stmt.offset(int(offset))
    return session.execute(stmt).all()


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
