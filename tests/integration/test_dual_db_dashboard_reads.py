from pathlib import Path

import pytest

from kajovospend.db.production_session import create_production_session_factory
from kajovospend.db.working_session import create_working_session_factory
from kajovospend.db.migrate import init_production_db, init_working_db
from kajovospend.db.production_models import Supplier as ProdSupplier, Document as ProdDocument, LineItem as ProdLineItem
from kajovospend.db.working_models import DocumentFile, Document as WorkDocument, LineItem as WorkLineItem
from kajovospend.db.dual_db_guard import DualDbConfigError, ensure_separate_databases
from kajovospend.ui import db_api
from kajovospend.utils.forensic_dual_db import canonical_db_path, assert_separate_sessions, snapshot_counts


def _seed_working(sf):
    with sf() as s:
        f = DocumentFile(
            sha256="w_sha",
            original_name="w.pdf",
            pages=1,
            current_path="/tmp/w.pdf",
            status="QUARANTINE",
        )
        s.add(f)
        s.flush()
        doc = WorkDocument(
            file_id=f.id,
            supplier_ico="99999999",
            doc_number="W-1",
            issue_date=None,
            total_with_vat=5.0,
            currency="CZK",
        )
        s.add(doc)
        s.flush()
        s.add(WorkLineItem(document_id=doc.id, line_no=1, name="Work item", quantity=1, vat_rate=0, line_total=5.0))
        s.commit()


def _seed_production(sf):
    with sf() as s:
        sup = ProdSupplier(ico="12345678", name="Prod s.r.o.")
        s.add(sup)
        s.flush()
        doc = ProdDocument(
            supplier_id=sup.id,
            supplier_ico=sup.ico,
            doc_number="P-1",
            issue_date=None,
            total_with_vat=100.0,
            total_without_vat=80.0,
            currency="CZK",
        )
        s.add(doc)
        s.flush()
        s.add(
            ProdLineItem(
                document_id=doc.id,
                line_no=1,
                name="Prod item",
                quantity=2,
                vat_rate=20.0,
                line_total=100.0,
                line_total_net=80.0,
            )
        )
        s.commit()


def test_dashboard_reads_from_production(tmp_path: Path):
    w_db = tmp_path / "working.sqlite"
    p_db = tmp_path / "production.sqlite"

    w_sf = create_working_session_factory(w_db)
    p_sf = create_production_session_factory(p_db)
    init_working_db(getattr(w_sf, "_engine", None) or getattr(w_sf, "bind", None))
    init_production_db(getattr(p_sf, "_engine", None) or getattr(p_sf, "bind", None))
    _seed_working(w_sf)
    _seed_production(p_sf)

    with p_sf() as ps, w_sf() as ws:
        assert canonical_db_path(ps) != canonical_db_path(ws)
        counts = db_api.production_counts(ps)
        assert counts["documents"] == 1
        assert counts["items"] == 1

        dash = db_api.dashboard_counts(ps, working_session=ws)
        assert dash["processed"] == 1
        assert dash["quarantine"] == 1  # from working DB

        stats = db_api.run_stats(ps)
        assert stats["receipts"] == 1
        assert stats["items"] == 1

        docs = db_api.list_documents(ps, working_session=ws)
        assert len(docs) == 1
        assert docs[0][0].doc_number == "P-1"

        items = db_api.list_items(ps, working_session=ws)
        assert len(items) == 1
        assert items[0]["doc_number"] == "P-1"
        assert items[0]["supplier_ico"] == "12345678"

        snap = snapshot_counts(ws, ps)
        assert snap["working_documents"] == 1
        assert snap["production_documents"] == 1


def test_guard_rejects_same_path(tmp_path: Path):
    db = tmp_path / "same.sqlite"
    db.touch()
    with pytest.raises(DualDbConfigError):
        ensure_separate_databases(str(db), str(db))
