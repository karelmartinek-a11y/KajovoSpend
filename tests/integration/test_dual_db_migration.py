from pathlib import Path

from sqlalchemy import text

from kajovospend.db.migrate import init_db
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.db.dual_db_migrate import migrate_legacy_single_db, MigrationError
from kajovospend.db.production_session import create_production_session_factory
from kajovospend.db.working_session import create_working_session_factory


def _seed_legacy(db_path: Path):
    eng = make_engine(str(db_path))
    init_db(eng)
    sf = make_session_factory(eng)
    with sf() as s:
        s.execute(
            text(
                "INSERT INTO suppliers(id, ico, ico_norm, name, pending_ares) VALUES (1, '12345678', '12345678', 'Legacy s.r.o.', 0)"
            )
        )
        s.execute(
            text(
                "INSERT INTO files(id, sha256, original_name, pages, current_path, status, created_at) "
                "VALUES (1, 'sha', 'a.pdf', 1, '/tmp/a.pdf', 'PROCESSED', CURRENT_TIMESTAMP)"
            )
        )
        s.execute(
            text(
                "INSERT INTO documents(id, file_id, supplier_id, supplier_ico, doc_number, issue_date, total_with_vat, page_from, currency, extraction_confidence, extraction_method, document_text_quality, requires_review, created_at, updated_at) "
                "VALUES (10, 1, 1, '12345678', 'FV-1', '2025-01-10', 100.0, 1, 'CZK', 1.0, 'offline', 1.0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        s.execute(
            text(
                "INSERT INTO items(id, document_id, line_no, name, quantity, vat_rate, line_total) "
                "VALUES (20, 10, 1, 'Item', 1, 0, 100.0)"
            )
        )
        s.commit()


def test_migration_splits_legacy(tmp_path: Path):
    legacy = tmp_path / "legacy.sqlite"
    working = tmp_path / "w.sqlite"
    prod = tmp_path / "p.sqlite"
    _seed_legacy(legacy)

    migrate_legacy_single_db(legacy, working, prod)

    w_sf = create_working_session_factory(working)
    p_sf = create_production_session_factory(prod)
    with w_sf() as ws, p_sf() as ps:
        files = ws.execute(text("SELECT count(*) FROM files")).scalar_one()
        assert files == 1
        docs_w = ws.execute(text("SELECT count(*) FROM documents")).scalar_one()
        assert docs_w == 0  # documents should be in production only
        docs_p = ps.execute(text("SELECT count(*) FROM documents")).scalar_one()
        items_p = ps.execute(text("SELECT count(*) FROM items")).scalar_one()
        assert docs_p == 1
        assert items_p == 1


def test_migration_refuses_nonempty_targets(tmp_path: Path):
    legacy = tmp_path / "legacy.sqlite"
    working = tmp_path / "w.sqlite"
    prod = tmp_path / "p.sqlite"
    _seed_legacy(legacy)
    migrate_legacy_single_db(legacy, working, prod)
    # second legacy seed should fail because targets now populated differently
    with open(tmp_path / "legacy2.sqlite", "w"):
        pass
    try:
        migrate_legacy_single_db(legacy, working, prod)
    except MigrationError:
        return
    assert False, "expected MigrationError"
