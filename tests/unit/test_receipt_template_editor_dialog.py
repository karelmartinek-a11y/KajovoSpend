from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from kajovospend.ui.receipt_template_editor import ReceiptTemplateEditorDialog, RoiRecord


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestReceiptTemplateEditorDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_dialog_builds_without_crash(self) -> None:
        paths = SimpleNamespace(data_dir=Path("."), models_dir=Path("."))
        dlg = ReceiptTemplateEditorDialog(paths=paths, cfg={})
        try:
            dlg.btn_fit_width.click()
            self.assertIsNotNone(dlg.canvas)
        finally:
            dlg.deleteLater()

    def test_prepare_sample_info_allows_same_source_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            pdf = data_dir / "templates" / "abc" / "sample.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.4\n%dummy\n")
            paths = SimpleNamespace(data_dir=data_dir, models_dir=data_dir)
            dlg = ReceiptTemplateEditorDialog(paths=paths, cfg={})
            try:
                dlg._sample_pdf_path = pdf
                dlg._sample_folder_relpath = Path("templates") / "abc"
                name, sha, rel = dlg._prepare_sample_info()
                self.assertEqual(name, "sample.pdf")
                self.assertTrue(bool(sha))
                self.assertEqual(rel, "templates/abc/sample.pdf")
            finally:
                dlg.deleteLater()

    def test_prepare_sample_info_copies_external_pdf_into_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as data_td, tempfile.TemporaryDirectory() as src_td:
            data_dir = Path(data_td)
            src_pdf = Path(src_td) / "source.pdf"
            src_pdf.write_bytes(b"%PDF-1.4\n%external\n")
            paths = SimpleNamespace(data_dir=data_dir, models_dir=data_dir)
            dlg = ReceiptTemplateEditorDialog(paths=paths, cfg={})
            try:
                dlg._sample_pdf_path = src_pdf
                name, sha, rel = dlg._prepare_sample_info()
                self.assertEqual(name, "source.pdf")
                self.assertTrue(bool(sha))
                self.assertTrue(bool(rel))
                stored = data_dir / Path(rel)
                self.assertTrue(stored.exists())
                self.assertEqual(stored.read_bytes(), src_pdf.read_bytes())
            finally:
                dlg.deleteLater()

    def test_load_template_recovers_sample_by_name_from_templates_storage(self) -> None:
        with tempfile.TemporaryDirectory() as data_td:
            data_dir = Path(data_td)
            stored_pdf = data_dir / "templates" / "saved" / "sample.pdf"
            stored_pdf.parent.mkdir(parents=True, exist_ok=True)
            stored_pdf.write_bytes(b"%PDF-1.4\n%stored\n")
            template = {
                "name": "X",
                "sample_file_name": "sample.pdf",
                "sample_file_relpath": "C:/missing/path/sample.pdf",
                "schema_json": "",
            }
            paths = SimpleNamespace(data_dir=data_dir, models_dir=data_dir)
            with patch.object(ReceiptTemplateEditorDialog, "_load_pdf", autospec=True) as mocked_load_pdf:
                dlg = ReceiptTemplateEditorDialog(paths=paths, cfg={}, template=template)
            try:
                self.assertEqual(dlg._sample_pdf_path, stored_pdf)
                mocked_load_pdf.assert_called_once()
            finally:
                dlg.deleteLater()

    def test_roi_changed_does_not_require_replace_confirmation(self) -> None:
        paths = SimpleNamespace(data_dir=Path("."), models_dir=Path("."))
        dlg = ReceiptTemplateEditorDialog(paths=paths, cfg={})
        try:
            dlg._roi_by_field["doc_number"] = RoiRecord(field="doc_number", page=1, box=(0.1, 0.1, 0.2, 0.2))
            called = {"question": 0}
            from kajovospend.ui import receipt_template_editor as rte

            q_orig = rte.QMessageBox.question
            rte.QMessageBox.question = lambda *args, **kwargs: called.__setitem__("question", called["question"] + 1)
            try:
                dlg._on_roi_changed("doc_number", 1, (0.2, 0.2, 0.4, 0.4))
            finally:
                rte.QMessageBox.question = q_orig
            self.assertEqual(called["question"], 0)
            self.assertEqual(dlg._roi_by_field["doc_number"].box, (0.2, 0.2, 0.4, 0.4))
        finally:
            dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
