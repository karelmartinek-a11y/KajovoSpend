from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, text, func, case
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from kajovospend.db.models import Supplier, Document, DocumentFile, LineItem, ImportJob, ServiceState


def counts(session: Session) -> Dict[str, int]:
    unprocessed = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "NEW")).scalar_one()
    processed = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "PROCESSED")).scalar_one()
    quarantine = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "QUARANTINE")).scalar_one()
    duplicates = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "DUPLICATE")).scalar_one()
    suppliers = session.execute(select(func.count()).select_from(Supplier)).scalar_one()
    docs = session.execute(select(func.count()).select_from(Document)).scalar_one()
    return {
        "unprocessed": int(unprocessed),
        "processed": int(processed),
        "quarantine": int(quarantine),
        "duplicates": int(duplicates),
        "suppliers": int(suppliers),
        "documents": int(docs),
    }


def run_stats(session: Session) -> Dict[str, Any]:
    """Statistics for RUN tab.

    All indicators are computed strictly from *fully processed* receipts:
    documents whose underlying file has status PROCESSED.
    QUARANTINE and DUPLICATE files are excluded from denominators.
    """

    # base document set = documents that belong to files marked as PROCESSED
    doc_ids = [
        int(r[0])
        for r in session.execute(
            select(Document.id)
            .join(DocumentFile, Document.file_id == DocumentFile.id)
            .where(DocumentFile.status == "PROCESSED")
        ).all()
    ]

    total_docs = len(doc_ids)
    if total_docs == 0:
        return {
            "suppliers": 0,
            "receipts": 0,
            "items": 0,
            "pct_offline": 0.0,
            "pct_api": 0.0,
            "pct_manual": 0.0,
            "sum_items_wo_vat": 0.0,
            "sum_items_w_vat": 0.0,
            "avg_receipt": 0.0,
            "avg_receipt_wo_vat": 0.0,
            "avg_item": 0.0,
            "avg_items_per_receipt": 0.0,
            "min_items_per_receipt": 0,
            "max_items_per_receipt": 0,
            "max_item_price": 0.0,
            "max_item_name": None,
        }

    # counts
    suppliers = int(
        session.execute(
            select(func.count(func.distinct(Document.supplier_ico)))
            .join(DocumentFile, Document.file_id == DocumentFile.id)
            .where(DocumentFile.status == "PROCESSED")
            .where(Document.supplier_ico.is_not(None))
        ).scalar_one()
        or 0
    )

    items_count = int(
        session.execute(select(func.count()).select_from(LineItem).where(LineItem.document_id.in_(doc_ids))).scalar_one()
        or 0
    )

    # success by extraction method
    def _pct(method: str) -> float:
        n = int(
            session.execute(
                select(func.count())
                .select_from(Document)
                .where(Document.id.in_(doc_ids))
                .where(Document.extraction_method == method)
            ).scalar_one()
            or 0
        )
        return (100.0 * n / total_docs) if total_docs else 0.0

    pct_offline = _pct("offline")
    pct_api = _pct("openai")
    pct_manual = _pct("manual")

    # sums and averages over items
    sum_with_vat = float(
        session.execute(
            select(func.sum(LineItem.line_total))
            .select_from(LineItem)
            .where(LineItem.document_id.in_(doc_ids))
        ).scalar_one()
        or 0.0
    )

    # best-effort VAT removal; if vat_rate is 0/NULL, treat as already without VAT
    sum_wo_vat = float(
        session.execute(
            select(
                func.sum(
                    case(
                        (
                            (LineItem.vat_rate.is_(None)) | (LineItem.vat_rate == 0),
                            LineItem.line_total,
                        ),
                        else_=LineItem.line_total / (1.0 + (LineItem.vat_rate / 100.0)),
                    )
                )
            )
            .select_from(LineItem)
            .where(LineItem.document_id.in_(doc_ids))
        ).scalar_one()
        or 0.0
    )

    avg_item = float(
        session.execute(
            select(func.avg(LineItem.line_total))
            .select_from(LineItem)
            .where(LineItem.document_id.in_(doc_ids))
        ).scalar_one()
        or 0.0
    )

    # per-document item counts (for avg/min/max)
    counts_rows = session.execute(
        select(LineItem.document_id, func.count(LineItem.id))
        .select_from(LineItem)
        .where(LineItem.document_id.in_(doc_ids))
        .group_by(LineItem.document_id)
    ).all()
    per_doc_counts = [int(c or 0) for _doc_id, c in counts_rows]
    # documents with 0 items (shouldn't happen but keep deterministic)
    if len(per_doc_counts) < total_docs:
        per_doc_counts.extend([0] * (total_docs - len(per_doc_counts)))

    avg_items_per_receipt = float(sum(per_doc_counts) / total_docs) if total_docs else 0.0
    min_items = int(min(per_doc_counts) if per_doc_counts else 0)
    max_items = int(max(per_doc_counts) if per_doc_counts else 0)

    # average receipt value: prefer Document.total_with_vat, fallback to sum(items)
    per_doc_sums = {int(did): float(s or 0.0) for did, s in session.execute(
        select(LineItem.document_id, func.sum(LineItem.line_total))
        .where(LineItem.document_id.in_(doc_ids))
        .group_by(LineItem.document_id)
    ).all()}
    doc_totals = session.execute(select(Document.id, Document.total_with_vat).where(Document.id.in_(doc_ids))).all()
    vals = []
    for did, tv in doc_totals:
        if tv is not None:
            vals.append(float(tv))
        else:
            vals.append(per_doc_sums.get(int(did), 0.0))
    avg_receipt = float(sum(vals) / len(vals)) if vals else 0.0

    # average receipt value without VAT: prefer Document.total_without_vat, fallback to computed wo_vat per doc
    per_doc_sums_wo = {int(did): float(s or 0.0) for did, s in session.execute(
        select(
            LineItem.document_id,
            func.sum(
                case(
                    (
                        (LineItem.vat_rate.is_(None)) | (LineItem.vat_rate == 0),
                        LineItem.line_total,
                    ),
                    else_=LineItem.line_total / (1.0 + (LineItem.vat_rate / 100.0)),
                )
            ),
        )
        .where(LineItem.document_id.in_(doc_ids))
        .group_by(LineItem.document_id)
    ).all()}
    doc_totals_wo = session.execute(select(Document.id, Document.total_without_vat).where(Document.id.in_(doc_ids))).all()
    vals_wo = []
    for did, tv in doc_totals_wo:
        if tv is not None and float(tv) > 0.0:
            vals_wo.append(float(tv))
        else:
            vals_wo.append(per_doc_sums_wo.get(int(did), 0.0))
    avg_receipt_wo_vat = float(sum(vals_wo) / len(vals_wo)) if vals_wo else 0.0

    # max item
    max_row = session.execute(
        select(LineItem.name, LineItem.line_total)
        .select_from(LineItem)
        .where(LineItem.document_id.in_(doc_ids))
        .order_by(LineItem.line_total.desc())
        .limit(1)
    ).first()
    max_name = None
    max_price = 0.0
    if max_row:
        max_name = max_row[0]
        max_price = float(max_row[1] or 0.0)

    return {
        "suppliers": suppliers,
        "receipts": total_docs,
        "items": items_count,
        "pct_offline": pct_offline,
        "pct_api": pct_api,
        "pct_manual": pct_manual,
        "sum_items_wo_vat": sum_wo_vat,
        "sum_items_w_vat": sum_with_vat,
        "avg_receipt": avg_receipt,
        "avg_receipt_wo_vat": avg_receipt_wo_vat,
        "avg_item": avg_item,
        "avg_items_per_receipt": avg_items_per_receipt,
        "min_items_per_receipt": min_items,
        "max_items_per_receipt": max_items,
        "max_item_price": max_price,
        "max_item_name": max_name,
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
      JOIN documents d ON d.id = items_fts.document_id
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
          JOIN documents d ON d.id = items_fts.document_id
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
            .where(DocumentFile.status != "QUARANTINE")
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
        .where(DocumentFile.status != "QUARANTINE")
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


def _ensure_items_fts2_populated(session: Session) -> None:
    """
    items_fts2 is used for fast per-item fulltext search in UI tab "POLOŽKY".
    This function is intentionally idempotent and safe to call from read paths.
    """
    try:
        exists = session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items_fts2' LIMIT 1")
        ).scalar()
        if not exists:
            return
        c = int(session.execute(text("SELECT COUNT(*) FROM items_fts2")).scalar_one() or 0)
        if c > 0:
            return

        # Backfill in one statement (fast enough for tens of thousands of rows).
        session.execute(
            text(
                """
                INSERT INTO items_fts2(item_id, document_id, item_name, supplier_ico, doc_number)
                SELECT i.id, i.document_id, COALESCE(i.name,''), COALESCE(d.supplier_ico,''), COALESCE(d.doc_number,'')
                FROM items i
                JOIN documents d ON d.id = i.document_id
                """
            )
        )
        session.commit()
    except Exception:
        # If anything goes wrong, the UI will still work (fallback will be used).
        try:
            session.rollback()
        except Exception:
            pass


