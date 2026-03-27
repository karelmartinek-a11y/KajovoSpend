from __future__ import annotations

from sqlalchemy.orm import Session

from kajovospend.db import working_models as wm
from kajovospend.db import production_models as pm
from kajovospend.db.production_queries import upsert_supplier as upsert_supplier_prod, insert_document_from_working


class PromotionError(RuntimeError):
    pass


def _is_complete(doc: wm.Document, items: list[wm.LineItem]) -> bool:
    if not items:
        return False
    if not doc.supplier_ico and not doc.supplier_id:
        return False
    if doc.total_with_vat is None:
        return False
    return True


def promote_document(
    work_session: Session,
    prod_session: Session,
    document_id: int,
) -> pm.Document | None:
    """
    Deterministic, idempotent promotion of a working document into production DB.
    Returns production Document or None if skipped (incomplete).
    """
    doc = work_session.get(wm.Document, document_id)
    if doc is None:
        raise PromotionError(f"Document {document_id} not found in working DB")
    items = list(doc.items)
    if not _is_complete(doc, items):
        return None

    supplier = None
    if doc.supplier_ico:
        supplier = upsert_supplier_prod(
            prod_session,
            doc.supplier_ico,
            name=doc.supplier.name if doc.supplier else None,
            dic=doc.supplier.dic if doc.supplier else None,
            address=doc.supplier.address if doc.supplier else None,
            is_vat_payer=doc.supplier.is_vat_payer if doc.supplier else None,
            legal_form=doc.supplier.legal_form if doc.supplier else None,
            street=doc.supplier.street if doc.supplier else None,
            street_number=doc.supplier.street_number if doc.supplier else None,
            orientation_number=doc.supplier.orientation_number if doc.supplier else None,
            city=doc.supplier.city if doc.supplier else None,
            zip_code=doc.supplier.zip_code if doc.supplier else None,
            pending_ares=doc.supplier.pending_ares if doc.supplier else None,
        )

    # Přenášíme i zdrojový file_id, aby šlo produkční doklad dohledat zpět na working soubor.
    prod_doc = insert_document_from_working(prod_session, supplier, doc, items)
    prod_session.commit()
    return prod_doc
