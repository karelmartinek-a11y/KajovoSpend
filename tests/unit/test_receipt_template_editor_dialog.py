from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from kajovospend.ui.receipt_template_editor import ReceiptTemplateEditorDialog


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


if __name__ == "__main__":
    unittest.main()
