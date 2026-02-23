from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text, select

from kajovospend.db.migrate import init_db
from kajovospend.db.models import Document, LineItem
from kajovospend.db.queries import add_document
from kajovospend.db.session import make_engine, make_session_factory


class TestDbNetGrossMigration(unittest.TestCase):
    def test_init_db_backfills_legacy_columns_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            engine = make_engine(str(db_path))

            with engine.begin() as con:
                # Simulace legacy DB před PULS-001 (jen minimální nutné tabulky/sloupce).
                con.execute(text("CREATE TABLE suppliers (id INTEGER PRIMARY KEY, ico TEXT, ico_norm TEXT)"))
                con.execute(text("CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT, sha256 TEXT, original_name TEXT, pages INTEGER, current_path TEXT, created_at TEXT, processed_at TEXT, mime_type TEXT, last_error TEXT)"))
                con.execute(text("CREATE TABLE documents (id INTEGER PRIMARY KEY, file_id INTEGER, supplier_id INTEGER, supplier_ico TEXT, doc_number TEXT, bank_account TEXT, issue_date TEXT, total_with_vat REAL, page_from INTEGER, page_to INTEGER, currency TEXT, extraction_confidence REAL, extraction_method TEXT, document_text_quality REAL, openai_model TEXT, openai_raw_response TEXT, requires_review INTEGER, review_reasons TEXT, created_at TEXT, updated_at TEXT)"))
                con.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, document_id INTEGER, line_no INTEGER, name TEXT, quantity REAL, unit_price REAL, vat_rate REAL, line_total REAL, ean TEXT, item_code TEXT)"))
                con.execute(text("CREATE TABLE import_jobs (id INTEGER PRIMARY KEY, created_at TEXT, started_at TEXT, finished_at TEXT, path TEXT, sha256 TEXT, status TEXT, error TEXT)"))
                con.execute(text("CREATE TABLE service_state (singleton INTEGER PRIMARY KEY, running INTEGER, last_success TEXT, last_error TEXT, last_error_at TEXT, queue_size INTEGER, last_seen TEXT)"))

                con.execute(text("INSERT INTO files(id, status, sha256, original_name, pages, current_path) VALUES (1, 'PROCESSED', 'x', 'a.pdf', 1, '/tmp/a.pdf')"))
                con.execute(text("INSERT INTO documents(id, file_id, supplier_ico, doc_number, total_with_vat, page_from, currency, extraction_confidence, extraction_method, requires_review) VALUES (1, 1, '12345678', '2025-1', 121.00, 1, 'CZK', 1.0, 'offline', 0)"))
                con.execute(text("INSERT INTO items(document_id, line_no, name, quantity, unit_price, vat_rate, line_total) VALUES (1, 1, 'A', 2.0, 50.0, 21.0, 121.0)"))

            init_db(engine)

            with engine.begin() as con:
                row = con.execute(text("SELECT unit_price_net, line_total_gross, line_total_net, vat_amount, unit_price_gross FROM items WHERE document_id=1 AND line_no=1")).fetchone()
                self.assertIsNotNone(row)
                self.assertAlmostEqual(float(row[0]), 50.0, places=4)  # legacy unit_price -> unit_price_net
                self.assertAlmostEqual(float(row[1]), 121.0, places=2)  # legacy line_total -> line_total_gross
                self.assertAlmostEqual(float(row[2]), 100.0, places=2)
                self.assertAlmostEqual(float(row[3]), 21.0, places=2)
                self.assertAlmostEqual(float(row[4]), 60.5, places=4)

                drow = con.execute(text("SELECT total_without_vat, total_vat_amount, doc_type FROM documents WHERE id=1")).fetchone()
                self.assertIsNotNone(drow)
                self.assertAlmostEqual(float(drow[0]), 100.0, places=2)
                self.assertAlmostEqual(float(drow[1]), 21.0, places=2)
                self.assertEqual(str(drow[2]), "invoice")

    def test_add_document_maps_legacy_and_fills_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "new.db"
            engine = make_engine(str(db_path))
            init_db(engine)
            sf = make_session_factory(engine)

            with sf() as session:
                session.execute(text("INSERT INTO files(id, sha256, original_name, pages, current_path, status) VALUES (1, 's', 'x.pdf', 1, '/tmp/x.pdf', 'PROCESSED')"))
                session.flush()

                doc = add_document(
                    session,
                    file_id=1,
                    supplier_id=None,
                    supplier_ico="12345678",
                    doc_number="FV-1",
                    bank_account=None,
                    issue_date=None,
                    total_with_vat=242.0,
                    currency="CZK",
                    confidence=1.0,
                    method="offline",
                    requires_review=False,
                    review_reasons=None,
                    items=[
                        {"name": "Položka", "quantity": 2, "unit_price": 100.0, "vat_rate": 21.0, "line_total": 242.0}
                    ],
                )
                session.commit()

                doc_db = session.execute(select(Document).where(Document.id == doc.id)).scalar_one()
                self.assertAlmostEqual(float(doc_db.total_without_vat or 0.0), 200.0, places=2)
                self.assertAlmostEqual(float(doc_db.total_vat_amount or 0.0), 42.0, places=2)
                self.assertEqual(doc_db.doc_type, "invoice")

                item = session.execute(select(LineItem).where(LineItem.document_id == doc.id)).scalar_one()
                self.assertAlmostEqual(float(item.unit_price_net or 0.0), 100.0, places=4)
                self.assertAlmostEqual(float(item.line_total_gross or 0.0), 242.0, places=2)
                self.assertAlmostEqual(float(item.line_total_net or 0.0), 200.0, places=2)
                self.assertAlmostEqual(float(item.vat_amount or 0.0), 42.0, places=2)


if __name__ == "__main__":
    unittest.main()
