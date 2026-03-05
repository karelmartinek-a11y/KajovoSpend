from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from kajovospend.db.migrate import init_db
from kajovospend.db.session import make_engine


class TestStandardReceiptTemplatesMigration(unittest.TestCase):
    def test_init_db_creates_template_table_with_required_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "templates.db"
            engine = make_engine(str(db_path))
            try:
                init_db(engine)
                with engine.begin() as con:
                    cols = {row[1] for row in con.execute(text("PRAGMA table_info('standard_receipt_templates')")).fetchall()}
                for expected in (
                    "id",
                    "name",
                    "schema_json",
                    "match_supplier_ico_norm",
                    "match_texts_json",
                    "sample_file_relpath",
                ):
                    self.assertIn(expected, cols)
            finally:
                engine.dispose()

    def test_init_db_is_idempotent_for_templates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "templates_idempotent.db"
            engine = make_engine(str(db_path))
            try:
                init_db(engine)
                init_db(engine)
                with engine.begin() as con:
                    idx_count = int(
                        con.execute(
                            text(
                                "SELECT COUNT(*) FROM sqlite_master "
                                "WHERE type='index' AND name='idx_standard_receipt_templates_name'"
                            )
                        ).scalar_one() or 0
                    )
                    self.assertEqual(idx_count, 1)
                    tbl_count = int(
                        con.execute(
                            text(
                                "SELECT COUNT(*) FROM sqlite_master "
                                "WHERE type='table' AND name='standard_receipt_templates'"
                            )
                        ).scalar_one() or 0
                    )
                    self.assertEqual(tbl_count, 1)
            finally:
                engine.dispose()