def count_items(session: Session, q: str = "") -> int:
    """
    Count of line items (excluding quarantine files).
    Supports fulltext search via items_fts2 if available.
    """
    try:
        q = (q or "").strip()
        if q:
            _ensure_items_fts2_populated(session)
            try:
                sql = """
                SELECT COUNT(*) AS c
                FROM items_fts2
                JOIN items i ON i.id = items_fts2.item_id
                JOIN documents d ON d.id = i.document_id
                JOIN files f ON f.id = d.file_id
                WHERE items_fts2 MATCH :q
                  AND f.status != 'QUARANTINE'
                """
                cnt = int(session.execute(text(sql), {"q": q}).scalar_one() or 0)
                if cnt > 0:
                    return cnt
            except Exception:
                pass
            # Fallback: LIKE scan (still acceptable for ~10k-100k rows)
            qq = f"%{q}%"
            sql = """
            SELECT COUNT(*) AS c
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN files f ON f.id = d.file_id
            WHERE f.status != 'QUARANTINE'
              AND (i.name LIKE :qq OR d.supplier_ico LIKE :qq OR d.doc_number LIKE :qq)
            """
            return int(session.execute(text(sql), {"qq": qq}).scalar_one() or 0)

        sql = """
        SELECT COUNT(*) AS c
        FROM items i
        JOIN documents d ON d.id = i.document_id
        JOIN files f ON f.id = d.file_id
        WHERE f.status != 'QUARANTINE'
        """
        return int(session.execute(text(sql)).scalar_one() or 0)
    except OperationalError:
        # DB not initialized yet; avoid breaking GUI.
        return 0


