from __future__ import annotations

import datetime as dt
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from kajovospend.db.models import Supplier
from kajovospend.db.queries import upsert_supplier
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico


def sync_pending_suppliers(sf, log, *, ttl_hours: float = 24.0, limit: int = 500) -> Dict[str, Any]:
    """Zpracuje pending_ares frontu s TTL gatingem.

    Vrací statistiku běhu (processed/synced/failed/skipped_ttl).
    """
    now = dt.datetime.utcnow()
    out = {"processed": 0, "synced": 0, "failed": 0, "skipped_ttl": 0}

    with sf() as session:  # type: Session
        rows = session.execute(
            select(Supplier)
            .where(Supplier.pending_ares == True)  # noqa: E712
            .order_by(Supplier.ares_last_sync.is_(None).desc(), Supplier.id.asc())
            .limit(int(limit))
        ).scalars().all()

        for sup in rows:
            out["processed"] += 1
            ico = str(getattr(sup, "ico", "") or "").strip()
            if not ico:
                sup.pending_ares = False
                session.add(sup)
                continue

            # TTL gate: pokud je čerstvý, jen odznač pending a nevolej síť.
            fresh = False
            if sup.ares_last_sync is not None:
                try:
                    age_h = (now - sup.ares_last_sync).total_seconds() / 3600.0
                    fresh = age_h <= float(ttl_hours)
                except Exception:
                    fresh = False
            if fresh:
                sup.pending_ares = False
                session.add(sup)
                out["skipped_ttl"] += 1
                continue

            try:
                ico_n = normalize_ico(ico)
                rec = fetch_by_ico(ico_n, timeout=4)
                upsert_supplier(
                    session,
                    rec.ico,
                    name=rec.name,
                    dic=rec.dic,
                    address=rec.address,
                    is_vat_payer=rec.is_vat_payer,
                    ares_last_sync=rec.fetched_at,
                    pending_ares=False,
                    legal_form=rec.legal_form,
                    street=rec.street,
                    street_number=rec.street_number,
                    orientation_number=rec.orientation_number,
                    city=rec.city,
                    zip_code=rec.zip_code,
                    overwrite=True,
                )
                out["synced"] += 1
            except Exception as e:
                sup.pending_ares = True
                session.add(sup)
                out["failed"] += 1
                try:
                    log.warning("sync-ares: selhal supplier=%s ico=%s: %s", sup.id, ico, e)
                except Exception:
                    pass

        session.commit()

    return out
