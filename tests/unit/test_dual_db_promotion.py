from pathlib import Path

from sqlalchemy.orm import Session

from kajovospend.db.working_session import create_working_session_factory
from kajovospend.db.production_session import create_production_session_factory
from kajovospend.db.working_queries import add_document, create_file_record, upsert_supplier
from kajovospend.service.promotion import promote_document


def test_promotion_idempotent(tmp_path: Path):
    wdb = tmp_path / "w.sqlite"
    pdb = tmp_path / "p.sqlite"
    w_sf = create_working_session_factory(wdb)
    p_sf = create_production_session_factory(pdb)

    with w_sf() as ws:
        sup = upsert_supplier(ws, "12345678", name="Test s.r.o.")
        f = create_file_record(ws, "sha", "a.pdf", "/tmp/a.pdf", 1, "PROCESSED")
        doc = add_document(
            ws,
            file_id=f.id,
            supplier_id=sup.id,
            supplier_ico="12345678",
            doc_number="FV-1",
            bank_account=None,
            issue_date=__import__("datetime").date(2025, 1, 1),
            total_with_vat=100.0,
            currency="CZK",
            confidence=1.0,
            method="offline",
            requires_review=False,
            review_reasons=None,
            items=[{"name": "X", "quantity": 1, "line_total": 100.0, "vat_rate": 0.0}],
        )
        ws.commit()
        with p_sf() as ps:
            res1 = promote_document(ws, ps, doc.id)
            ps.commit()
            res2 = promote_document(ws, ps, doc.id)
            ps.commit()
            assert res1.id == res2.id

    from sqlalchemy import text
    with p_sf() as ps:
        docs = ps.execute(text("SELECT count(*) FROM documents")).scalar_one()
        items = ps.execute(text("SELECT count(*) FROM items")).scalar_one()
        assert docs == 1
        assert items == 1
