from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, text, func, case, bindparam
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from kajovospend.db.production_models import (
    Document as ProdDocument,
    LineItem as ProdLineItem,
    StandardReceiptTemplate,
    Supplier as ProdSupplier,
)
from kajovospend.db.working_models import (
    Document as WorkDocument,
    DocumentFile,
    ImportJob,
    ServiceState,
)


def working_counts(session: Session) -> Dict[str, int]:
    """Operational counts from working DB."""
    unprocessed = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "NEW")).scalar_one()
    processed = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "PROCESSED")).scalar_one()
    quarantine = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "QUARANTINE")).scalar_one()
    duplicates = session.execute(select(func.count()).select_from(DocumentFile).where(DocumentFile.status == "DUPLICATE")).scalar_one()
    jobs = session.execute(select(func.count()).select_from(ImportJob)).scalar_one()
    return {
        "unprocessed": int(unprocessed),
        "processed": int(processed),
        "quarantine": int(quarantine),
        "duplicates": int(duplicates),
        "import_jobs": int(jobs),
    }


def production_counts(session: Session) -> Dict[str, int]:
    """Business counts from production DB (no workflow statuses)."""
    suppliers = session.execute(select(func.count()).select_from(ProdSupplier)).scalar_one()
    docs = session.execute(select(func.count()).select_from(ProdDocument)).scalar_one()
    items = session.execute(select(func.count()).select_from(ProdLineItem)).scalar_one()
    return {
        "suppliers": int(suppliers),
        "documents": int(docs),
        "items": int(items),
    }


def dashboard_counts(prod_session: Session, *, working_session: Session | None = None) -> Dict[str, int]:
    """
    Mixed dashboard view:
    - processed/business numbers come exclusively from production DB
    - operational/quarantine/duplicate numbers come from working DB (optional)
    """
    prod = production_counts(prod_session)
    out: Dict[str, int] = {
        "processed": prod.get("documents", 0),
        "suppliers": prod.get("suppliers", 0),
        "items": prod.get("items", 0),
    }
    if working_session:
        ops = working_counts(working_session)
        out.update(
            {
                "unprocessed": ops.get("unprocessed", 0),
                "quarantine": ops.get("quarantine", 0),
                "duplicates": ops.get("duplicates", 0),
                "import_jobs": ops.get("import_jobs", 0),
                "processed_working": ops.get("processed", 0),
            }
        )
    return out


