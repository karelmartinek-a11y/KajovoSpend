from __future__ import annotations

import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from tests.gui.smoke_support import run_gui_audit, run_import_smoke, write_smoke_fixture_pdf


class TestGuiSmokeInfra(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_fixture_pdf_writer_creates_valid_pdf(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            pdf = write_smoke_fixture_pdf(Path(td) / "smoke.pdf")
            self.assertTrue(pdf.exists())
            self.assertGreater(pdf.stat().st_size, 100)
            self.assertEqual(pdf.suffix.lower(), ".pdf")

    def test_import_smoke_finishes_in_processed_state(self) -> None:
        result = run_import_smoke(workspace_name="kajovospend-test-import-smoke")
        self.assertEqual(result["status"], "PROCESSED")
        self.assertTrue(result["document_ids"])
        self.assertTrue(Path(result["fixture_pdf"]).exists())

    def test_gui_audit_captures_tabs_and_dialogs(self) -> None:
        report = run_gui_audit(workspace_name="kajovospend-test-gui-audit")
        self.assertGreaterEqual(report["summary"]["tab_count"], 8)
        self.assertGreaterEqual(report["summary"]["dialog_count"], 3)
        self.assertTrue(report["screenshots"])
        for shot in report["screenshots"]:
            self.assertTrue(Path(shot).exists())


if __name__ == "__main__":
    unittest.main()
