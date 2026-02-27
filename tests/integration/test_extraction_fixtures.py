from __future__ import annotations

import logging
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select

from kajovospend.db.migrate import init_db
from kajovospend.db.models import Document, LineItem
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.service.processor import Processor
from kajovospend.utils.paths import resolve_app_paths


def _make_minimal_pdf_with_text(text: str) -> bytes:
    # Minimal single-page PDF with embedded text that pypdf can extract.
    s = (text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 10 Tf 50 750 Td ({s}) Tj ET"

    def obj(n: int, body: str) -> str:
        return f"{n} 0 obj\n{body}\nendobj\n"

    header = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objs: List[str] = []
    objs.append(obj(1, "<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append(obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"))
    objs.append(
        obj(
            3,
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >> >>",
        )
    )
    stream_len = len(content.encode("latin1"))
    objs.append(f"4 0 obj\n<< /Length {stream_len} >>\nstream\n{content}\nendstream\nendobj\n")
    objs.append(obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    # xref offsets
    offsets: List[int] = [0]
    cur = len(header.encode("latin1"))
    for o in objs:
        offsets.append(cur)
        cur += len(o.encode("latin1"))

    xref_start = cur
    xref = "xref\n0 6\n"
    xref += "0000000000 65535 f \n"
    for off in offsets[1:]:
        xref += f"{off:010d} 00000 n \n"

    trailer = "trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_start) + "\n%%EOF\n"
    return (header + "".join(objs) + xref + trailer).encode("latin1")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.lines.append(str(record.getMessage()))


class TestExtractionFixturesHarness(unittest.TestCase):
    def test_pdf_embedded_path_logs_and_extracts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            out_dir = root / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            db_path = root / "test_extract.db"

            # Minimal cfg for Processor
            cfg: Dict[str, Any] = {
                "app": {"data_dir": str(data_dir), "db_path": str(db_path), "log_dir": str(root / "logs")},
                "paths": {"output_dir": str(out_dir), "duplicate_dir_name": "DUPLICITY", "quarantine_dir_name": "KARANTENA"},
                "ocr": {"min_confidence": 0.65, "pdf_dpi": 200},
            }
            paths = resolve_app_paths(cfg["app"]["data_dir"], cfg["app"]["db_path"], cfg["app"]["log_dir"], cfg["ocr"].get("models_dir"))

            log = logging.getLogger("kajovospend_test_fixture")
            log.setLevel(logging.DEBUG)
            log.propagate = False
            h = _ListHandler()
            h.setLevel(logging.DEBUG)
            h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            log.addHandler(h)

            engine = make_engine(str(paths.db_path))
            init_db(engine)
            sf = make_session_factory(engine)

            # Embedded-text PDF that should parse basic fields + 1 rounding item
            txt = "\n".join(
                [
                    "ICO: 12345678",
                    "VS: 2025001",
                    "Datum vystaveni: 01.01.2025",
                    "Cena celkem 100,00 CZK",
                    "Zaokrouhleni 100,00",
                ]
            )
            pdf_path = root / "fixture1.pdf"
            pdf_path.write_bytes(_make_minimal_pdf_with_text(txt))

            proc = Processor(cfg, paths, log)
            with patch("kajovospend.service.processor.fetch_by_ico") as fetch_ares:
                from kajovospend.integrations.ares import AresRecord

                fetch_ares.return_value = AresRecord(
                    ico="12345678",
                    name="ACME s.r.o.",
                    legal_form="společnost s ručením omezeným",
                    is_vat_payer=True,
                    address="U Testu 1, Praha 1, 11000",
                    street="U Testu",
                    street_number="1",
                    city="Praha",
                    zip_code="11000",
                )
                with sf() as session:
                    res = proc.process_path(session, pdf_path)
                    session.commit()

                    self.assertIn(res.get("status"), {"PROCESSED", "QUARANTINE"})
                    self.assertEqual(res.get("text_method"), "embedded")

                    doc_ids = list(res.get("document_ids") or [])
                    self.assertTrue(doc_ids, "expected at least 1 extracted document")

                    doc = session.execute(select(Document).where(Document.id == int(doc_ids[0]))).scalar_one()
                    items = session.execute(select(LineItem).where(LineItem.document_id == doc.id)).scalars().all()
                    self.assertTrue(doc.supplier_ico)
                    self.assertTrue(doc.doc_number)
                    self.assertIsNotNone(doc.issue_date)
                    self.assertIsNotNone(doc.total_with_vat)
                    self.assertGreaterEqual(len(items), 1)

            proc.close()
            engine.dispose()

            # Verify decision log exists
            joined = "\n".join(h.lines)
            self.assertIn("PDF text source: embedded", joined)
            self.assertIn("quality=", joined)


if __name__ == "__main__":
    unittest.main()