def run_stats(session: Session) -> Dict[str, Any]:
    """Statistics for RUN tab, production DB only."""

    doc_ids = [int(r[0]) for r in session.execute(select(ProdDocument.id)).all()]

    total_docs = len(doc_ids)
    if total_docs == 0:
        return {
            "suppliers": 0,
            "receipts": 0,
            "items": 0,
            "pct_offline": 0.0,
            "pct_api": 0.0,
            "pct_template": 0.0,
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
            select(func.count(func.distinct(ProdDocument.supplier_ico)))
            .where(ProdDocument.supplier_ico.is_not(None))
        ).scalar_one()
        or 0
    )

    items_count = int(
        session.execute(select(func.count()).select_from(ProdLineItem).where(ProdLineItem.document_id.in_(doc_ids))).scalar_one()
        or 0
    )

    # success by extraction method
    def _pct(method: str) -> float:
        n = int(
            session.execute(
                select(func.count())
                .select_from(ProdDocument)
                .where(ProdDocument.id.in_(doc_ids))
                .where(ProdDocument.extraction_method == method)
            ).scalar_one()
            or 0
        )
        return (100.0 * n / total_docs) if total_docs else 0.0

    pct_offline = _pct("offline")
    pct_api = _pct("openai")
    pct_template = _pct("template")
    pct_manual = _pct("manual")

    # sums and averages over items
    sum_with_vat = float(
        session.execute(
            select(func.sum(ProdLineItem.line_total))
            .select_from(ProdLineItem)
            .where(ProdLineItem.document_id.in_(doc_ids))
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
                            (ProdLineItem.vat_rate.is_(None)) | (ProdLineItem.vat_rate == 0),
                            ProdLineItem.line_total,
                        ),
                        else_=ProdLineItem.line_total / (1.0 + (ProdLineItem.vat_rate / 100.0)),
                    )
                )
            )
            .select_from(ProdLineItem)
            .where(ProdLineItem.document_id.in_(doc_ids))
        ).scalar_one()
        or 0.0
    )

    avg_item = float(
        session.execute(
            select(func.avg(ProdLineItem.line_total))
            .select_from(ProdLineItem)
            .where(ProdLineItem.document_id.in_(doc_ids))
        ).scalar_one()
        or 0.0
    )

    # per-document item counts (for avg/min/max)
    counts_rows = session.execute(
        select(ProdLineItem.document_id, func.count(ProdLineItem.id))
        .select_from(ProdLineItem)
        .where(ProdLineItem.document_id.in_(doc_ids))
        .group_by(ProdLineItem.document_id)
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
        select(ProdLineItem.document_id, func.sum(ProdLineItem.line_total))
        .where(ProdLineItem.document_id.in_(doc_ids))
        .group_by(ProdLineItem.document_id)
    ).all()}
    doc_totals = session.execute(select(ProdDocument.id, ProdDocument.total_with_vat).where(ProdDocument.id.in_(doc_ids))).all()
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
            ProdLineItem.document_id,
            func.sum(
                case(
                    (
                        (ProdLineItem.vat_rate.is_(None)) | (ProdLineItem.vat_rate == 0),
                        ProdLineItem.line_total,
                    ),
                    else_=ProdLineItem.line_total / (1.0 + (ProdLineItem.vat_rate / 100.0)),
                )
            ),
        )
        .where(ProdLineItem.document_id.in_(doc_ids))
        .group_by(ProdLineItem.document_id)
    ).all()}
    doc_totals_wo = session.execute(select(ProdDocument.id, ProdDocument.total_without_vat).where(ProdDocument.id.in_(doc_ids))).all()
    vals_wo = []
    for did, tv in doc_totals_wo:
        if tv is not None and float(tv) > 0.0:
            vals_wo.append(float(tv))
        else:
            vals_wo.append(per_doc_sums_wo.get(int(did), 0.0))
    avg_receipt_wo_vat = float(sum(vals_wo) / len(vals_wo)) if vals_wo else 0.0

    # max item
    max_row = session.execute(
        select(ProdLineItem.name, ProdLineItem.line_total)
        .select_from(ProdLineItem)
        .where(ProdLineItem.document_id.in_(doc_ids))
        .order_by(ProdLineItem.line_total.desc())
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
        "pct_template": pct_template,
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


def list_standard_receipt_templates(session: Session) -> List[Dict[str, Any]]:
    rows = (
        session.query(StandardReceiptTemplate)
        .order_by(StandardReceiptTemplate.updated_at.desc())
        .all()
    )
    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": int(row.id),
                "name": row.name,
                "enabled": bool(row.enabled),
                "match_ico": row.match_supplier_ico_norm,
                "updated_at": row.updated_at,
                "sample_file_name": row.sample_file_name,
                "sample_file_relpath": row.sample_file_relpath,
            }
        )
    return result


def get_standard_receipt_template(session: Session, template_id: int) -> Dict[str, Any]:
    tpl = session.get(StandardReceiptTemplate, int(template_id))
    if not tpl:
        raise KeyError(template_id)
    return {
        "id": int(tpl.id),
        "name": tpl.name,
        "enabled": bool(tpl.enabled),
        "match_supplier_ico_norm": tpl.match_supplier_ico_norm,
        "match_texts_json": tpl.match_texts_json,
        "schema_json": tpl.schema_json,
        "sample_file_name": tpl.sample_file_name,
        "sample_file_sha256": tpl.sample_file_sha256,
        "sample_file_relpath": tpl.sample_file_relpath,
        "updated_at": tpl.updated_at,
    }


def create_standard_receipt_template(session: Session, payload: Dict[str, Any]) -> int:
    tpl = StandardReceiptTemplate(
        name=str(payload.get("name") or ""),
        enabled=bool(payload.get("enabled")) if payload.get("enabled") is not None else True,
        match_supplier_ico_norm=payload.get("match_supplier_ico_norm"),
        match_texts_json=payload.get("match_texts_json"),
        schema_json=str(payload.get("schema_json") or ""),
        sample_file_name=payload.get("sample_file_name"),
        sample_file_sha256=payload.get("sample_file_sha256"),
        sample_file_relpath=payload.get("sample_file_relpath"),
    )
    session.add(tpl)
    session.flush()
    return int(tpl.id)


