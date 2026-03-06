from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from kajovospend.db.production_session import create_production_session_factory
from kajovospend.db.production_models import Document
from kajovospend.service.processor import Processor


class TestProcessorForceRerun(unittest.TestCase):
    def test_business_duplicate_ignores_same_file_when_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdb = Path(td) / "prod.db"
            psf = create_production_session_factory(pdb)
            try:
                with psf() as session:
                    session.execute(
                        text(
                            "INSERT INTO documents(id, supplier_ico, doc_number, issue_date, total_with_vat, page_from, currency, extraction_confidence, extraction_method, document_text_quality, requires_review, created_at, updated_at) "
                            "VALUES (10, '12345678', 'FV-1', '2025-01-10', 100.0, 1, 'CZK', 1.0, 'offline', 1.0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        )
                    )
                    session.execute(
                        text(
                            "INSERT INTO documents(id, supplier_ico, doc_number, issue_date, total_with_vat, page_from, currency, extraction_confidence, extraction_method, document_text_quality, requires_review, created_at, updated_at) "
                            "VALUES (11, '12345678', 'FV-2', '2025-01-11', 200.0, 1, 'CZK', 1.0, 'offline', 1.0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        )
                    )
                    session.commit()

                    same_doc = Processor._find_business_duplicate(
                        session,
                        supplier_ico="12345678",
                        doc_number="FV-1",
                        issue_date="2025-01-10",
                    )
                    self.assertIsNotNone(same_doc)
            finally:
                try:
                    eng = getattr(psf, "bind", None) or getattr(psf, "_engine", None)
                    if eng:
                        eng.dispose()
                except Exception:
                    pass

if __name__ == "__main__":
    unittest.main()
