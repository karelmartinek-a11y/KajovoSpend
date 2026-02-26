from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from kajovospend.db.migrate import init_db
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.service.processor import Processor


class TestProcessorForceRerun(unittest.TestCase):
    def test_business_duplicate_ignores_same_file_when_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "proc.db"
            engine = make_engine(str(db_path))
            init_db(engine)
            sf = make_session_factory(engine)

            with sf() as session:
                session.execute(
                    text(
                        "INSERT INTO files(id, sha256, original_name, pages, current_path, status, created_at) "
                        "VALUES (1, 'sha-a', 'a.pdf', 1, '/tmp/a.pdf', 'PROCESSED', CURRENT_TIMESTAMP)"
                    )
                )
                session.execute(
                    text(
                        "INSERT INTO files(id, sha256, original_name, pages, current_path, status, created_at) "
                        "VALUES (2, 'sha-b', 'b.pdf', 1, '/tmp/b.pdf', 'PROCESSED', CURRENT_TIMESTAMP)"
                    )
                )
                session.execute(
                    text(
                        "INSERT INTO documents(id, file_id, supplier_ico, doc_number, issue_date, total_with_vat, "
                        "page_from, currency, extraction_confidence, extraction_method, document_text_quality, requires_review, created_at, updated_at) "
                        "VALUES (10, 1, '12345678', 'FV-1', '2025-01-10', 100.0, 1, 'CZK', 1.0, 'offline', 1.0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                session.execute(
                    text(
                        "INSERT INTO documents(id, file_id, supplier_ico, doc_number, issue_date, total_with_vat, "
                        "page_from, currency, extraction_confidence, extraction_method, document_text_quality, requires_review, created_at, updated_at) "
                        "VALUES (11, 2, '12345678', 'FV-2', '2025-01-11', 200.0, 1, 'CZK', 1.0, 'offline', 1.0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                session.commit()

                same_file = Processor._find_business_duplicate(
                    session,
                    supplier_ico="12345678",
                    doc_number="FV-1",
                    issue_date="2025-01-10",
                    exclude_file_id=1,
                )
                self.assertIsNone(same_file)

                same_file_without_exclusion = Processor._find_business_duplicate(
                    session,
                    supplier_ico="12345678",
                    doc_number="FV-1",
                    issue_date="2025-01-10",
                )
                self.assertIsNotNone(same_file_without_exclusion)

                other_file = Processor._find_business_duplicate(
                    session,
                    supplier_ico="12345678",
                    doc_number="FV-2",
                    issue_date="2025-01-11",
                    exclude_file_id=1,
                )
                self.assertIsNotNone(other_file)


if __name__ == "__main__":
    unittest.main()
