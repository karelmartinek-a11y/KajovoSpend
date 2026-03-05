from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QRegularExpression
from PySide6.QtGui import QDesktopServices, QRegularExpressionValidator, QUrl
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QAbstractItemView,
)

from kajovospend.extract.standard_receipts import legend_text, parse_template_schema_text, TemplateSchemaError
from kajovospend.utils.hashing import sha256_file

from . import db_api


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


class StandardReceiptTemplateDialog(QDialog):
    def __init__(self, paths, template: Dict[str, Any] | None = None, parent=None):
        super().__init__(parent)
        self.paths = paths
        self._template = template or {}
        self.setWindowTitle("Standardní účtenka")

        self._selected_sample_path: Optional[Path] = None
        self._sample_folder_relpath: Optional[Path] = None

        sample_rel = self._template.get("sample_file_relpath")
        if sample_rel:
            try:
                rel_path = Path(sample_rel)
                if rel_path.parent:
                    self._sample_folder_relpath = rel_path.parent
            except Exception:
                self._sample_folder_relpath = None

        self._existing_sample_relpath = sample_rel

        self._existing_sample_sha = self._template.get("sample_file_sha256")
        self._existing_sample_name = self._template.get("sample_file_name")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.ed_name = QLineEdit(self._template.get("name") or "")
        form.addRow("Název", self.ed_name)

        self.chk_enabled = QCheckBox("Aktivní")
        self.chk_enabled.setChecked(bool(self._template.get("enabled", True)))
        form.addRow(self.chk_enabled)

        ico_validator = QRegularExpressionValidator(QRegularExpression(r"^\d*$"), self)
        self.ed_match_ico = QLineEdit(self._template.get("match_supplier_ico_norm") or "")
        self.ed_match_ico.setValidator(ico_validator)
        self.ed_match_ico.editingFinished.connect(self._normalize_ico_field)
        form.addRow("Match IČO", self.ed_match_ico)

        match_texts = self._template.get("match_texts_json")
        tokens = []
        if match_texts:
            try:
                parsed = json.loads(match_texts)
                if isinstance(parsed, list):
                    tokens = [str(x) for x in parsed if str(x).strip()]
            except Exception:
                tokens = []
        self.match_texts = QTextEdit("\n".join(tokens))
        form.addRow("Match texty (1 token / řádek)", self.match_texts)

        sample_row = QHBoxLayout()
        self.btn_choose_sample = QPushButton("Vybrat PDF")
        self.btn_choose_sample.clicked.connect(self._select_sample_file)
        self.lbl_sample = QLabel(self._existing_sample_name or "Žádný soubor")
        self.lbl_sample.setWordWrap(True)
        self.btn_open_sample = QPushButton("Otevřít")
        self.btn_open_sample.clicked.connect(self._open_sample_file)
        self.btn_open_sample.setEnabled(bool(self._existing_sample_name))
        sample_row.addWidget(self.btn_choose_sample)
        sample_row.addWidget(self.lbl_sample, 1)
        sample_row.addWidget(self.btn_open_sample)
        form.addRow("Vzorový soubor", sample_row)

        schema_label = QLabel("Mapa polí (JSON)")
        layout.addLayout(form)
        layout.addWidget(schema_label)

        self.schema_edit = QTextEdit(self._template.get("schema_json") or "")
        layout.addWidget(self.schema_edit)

        schema_controls = QHBoxLayout()
        self.lbl_schema_status = QLabel("")
        self.btn_validate_schema = QPushButton("Validovat JSON")
        self.btn_validate_schema.clicked.connect(self._validate_schema_action)
        schema_controls.addWidget(self.btn_validate_schema)
        schema_controls.addWidget(self.lbl_schema_status, 1)
        layout.addLayout(schema_controls)

        legend_btn = QPushButton("Zkopírovat legendu")
        legend_btn.clicked.connect(self._copy_legend)
        layout.addWidget(legend_btn, alignment=Qt.AlignRight)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _normalize_ico_field(self) -> None:
        text = self.ed_match_ico.text()
        digits = re.sub(r"\D+", "", text)
        if digits:
            self.ed_match_ico.setText(digits.zfill(8) if len(digits) < 8 else digits)
        else:
            self.ed_match_ico.clear()

    def _select_sample_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Vybrat vzorový PDF", str(Path.home()), "PDF Files (*.pdf)")
        if not path:
            return
        self._selected_sample_path = Path(path)
        self.lbl_sample.setText(self._selected_sample_path.name)
        self.btn_open_sample.setEnabled(True)

    def _open_sample_file(self) -> None:
        if self._selected_sample_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._selected_sample_path)))
            return
        if not self._existing_sample_name or not self._existing_sample_relpath:
            return
        target = self.paths.data_dir / self._existing_sample_relpath
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _validate_schema_action(self) -> None:
        text = self.schema_edit.toPlainText().strip()
        if not text:
            self.lbl_schema_status.setText("JSON nesmí být prázdný.")
            return
        try:
            parse_template_schema_text(text)
        except TemplateSchemaError as exc:
            self.lbl_schema_status.setText(str(exc))
            return
        self.lbl_schema_status.setText("OK")

    def _copy_legend(self) -> None:
        QApplication.clipboard().setText(legend_text())
        QMessageBox.information(self, "Legenda", "Legenda byla zkopírována.")

    def _prepare_sample_info(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if self._selected_sample_path:
            folder = self._sample_folder_relpath or Path("templates") / uuid.uuid4().hex
            dest_dir = self.paths.data_dir / folder
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / self._selected_sample_path.name
            shutil.copy2(self._selected_sample_path, dest_path)
            self._sample_folder_relpath = folder
            relpath = str(dest_path.relative_to(self.paths.data_dir))
            self._existing_sample_relpath = relpath
            self._existing_sample_name = self._selected_sample_path.name
            self._existing_sample_sha = sha256_file(dest_path)
        return (self._existing_sample_name, self._existing_sample_sha, self._existing_sample_relpath)

    def _canonical_schema(self) -> str:
        text = self.schema_edit.toPlainText().strip()
        try:
            parsed = json.loads(text)
        except Exception:
            return text
        return json.dumps(parsed, ensure_ascii=False, indent=2)

    def accept(self) -> None:
        if not self.ed_name.text().strip():
            QMessageBox.warning(self, "Chyba", "Název je povinný.")
            return
        try:
            schema = self._canonical_schema()
            parse_template_schema_text(schema)
        except TemplateSchemaError as exc:
            QMessageBox.warning(self, "Schéma", f"Šablona není validní: {exc}")
            return
        super().accept()

    @property
    def payload(self) -> Dict[str, Any]:
        schema = self._canonical_schema()
        sample_name, sample_sha, sample_rel = self._prepare_sample_info()
        tokens = [line.strip() for line in self.match_texts.toPlainText().splitlines() if line.strip()]
        match_texts = json.dumps(tokens, ensure_ascii=False) if tokens else None
        return {
            "name": self.ed_name.text().strip(),
            "enabled": bool(self.chk_enabled.isChecked()),
            "match_supplier_ico_norm": self.ed_match_ico.text().strip() or None,
            "match_texts_json": match_texts,
            "schema_json": schema,
            "sample_file_name": sample_name,
            "sample_file_sha256": sample_sha,
            "sample_file_relpath": sample_rel,
        }


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

    def _run_db(
        self,
        work,
        done,
        *,
        timeout_ms: int | None = None,
    ) -> None:
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
                rows = db_api.list_standard_receipt_templates(session)
            return rows

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

    def _add_template(self) -> None:
        dlg = StandardReceiptTemplateDialog(self.paths, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        payload = dlg.payload

        def work():
            with self.sf() as session:
                tid = db_api.create_standard_receipt_template(session, payload)
                session.commit()
            return tid

        def done(_):
            self.refresh_templates()

        self._run_db(work, done, timeout_ms=15000)

    def _edit_template(self) -> None:
        row = self._selected_row()
        if not row:
            return
        template_id = int(row["id"])

        def work():
            with self.sf() as session:
                data = db_api.get_standard_receipt_template(session, template_id)
            return data

        def done(data):
            dlg = StandardReceiptTemplateDialog(self.paths, template=data, parent=self)
            if dlg.exec() != QDialog.Accepted:
                return
            payload = dlg.payload

            def work_update():
                with self.sf() as session:
                    db_api.update_standard_receipt_template(session, template_id, payload)
                    session.commit()

            def done_update(_):
                self.refresh_templates()

            self._run_db(work_update, done_update, timeout_ms=15000)

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

        def done(_):
            self.refresh_templates()

        self._run_db(work, done, timeout_ms=15000)
