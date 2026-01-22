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

    # 1) pokus: přesná shoda na uložené IČO
    s = session.execute(select(Supplier).where(Supplier.ico == ico_norm)).scalar_one_or_none()

    # 2) fallback: v DB může být IČO historicky uloženo s mezerami/znaky -> match přes normalizaci
    if s is None and ico_norm:
        candidates = session.execute(select(Supplier).where(Supplier.ico.is_not(None))).scalars().all()
        for cand in candidates:
            if _normalize_ico_soft(cand.ico) == ico_norm:
                s = cand
                break

    if not s:
        s = Supplier(ico=ico_norm)
        session.add(s)
    else:
        # kanonizace IČO v DB (pokud doteď bylo třeba s mezerami)
        s.ico = ico_norm

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
