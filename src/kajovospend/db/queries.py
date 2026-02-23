from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional
import re

from sqlalchemy import text, select, func
from sqlalchemy.orm import Session

from .models import Supplier, DocumentFile, Document, LineItem, ImportJob, ServiceState

_ICO_DIGITS_RE = re.compile(r"\D+")


def _normalize_ico_soft(ico: Optional[str]) -> Optional[str]:
    """
    Soft normalizace IČO pro matching v DB:
    - None -> None
    - ponechá jen číslice
    - doplní zleva nuly na 8 (pokud délka <= 8)
    - pokud je >8 číslic, vrátí původní digit string (bez paddingu) – nechceme házet výjimku v DB vrstvě
    """
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


def upsert_supplier(
    session: Session,
    ico: str,
    name: str | None = None,
    dic: str | None = None,
    address: str | None = None,
    is_vat_payer: bool | None = None,
    ares_last_sync: dt.datetime | None = None,
    pending_ares: bool | None = None,
    *,
    legal_form: str | None = None,
    street: str | None = None,
    street_number: str | None = None,
    orientation_number: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    overwrite: bool | None = None,
) -> Supplier:
    # overwrite default: pokud jde o ARES sync (ares_last_sync != None), chceme přepsat i None hodnotami
    if overwrite is None:
        overwrite = ares_last_sync is not None

    ico_norm = _normalize_ico_soft(ico) or str(ico).strip()

    # Fast path: indexed lookup by normalized key (no full table scan).
    s = session.execute(
        select(Supplier).where(
            (Supplier.ico_norm == ico_norm) | (Supplier.ico == ico_norm)
        )
    ).scalar_one_or_none()

    if not s:
        s = Supplier(ico=ico_norm, ico_norm=ico_norm)
        session.add(s)
    else:
        # kanonizace IČO v DB (pokud doteď bylo třeba s mezerami)
        s.ico = ico_norm
        s.ico_norm = ico_norm

    def _set(attr: str, val):
        if overwrite or val is not None:
            setattr(s, attr, val)

    _set("name", name)
    _set("dic", dic)
    _set("legal_form", legal_form)
    _set("address", address)
    _set("street", street)
    _set("street_number", street_number)
    _set("orientation_number", orientation_number)
    _set("city", city)
    _set("zip_code", zip_code)
    _set("is_vat_payer", is_vat_payer)
    if ares_last_sync is not None:
        s.ares_last_sync = ares_last_sync
    if pending_ares is not None:
        s.pending_ares = bool(pending_ares)

    session.flush()
    return s


def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return float(default)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
        if not s:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _infer_doc_type(doc_number: str | None) -> str:
    s = str(doc_number or "").strip()
    return "invoice" if s else "receipt"


