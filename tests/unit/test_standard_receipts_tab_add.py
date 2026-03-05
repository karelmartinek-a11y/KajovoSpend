from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QDialog

from kajovospend.ui import standard_receipts_tab as srt


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _DummySession:
    def __init__(self) -> None:
        self.commit_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1


class _FakeDialog:
    exec_result = QDialog.DialogCode.Rejected
    payload = {"name": "Test"}
    init_calls = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).init_calls += 1

    def exec(self) -> int:
        return type(self).exec_result


class _RaisingDialog:
    def __init__(self, *args, **kwargs) -> None:
        raise AttributeError("boom")


class TestStandardReceiptsTabAdd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._orig_dialog = srt.ReceiptTemplateEditorDialog
        self._orig_list = srt.db_api.list_standard_receipt_templates
        self._orig_create = srt.db_api.create_standard_receipt_template
        self._orig_get = srt.db_api.get_standard_receipt_template
        self._orig_update = srt.db_api.update_standard_receipt_template
        srt.ReceiptTemplateEditorDialog = _FakeDialog
        srt.db_api.list_standard_receipt_templates = lambda _session: []
        _FakeDialog.init_calls = 0
        _FakeDialog.exec_result = 0
        self.created_payload = None

        self.session = _DummySession()

        @contextmanager
        def sf():
            yield self.session

        self.tab = srt.StandardReceiptsTab(
            sf=sf,
            cfg={},
            paths=SimpleNamespace(data_dir=Path("."), models_dir=Path(".")),
            runner_host=None,
            runner_cls=None,
        )

    def tearDown(self) -> None:
        srt.ReceiptTemplateEditorDialog = self._orig_dialog
        srt.db_api.list_standard_receipt_templates = self._orig_list
        srt.db_api.create_standard_receipt_template = self._orig_create
        srt.db_api.get_standard_receipt_template = self._orig_get
        srt.db_api.update_standard_receipt_template = self._orig_update
        self.tab.deleteLater()

    def test_add_template_accepts_checked_argument(self) -> None:
        _FakeDialog.exec_result = QDialog.DialogCode.Rejected
        self.tab._add_template(True)
        self.assertEqual(_FakeDialog.init_calls, 1)

    def test_add_template_persists_when_dialog_accepted(self) -> None:
        def fake_create(_session, payload):
            self.created_payload = payload
            return 42

        srt.db_api.create_standard_receipt_template = fake_create
        _FakeDialog.exec_result = QDialog.DialogCode.Accepted
        _FakeDialog.payload = {"name": "Moje sablona"}

        self.tab._add_template(True)

        self.assertEqual(_FakeDialog.init_calls, 1)
        self.assertEqual(self.created_payload, {"name": "Moje sablona"})
        self.assertEqual(self.session.commit_calls, 1)

    def test_add_template_handles_editor_init_failure(self) -> None:
        warning_calls = []
        original_warning = srt.QMessageBox.warning
        srt.ReceiptTemplateEditorDialog = _RaisingDialog
        srt.QMessageBox.warning = lambda *args, **kwargs: warning_calls.append((args, kwargs))
        try:
            self.tab._add_template(True)
        finally:
            srt.QMessageBox.warning = original_warning
        self.assertEqual(len(warning_calls), 1)

    def test_edit_template_persists_when_dialog_accepted(self) -> None:
        get_calls = []
        update_calls = []

        def fake_get(_session, template_id):
            get_calls.append(template_id)
            return {"id": template_id, "name": "Old"}

        def fake_update(_session, template_id, payload):
            update_calls.append((template_id, payload))

        srt.db_api.get_standard_receipt_template = fake_get
        srt.db_api.update_standard_receipt_template = fake_update
        _FakeDialog.exec_result = QDialog.DialogCode.Accepted
        _FakeDialog.payload = {"name": "New"}

        self.tab.model.update_rows([{"id": 1, "name": "X", "sample_file_relpath": None}])
        self.tab.table.setCurrentIndex(self.tab.model.index(0, 0))
        self.tab._edit_template()

        self.assertEqual(get_calls, [1])
        self.assertEqual(update_calls, [(1, {"name": "New"})])


if __name__ == "__main__":
    unittest.main()