def update_standard_receipt_template(session: Session, template_id: int, payload: Dict[str, Any]) -> None:
    tpl = session.get(StandardReceiptTemplate, int(template_id))
    if not tpl:
        raise KeyError(template_id)
    if "name" in payload and payload["name"] is not None:
        tpl.name = str(payload["name"])
    if "enabled" in payload:
        tpl.enabled = bool(payload["enabled"])
    if "match_supplier_ico_norm" in payload:
        tpl.match_supplier_ico_norm = payload.get("match_supplier_ico_norm")
    if "match_texts_json" in payload:
        tpl.match_texts_json = payload.get("match_texts_json")
    if "schema_json" in payload and payload["schema_json"] is not None:
        tpl.schema_json = str(payload["schema_json"])
    if "sample_file_name" in payload:
        tpl.sample_file_name = payload.get("sample_file_name")
    if "sample_file_sha256" in payload:
        tpl.sample_file_sha256 = payload.get("sample_file_sha256")
    if "sample_file_relpath" in payload:
        tpl.sample_file_relpath = payload.get("sample_file_relpath")
    session.add(tpl)


def delete_standard_receipt_template(session: Session, template_id: int) -> None:
    tpl = session.get(StandardReceiptTemplate, int(template_id))
    if not tpl:
        raise KeyError(template_id)
    session.delete(tpl)


def list_suppliers(session: Session, q: str = "") -> List[ProdSupplier]:
    stmt = select(ProdSupplier)
    if q.strip():
        qq = f"%{q.strip()}%"
        stmt = stmt.where(
            (ProdSupplier.ico.like(qq))
            | (ProdSupplier.name.like(qq))
            | (ProdSupplier.dic.like(qq))
            | (ProdSupplier.address.like(qq))
            | (ProdSupplier.city.like(qq))
            | (ProdSupplier.legal_form.like(qq))
            | (ProdSupplier.street.like(qq))
        )
    stmt = stmt.order_by(ProdSupplier.name.is_(None), ProdSupplier.name)
    return list(session.execute(stmt).scalars().all())


def merge_suppliers(session: Session, keep_id: int, merge_ids: List[int]) -> None:
    merge_ids = [i for i in merge_ids if i != keep_id]
    if not merge_ids:
        return
    keep = session.get(ProdSupplier, keep_id)
    if not keep:
        raise KeyError(keep_id)

    docs = session.execute(select(ProdDocument).where(ProdDocument.supplier_id.in_(merge_ids))).scalars().all()
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
        sup = session.get(ProdSupplier, sid)
        if sup:
            session.delete(sup)
    session.flush()

def _apply_date_filters(stmt, date_from=None, date_to=None):
    if date_from:
        stmt = stmt.where(ProdDocument.issue_date >= date_from)
    if date_to:
        stmt = stmt.where(ProdDocument.issue_date <= date_to)
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

    stmt = select(func.count(ProdDocument.id))
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
    working_session: Session | None = None,
) -> List[Tuple[ProdDocument, DocumentFile | None]]:
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

        stmt = select(ProdDocument).where(ProdDocument.id.in_(ids))
        stmt = _apply_date_filters(stmt, date_from=date_from, date_to=date_to)
        docs = session.execute(stmt).scalars().all()
    else:
        stmt = select(ProdDocument).order_by(ProdDocument.issue_date.desc().nullslast(), ProdDocument.id.desc())
        stmt = _apply_date_filters(stmt, date_from=date_from, date_to=date_to)
        if limit is not None:
            stmt = stmt.limit(int(limit))
        if offset:
            stmt = stmt.offset(int(offset))
        docs = session.execute(stmt).scalars().all()

    file_map: Dict[int, DocumentFile | None] = {}
    if working_session:
        file_ids = [int(d.file_id) for d in docs if d.file_id is not None]
        if file_ids:
            rows = working_session.execute(select(DocumentFile).where(DocumentFile.id.in_(file_ids))).scalars().all()
            file_map = {int(f.id): f for f in rows}
    return [(d, file_map.get(int(d.file_id)) if d.file_id is not None else None) for d in docs]


