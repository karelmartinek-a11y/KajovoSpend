from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from kajovospend.extract.standard_receipts import legend_text

from . import db_api
from .receipt_template_editor import ReceiptTemplateEditorDialog

log = logging.getLogger(__name__)


class TemplateTableModel(QAbstractTableModel):
    HEADERS = ["Název", "Aktivní", "Match IČO", "Aktualizováno", "Vzorový soubor"]

    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        super().__init__()
        self._rows = rows or []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return row.get("name") or ""
            if col == 1:
                return "Ano" if row.get("enabled") else "Ne"
            if col == 2:
                return row.get("match_ico") or ""
            if col == 3:
                updated = row.get("updated_at")
                if updated:
                    try:
                        return updated.isoformat(sep=" ", timespec="minutes")
                    except Exception:
                        return str(updated)
                return ""
            if col == 4:
                return row.get("sample_file_name") or ""
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        if 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def update_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_data(self, row: int) -> Dict[str, Any]:
        return self._rows[row]


class StandardReceiptsTab(QWidget):
    def __init__(self, sf, cfg, paths, runner_host, runner_cls, parent=None):
        super().__init__(parent)
        self.sf = sf
        self.cfg = cfg
        self.paths = paths
        self._runner_host = runner_host
        self._runner_cls = runner_cls
        self._templates: List[Dict[str, Any]] = []

        layout = QVBoxLayout(self)
        top_label = QLabel("Správa standardních účtenek")
        top_label.setWordWrap(True)
        layout.addWidget(top_label)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.model = TemplateTableModel()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Přidat")
        self.btn_add.clicked.connect(self._add_template)
        self.btn_edit = QPushButton("Editovat")
        self.btn_edit.clicked.connect(self._edit_template)
        self.btn_delete = QPushButton("Smazat")
        self.btn_delete.clicked.connect(self._delete_template)
        self.btn_open = QPushButton("Otevřít vzor")
        self.btn_open.clicked.connect(self._open_sample)
        self.btn_copy_legend = QPushButton("Zkopírovat legendu")
        self.btn_copy_legend.clicked.connect(self._copy_legend)
        for btn in (self.btn_add, self.btn_edit, self.btn_delete, self.btn_open, self.btn_copy_legend):
            btn_layout.addWidget(btn)
        layout.addLayout(btn_layout)

        self.table.selectionModel().selectionChanged.connect(self._update_action_state)
        self._update_action_state()
        self.refresh_templates()

    def _run_db(self, work, done, *, timeout_ms: int | None = None) -> None:
        def on_error(msg: str | None) -> None:
            QMessageBox.warning(self, "Šablony", f"Operace selhala: {msg}")

        if self._runner_cls and self._runner_host:
            self._runner_cls.run(self._runner_host, work, done, on_error, timeout_ms=timeout_ms)
        else:
            try:
                res = work()
            except Exception as exc:  # pragma: no cover
                on_error(str(exc))
            else:
                done(res)

    def refresh_templates(self) -> None:
        def work():
            with self.sf() as session:
                return db_api.list_standard_receipt_templates(session)

        def done(rows):
            self._templates = rows
            self.model.update_rows(rows)
            self._update_action_state()

        self._run_db(work, done, timeout_ms=15000)

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        idx = self.table.selectionModel().currentIndex()
        if not idx.isValid():
            return None
        return self.model.row_data(idx.row())

    def _update_action_state(self) -> None:
        selected = self._selected_row()
        enabled = bool(selected)
        self.btn_edit.setEnabled(enabled)
        self.btn_delete.setEnabled(enabled)
        has_sample = bool(selected and selected.get("sample_file_relpath"))
        self.btn_open.setEnabled(has_sample)

    def _open_sample(self) -> None:
        row = self._selected_row()
        if not row:
            return
        rel = row.get("sample_file_relpath")
        if not rel:
            return
        target = self.paths.data_dir / rel
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        else:
            QMessageBox.warning(self, "Vzorek", "Vzorový soubor nelze najít.")

    def _copy_legend(self) -> None:
        QApplication.clipboard().setText(legend_text())
        QMessageBox.information(self, "Legenda", "Legenda byla zkopírována.")

    def _add_template(self, _checked: bool = False) -> None:
        try:
            dlg = ReceiptTemplateEditorDialog(self.paths, self.cfg, parent=self)
        except Exception:
            log.exception("Nepodarilo se otevrit editor standardni uctenky")
            QMessageBox.warning(self, "Sablony", "Nepodarilo se otevrit editor sablony. Detaily jsou v logu.")
            return
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.payload

        def work():
            with self.sf() as session:
                tid = db_api.create_standard_receipt_template(session, payload)
                session.commit()
            return tid

        self._run_db(work, lambda _: self.refresh_templates(), timeout_ms=15000)

    def _edit_template(self) -> None:
        row = self._selected_row()
        if not row:
            return
        template_id = int(row["id"])

        def work():
            with self.sf() as session:
                return db_api.get_standard_receipt_template(session, template_id)

        def done(data):
            dlg = ReceiptTemplateEditorDialog(self.paths, self.cfg, template=data, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            payload = dlg.payload

            def work_update():
                with self.sf() as session:
                    db_api.update_standard_receipt_template(session, template_id, payload)
                    session.commit()

            self._run_db(work_update, lambda _: self.refresh_templates(), timeout_ms=15000)

        self._run_db(work, done, timeout_ms=15000)

    def _delete_template(self) -> None:
        row = self._selected_row()
        if not row:
            return
        if QMessageBox.question(self, "Šablony", "Opravdu smazat šablonu?") != QMessageBox.Yes:
            return
        template_id = int(row["id"])

        def work():
            with self.sf() as session:
                db_api.delete_standard_receipt_template(session, template_id)
                session.commit()

        self._run_db(work, lambda _: self.refresh_templates(), timeout_ms=15000)