def _to_str(v, max_len: int) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:max_len]


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
    doc_type = _infer_doc_type(doc_number)

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
        doc_type=doc_type,
        processing_profile=processing_profile,
    )
    session.add(d)
    session.flush()
    line_no = 1
    sum_net = 0.0
    sum_gross = 0.0
    has_any_net = False
    has_any_gross = False

    for it in items:
        qty = _to_float(it.get("quantity"), 1.0)
        if qty == 0.0:
            qty = 1.0

        vat_rate = _to_float(it.get("vat_rate"), 0.0)

        unit_price_legacy = it.get("unit_price")
        unit_price_legacy_f = None if unit_price_legacy is None else _to_float(unit_price_legacy, 0.0)
        line_total_legacy = _to_float(it.get("line_total"), 0.0)

        unit_price_net = it.get("unit_price_net")
        unit_price_net_f = None if unit_price_net is None else _to_float(unit_price_net, 0.0)
        unit_price_gross = it.get("unit_price_gross")
        unit_price_gross_f = None if unit_price_gross is None else _to_float(unit_price_gross, 0.0)
        line_total_net = it.get("line_total_net")
        line_total_net_f = None if line_total_net is None else _to_float(line_total_net, 0.0)
        line_total_gross = it.get("line_total_gross")
        line_total_gross_f = None if line_total_gross is None else _to_float(line_total_gross, 0.0)

        # Kompatibilita: legacy mapování dle zadání.
        if unit_price_net_f is None and unit_price_legacy_f is not None:
            unit_price_net_f = unit_price_legacy_f
        if line_total_gross_f is None and line_total_legacy != 0.0:
            line_total_gross_f = line_total_legacy

        # Deterministické dopočty z dostupných dat.
        if line_total_net_f is None and unit_price_net_f is not None:
            line_total_net_f = round(unit_price_net_f * qty, 2)
        if line_total_gross_f is None and unit_price_gross_f is not None:
            line_total_gross_f = round(unit_price_gross_f * qty, 2)
        if line_total_gross_f is None and line_total_net_f is not None:
            line_total_gross_f = round(line_total_net_f * (1.0 + vat_rate / 100.0), 2) if vat_rate > 0 else round(line_total_net_f, 2)
        if line_total_net_f is None and line_total_gross_f is not None:
            line_total_net_f = round(line_total_gross_f / (1.0 + vat_rate / 100.0), 2) if vat_rate > 0 else round(line_total_gross_f, 2)

        if unit_price_gross_f is None and line_total_gross_f is not None and qty != 0.0:
            unit_price_gross_f = round(line_total_gross_f / qty, 4)
        if unit_price_net_f is None and line_total_net_f is not None and qty != 0.0:
            unit_price_net_f = round(line_total_net_f / qty, 4)

        vat_amount = it.get("vat_amount")
        vat_amount_f = None if vat_amount is None else _to_float(vat_amount, 0.0)
        if vat_amount_f is None and (line_total_gross_f is not None and line_total_net_f is not None):
            vat_amount_f = round(line_total_gross_f - line_total_net_f, 2)

        li = LineItem(
            document_id=d.id,
            line_no=line_no,
            name=str(it.get("name") or "").strip()[:512] or f"Položka {line_no}",
            quantity=qty,
            unit_price=unit_price_net_f,
            vat_rate=vat_rate,
            line_total=round(line_total_gross_f, 2) if line_total_gross_f is not None else 0.0,
            ean=_to_str(it.get("ean"), 64),
            item_code=_to_str(it.get("item_code"), 64),
            unit_price_net=unit_price_net_f,
            unit_price_gross=unit_price_gross_f,
            line_total_net=line_total_net_f,
            line_total_gross=line_total_gross_f,
            vat_amount=vat_amount_f,
            vat_code=_to_str(it.get("vat_code"), 32),
        )
        session.add(li)
        line_no += 1

        if line_total_net_f is not None:
            sum_net += float(line_total_net_f)
            has_any_net = True
        if line_total_gross_f is not None:
            sum_gross += float(line_total_gross_f)
            has_any_gross = True

    # Dokumentové agregáty (deterministické, kompatibilní se stávajícím total_with_vat).
    if d.total_without_vat is None:
        d.total_without_vat = round(sum_net, 2) if has_any_net else None
    if d.total_with_vat is None and has_any_gross:
        d.total_with_vat = round(sum_gross, 2)
    if d.total_vat_amount is None and d.total_with_vat is not None and d.total_without_vat is not None:
        d.total_vat_amount = round(float(d.total_with_vat) - float(d.total_without_vat), 2)

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

    # Optional richer FTS for per-item search (used by UI tab "POLOŽKY").
    # Keep backward compatibility with DBs that don't have items_fts2.
    try:
        session.execute(text("DELETE FROM items_fts2 WHERE document_id = :id"), {"id": doc_id})
        for it in items:
            session.execute(
                text(
                    "INSERT INTO items_fts2(item_id, document_id, item_name, supplier_ico, doc_number) "
                    "VALUES(:iid,:did,:name,:ico,:dn)"
                ),
                {
                    "iid": int(it.id),
                    "did": int(doc_id),
                    "name": it.name or "",
                    "ico": row.supplier_ico or "",
                    "dn": row.doc_number or "",
                },
            )
    except Exception:
        pass


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
