from __future__ import annotations

import unittest
from pathlib import Path

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QHeaderView, QTableView

from tests.gui.smoke_support import (
    SelectionTruthEvidence,
    _audit_table_headers,
    evaluate_selection_truth,
    run_gui_audit,
    run_import_smoke,
    write_smoke_fixture_pdf,
)


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

    def test_import_smoke_covers_multiple_scenarios(self) -> None:
        result = run_import_smoke(workspace_name="kajovospend-test-import-smoke")
        self.assertEqual(result["status"], "PASS")
        self.assertGreaterEqual(result["summary"]["case_count"], 5)
        self.assertTrue(result["document_ids"])
        self.assertTrue(Path(result["fixture_pdf"]).exists())
        case_names = {case["name"] for case in result["cases"]}
        self.assertTrue({"embedded_success", "quarantine_case", "duplicate_case", "ares_failure_case"}.issubset(case_names))

        embedded = next(case for case in result["cases"] if case["name"] == "embedded_success")
        self.assertEqual(embedded["status"], "PROCESSED")
        duplicate = next(case for case in result["cases"] if case["name"] == "duplicate_case")
        self.assertEqual(duplicate["status"], "DUPLICATE")
        quarantine = next(case for case in result["cases"] if case["name"] == "quarantine_case")
        self.assertEqual(quarantine["status"], "QUARANTINE")
        ares_failure = next(case for case in result["cases"] if case["name"] == "ares_failure_case")
        self.assertEqual(ares_failure["status"], "QUARANTINE")

        if result["support"]["ocr_engine"]:
            ocr_case = next(case for case in result["cases"] if case["name"] == "ocr_path")
            template_case = next(case for case in result["cases"] if case["name"] == "template_path")
            self.assertIn(ocr_case["status"], {"PROCESSED", "QUARANTINE"})
            self.assertIn(template_case["status"], {"PROCESSED", "QUARANTINE"})
        else:
            ocr_case = next(case for case in result["cases"] if case["name"] == "ocr_path")
            template_case = next(case for case in result["cases"] if case["name"] == "template_path")
            self.assertEqual(ocr_case["status"], "SKIPPED")
            self.assertEqual(template_case["status"], "SKIPPED")

    def test_gui_audit_captures_tabs_and_dialogs(self) -> None:
        report = run_gui_audit(workspace_name="kajovospend-test-gui-audit")
        self.assertGreaterEqual(report["summary"]["tab_count"], 8)
        self.assertGreaterEqual(report["summary"]["dialog_count"], 3)
        self.assertGreaterEqual(report["summary"]["import_case_count"], 5)
        self.assertIn("populated_state", report)
        self.assertIn("docs", report["populated_state"])
        self.assertIn("items", report["populated_state"])
        self.assertTrue(report["populated_state"]["screenshots"])
        self.assertTrue(report["screenshots"])
        for shot in report["screenshots"]:
            self.assertTrue(Path(shot).exists())
        for shot in report["populated_state"]["screenshots"]:
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

    def test_truth_guard_detects_old_backend_empty_state(self) -> None:
        old_bad = SelectionTruthEvidence(
            scope="docs",
            tab="ÚČTENKY",
            row_index=0,
            backend_id=1,
            backend_file_id=None,
            backend_path=None,
            ui_source_text="",
            action_enabled=True,
            preview_has_pixmap=False,
            preview_has_scene_items=False,
        )
        findings = evaluate_selection_truth(old_bad)
        kinds = {item.kind for item in findings}
        self.assertIn("action_enabled_without_backend_path", kinds)
        self.assertNotIn("source_line_claims_path_without_backend", kinds)

    def test_truth_guard_accepts_consistent_state(self) -> None:
        good = SelectionTruthEvidence(
            scope="items",
            tab="POLOŽKY",
            row_index=0,
            backend_id=9,
            backend_file_id=3,
            backend_path="C:/temp/doc.pdf",
            ui_source_text="C:/temp/doc.pdf",
            action_enabled=True,
            preview_has_pixmap=True,
            preview_has_scene_items=True,
        )
        findings = evaluate_selection_truth(good)
        self.assertFalse(findings)


if __name__ == "__main__":
    unittest.main()
