from __future__ import annotations

import unittest
from pathlib import Path

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QHeaderView, QTableView

from tests.gui.smoke_support import _audit_table_headers, run_gui_audit, run_import_smoke, write_smoke_fixture_pdf


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

    def test_header_audit_detects_too_narrow_sections(self) -> None:
        table = QTableView()
        table.setObjectName("SyntheticHeaderAuditTable")
        model = QStandardItemModel(1, 2)
        model.setHorizontalHeaderLabels(["Velmi dlouhá hlavička sloupce", "Krátká"])
        model.setItem(0, 0, QStandardItem("hodnota"))
        model.setItem(0, 1, QStandardItem("x"))
        table.setModel(model)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        table.setColumnWidth(0, 60)
        table.setColumnWidth(1, 80)
        table.resize(220, 120)
        table.show()
        self._app.processEvents()

        incidents = _audit_table_headers(table)
        self.assertTrue(any(incident.kind == "header_overflow" for incident in incidents))
        self.assertTrue(any("Velmi dlouhá hlavička" in incident.text for incident in incidents))

    def test_gui_audit_reports_no_header_overflow_for_main_window(self) -> None:
        report = run_gui_audit(workspace_name="kajovospend-test-gui-audit-headers")
        incidents = [
            incident
            for artifact in [*report["tabs"], *report["dialogs"]]
            for incident in artifact["incidents"]
            if incident["kind"] in {"header_overflow", "header_viewport_overflow", "header_not_visible"}
        ]
        self.assertFalse(incidents, incidents)


if __name__ == "__main__":
    unittest.main()
