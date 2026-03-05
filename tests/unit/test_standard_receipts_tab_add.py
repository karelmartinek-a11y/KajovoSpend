from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from kajovospend.ui import standard_receipts_tab as srt


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _DummySession:
    def __init__(self) -> None:
        self.commit_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1


class _FakeDialog:
    Accepted = 1
    exec_result = 0
    payload = {"name": "Test"}
    init_calls = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).init_calls += 1

    def exec(self) -> int:
        return type(self).exec_result


class TestStandardReceiptsTabAdd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._orig_dialog = srt.ReceiptTemplateEditorDialog
        self._orig_list = srt.db_api.list_standard_receipt_templates
        self._orig_create = srt.db_api.create_standard_receipt_template
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
        self.tab.deleteLater()

    def test_add_template_accepts_checked_argument(self) -> None:
        _FakeDialog.exec_result = 0
        self.tab._add_template(True)
        self.assertEqual(_FakeDialog.init_calls, 1)

    def test_add_template_persists_when_dialog_accepted(self) -> None:
        def fake_create(_session, payload):
            self.created_payload = payload
            return 42

        srt.db_api.create_standard_receipt_template = fake_create
        _FakeDialog.exec_result = _FakeDialog.Accepted
        _FakeDialog.payload = {"name": "Moje sablona"}

        self.tab._add_template(True)

        self.assertEqual(_FakeDialog.init_calls, 1)
        self.assertEqual(self.created_payload, {"name": "Moje sablona"})
        self.assertEqual(self.session.commit_calls, 1)


if __name__ == "__main__":
    unittest.main()