def _parse_fulltext(query: str) -> tuple[str, Dict[str, Any]]:
    """
    Převod dotazu na MATCH syntaxi s podporou 'AND'. Výchozí je OR mezi slovy.
    Příklad: "mléko AND bio" -> '(mléko) AND (bio)'
    """
    q = (query or "").strip()
    if not q:
        return "", {}
    tokens = q.split()
    parts = []
    buf = []
    use_and = False
    for t in tokens:
        if t.upper() == "AND":
            use_and = True
            continue
        buf.append(t)
    if use_and:
        parts = [f'({t})' for t in buf]
        match = " AND ".join(parts)
    else:
        match = " ".join(buf)
    return match, {}


def list_items(
    session: Session,
    q: str = "",
    *,
    limit: int | None = None,
    offset: int = 0,
    group_id: int | None = None,
    group_none: bool = False,
    vat_rate: float | None = None,
    ids_receipt: List[int] | None = None,
    ids_supplier: List[int] | None = None,
    price_op: str | None = None,
    price_val: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
) -> List[Dict[str, Any]]:
    """
    Paginated list of line items for UI tab "POLOŽKY".
    - fulltext: default OR mezi slovy, explicitní AND kombinuje termy.
    - filtry: group_id / bez skupiny, DPH, ID_Uctenky, ID_Dodavatele, cena (=,>,<,between).
    """
    try:
        q = (q or "").strip()
        params: Dict[str, Any] = {}
        lim_sql = ""
        if limit is not None:
            lim_sql = " LIMIT :lim OFFSET :off"
            params["lim"] = int(limit)
            params["off"] = int(offset or 0)

        where = ["f.status != 'QUARANTINE'"]

        # Skupiny
        if group_id is not None:
            where.append("i.group_id = :gid")
            params["gid"] = int(group_id)
        if group_none:
            where.append("(i.group_id IS NULL)")

        # DPH filtr
        if vat_rate is not None:
            where.append("COALESCE(i.vat_rate,0) = :vat")
            params["vat"] = float(vat_rate)

        # ID účtenek
        if ids_receipt:
            where.append("i.id_receipt IN :idr")
            params["idr"] = tuple(int(x) for x in ids_receipt)

        # ID dodavatelů
        if ids_supplier:
            where.append("i.id_supplier IN :idsup")
            params["idsup"] = tuple(int(x) for x in ids_supplier)

        # Cena/ks bez DPH (použijeme unit_price_net fallback z line_total_net/qty)
        price_expr = "COALESCE(i.unit_price_net, CASE WHEN i.quantity IS NOT NULL AND i.quantity != 0 THEN i.line_total_net / i.quantity ELSE NULL END)"
        if price_op in ("eq", "=") and price_val is not None:
            where.append(f"{price_expr} = :pval")
            params["pval"] = float(price_val)
        elif price_op in (">", "gt") and price_val is not None:
            where.append(f"{price_expr} > :pval")
            params["pval"] = float(price_val)
        elif price_op in ("<", "lt") and price_val is not None:
            where.append(f"{price_expr} < :pval")
            params["pval"] = float(price_val)
        elif price_op in ("between", "range") and price_min is not None and price_max is not None:
            where.append(f"{price_expr} BETWEEN :pmin AND :pmax")
            params["pmin"] = float(price_min)
            params["pmax"] = float(price_max)

        where_sql = " AND ".join(where) if where else "1=1"

        # fulltext
        if q:
            _ensure_items_fts2_populated(session)
            match, extra = _parse_fulltext(q)
            params.update(extra)
            try:
                sql_ids = f"""
                WITH hits AS (
                  SELECT item_id, bm25(items_fts2) AS rank
                  FROM items_fts2
                  WHERE items_fts2 MATCH :match
                )
                SELECT item_id
                FROM hits
                ORDER BY rank ASC
                {lim_sql}
                """
                params_ids = dict(params)
                params_ids["match"] = match
                rows = session.execute(text(sql_ids), params_ids).fetchall()
                ids = [int(r.item_id) for r in rows]
                if ids:
                    params["ids"] = tuple(ids)
                    where_sql_ids = where_sql + " AND i.id IN :ids"
                else:
                    like = f"%{q}%"
                    where_sql_ids = where_sql + " AND (i.name LIKE :like OR d.supplier_ico LIKE :like OR d.doc_number LIKE :like)"
                    params["like"] = like
            except Exception:
                # fallback LIKE
                like = f"%{q}%"
                where_sql_ids = where_sql + " AND (i.name LIKE :like OR d.supplier_ico LIKE :like OR d.doc_number LIKE :like)"
                params["like"] = like
            where_sql_final = where_sql_ids
        else:
            where_sql_final = where_sql

        sql = f"""
        SELECT
          COALESCE(i.id_item, i.id) AS id_item,
          i.id_receipt   AS id_receipt,
          i.id_supplier  AS id_supplier,
          i.document_id  AS document_id,
          i.line_no      AS line_no,
          i.name         AS item_name,
          i.quantity     AS quantity,
          i.vat_rate     AS vat_rate,
          i.unit_price_net AS unit_price_net,
          i.line_total_net AS line_total_net,
          i.line_total    AS line_total_gross,
          i.group_id     AS group_id,
          d.issue_date   AS issue_date,
          d.total_with_vat AS doc_total_with_vat,
          d.total_without_vat AS doc_total_without_vat,
          (
            SELECT COUNT(*)
            FROM items i2
            WHERE i2.document_id = d.id
          ) AS doc_items_count,
          d.doc_number   AS doc_number,
          d.supplier_ico AS supplier_ico,
          s.name         AS supplier_name,
          f.current_path AS current_path
        FROM items i
        JOIN documents d ON d.id = i.document_id
        JOIN files f ON f.id = d.file_id
        LEFT JOIN suppliers s ON s.id = d.supplier_id
        WHERE {where_sql_final}
        ORDER BY d.issue_date DESC NULLS LAST, d.id DESC, i.line_no ASC
        {lim_sql}
        """
        return [dict(r) for r in session.execute(text(sql), params).mappings().all()]
    except OperationalError:
        return []


def service_jobs(session: Session, limit: int = 200) -> List[ImportJob]:
    stmt = select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def service_state(session: Session) -> ServiceState | None:
    return session.execute(select(ServiceState).where(ServiceState.singleton == 1)).scalar_one_or_none()