def get_document_detail(session: Session, doc_id: int, *, working_session: Session | None = None) -> Dict[str, Any]:
    doc = session.get(ProdDocument, doc_id)
    if not doc:
        raise KeyError(doc_id)
    f = None
    if working_session and doc.file_id is not None:
        f = working_session.get(DocumentFile, doc.file_id)
    items = (
        session.execute(select(ProdLineItem).where(ProdLineItem.document_id == doc_id).order_by(ProdLineItem.line_no))
        .scalars()
        .all()
    )
    return {"doc": doc, "file": f, "items": items}


def list_quarantine(session: Session) -> List[Tuple[WorkDocument, DocumentFile]]:
    stmt = (
        select(WorkDocument, DocumentFile)
        .join(DocumentFile, DocumentFile.id == WorkDocument.file_id)
        .where(DocumentFile.status == "QUARANTINE")
        .order_by(WorkDocument.created_at.desc())
    )
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
        total_items = int(session.execute(text("SELECT COUNT(*) FROM items")).scalar_one() or 0)
        total_fts = int(session.execute(text("SELECT COUNT(*) FROM items_fts2")).scalar_one() or 0)
        if total_items == 0:
            return
        if total_fts < total_items:
            # Backfill missing rows (idempotent).
            session.execute(
                text(
                    """
                    INSERT INTO items_fts2(item_id, document_id, item_name, supplier_ico, doc_number)
                    SELECT i.id, i.document_id, COALESCE(i.name,''), COALESCE(d.supplier_ico,''), COALESCE(d.doc_number,'')
                    FROM items i
                    JOIN documents d ON d.id = i.document_id
                    LEFT JOIN items_fts2 fts ON fts.item_id = i.id
                    WHERE fts.item_id IS NULL
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
    Count of line items from production DB.
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
                WHERE items_fts2 MATCH :q
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
            WHERE (LOWER(i.name) LIKE LOWER(:qq) OR LOWER(d.supplier_ico) LIKE LOWER(:qq) OR LOWER(d.doc_number) LIKE LOWER(:qq))
            """
            return int(session.execute(text(sql), {"qq": qq}).scalar_one() or 0)

        sql = """
        SELECT COUNT(*) AS c
        FROM items i
        JOIN documents d ON d.id = i.document_id
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
    working_session: Session | None = None,
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

        where: List[str] = []

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
                    where_sql_ids = where_sql + " AND (LOWER(i.name) LIKE LOWER(:like) OR LOWER(d.supplier_ico) LIKE LOWER(:like) OR LOWER(d.doc_number) LIKE LOWER(:like))"
                    params["like"] = like
            except Exception:
                # fallback LIKE
                like = f"%{q}%"
                where_sql_ids = where_sql + " AND (LOWER(i.name) LIKE LOWER(:like) OR LOWER(d.supplier_ico) LIKE LOWER(:like) OR LOWER(d.doc_number) LIKE LOWER(:like))"
                params["like"] = like
            where_sql_final = where_sql_ids
        else:
            where_sql_final = where_sql

        sql_base = f"""
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
          d.file_id      AS file_id
        FROM items i
        JOIN documents d ON d.id = i.document_id
        LEFT JOIN suppliers s ON s.id = d.supplier_id
        WHERE {where_sql_final}
        ORDER BY d.issue_date DESC, d.id DESC, i.line_no ASC
        {lim_sql}
        """
        stmt = text(sql_base)
        if "ids" in params:
            stmt = stmt.bindparams(bindparam("ids", expanding=True))
        rows = [dict(r) for r in session.execute(stmt, params).mappings().all()]
        if working_session:
            file_ids = {int(r["file_id"]) for r in rows if r.get("file_id") is not None}
            file_map = {}
            if file_ids:
                from kajovospend.db.working_models import DocumentFile
                file_map = {int(f.id): f.current_path for f in working_session.execute(select(DocumentFile).where(DocumentFile.id.in_(file_ids))).scalars().all()}
            for r in rows:
                r["current_path"] = file_map.get(int(r["file_id"])) if r.get("file_id") is not None else None
        return rows
    except OperationalError:
        return []


def service_jobs(session: Session, limit: int = 200) -> List[ImportJob]:
    stmt = select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def service_state(session: Session) -> ServiceState | None:
    return session.execute(select(ServiceState).where(ServiceState.singleton == 1)).scalar_one_or_none()
