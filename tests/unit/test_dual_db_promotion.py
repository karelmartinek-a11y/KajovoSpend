import datetime as dt
from pathlib import Path

from sqlalchemy import text

from kajovospend.db.working_session import create_working_session_factory
from kajovospend.db.production_session import create_production_session_factory
from kajovospend.db.production_models import Document as ProdDocument, Supplier as ProdSupplier, LineItem as ProdLineItem
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

    with p_sf() as ps:
        docs = ps.execute(text("SELECT count(*) FROM documents")).scalar_one()
        items = ps.execute(text("SELECT count(*) FROM items")).scalar_one()
        assert docs == 1
        assert items == 1


def test_promotion_keeps_source_file_link(tmp_path: Path):
    wdb = tmp_path / "w.sqlite"
    pdb = tmp_path / "p.sqlite"
    w_sf = create_working_session_factory(wdb)
    p_sf = create_production_session_factory(pdb)

    with w_sf() as ws:
        sup = upsert_supplier(ws, "12345678", name="Test s.r.o.")
        f = create_file_record(ws, "sha-src", "a.pdf", "/tmp/a.pdf", 1, "PROCESSED")
        doc = add_document(
            ws,
            file_id=f.id,
            supplier_id=sup.id,
            supplier_ico="12345678",
            doc_number="FV-2",
            bank_account=None,
            issue_date=dt.date(2025, 1, 2),
            total_with_vat=200.0,
            currency="CZK",
            confidence=1.0,
            method="offline",
            requires_review=False,
            review_reasons=None,
            items=[{"name": "X", "quantity": 1, "line_total": 200.0, "vat_rate": 0.0}],
        )
        ws.commit()

        with p_sf() as ps:
            promoted = promote_document(ws, ps, doc.id)
            ps.commit()

            assert promoted is not None
            assert promoted.file_id == f.id

            details = ps.execute(text("SELECT file_id FROM documents WHERE id = :id"), {"id": promoted.id}).scalar_one()
            assert details == f.id


def test_promotion_repairs_missing_source_file_link(tmp_path: Path):
    wdb = tmp_path / "w.sqlite"
    pdb = tmp_path / "p.sqlite"
    w_sf = create_working_session_factory(wdb)
    p_sf = create_production_session_factory(pdb)

    with w_sf() as ws:
        sup = upsert_supplier(ws, "12345678", name="Test s.r.o.")
        f = create_file_record(ws, "sha-repair", "a.pdf", "/tmp/a.pdf", 1, "PROCESSED")
        doc = add_document(
            ws,
            file_id=f.id,
            supplier_id=sup.id,
            supplier_ico="12345678",
            doc_number="FV-3",
            bank_account=None,
            issue_date=dt.date(2025, 1, 3),
            total_with_vat=300.0,
            currency="CZK",
            confidence=1.0,
            method="offline",
            requires_review=False,
            review_reasons=None,
            items=[{"name": "X", "quantity": 1, "line_total": 300.0, "vat_rate": 0.0}],
        )
        ws.commit()

        with p_sf() as ps:
            prod_sup = ProdSupplier(ico="12345678", name="Test s.r.o.")
            ps.add(prod_sup)
            ps.flush()
            broken = ProdDocument(
                file_id=None,
                supplier_id=prod_sup.id,
                supplier_ico="12345678",
                doc_number="FV-3",
                issue_date=dt.date(2025, 1, 3),
                total_with_vat=300.0,
                currency="CZK",
            )
            ps.add(broken)
            ps.flush()
            ps.add(ProdLineItem(document_id=broken.id, line_no=1, name="X", quantity=1, vat_rate=0.0, line_total=300.0))
            ps.commit()

            promoted = promote_document(ws, ps, doc.id)
            ps.commit()

            assert promoted is not None
            assert promoted.id == broken.id
            assert promoted.file_id == f.id
