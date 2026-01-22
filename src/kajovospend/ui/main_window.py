from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QAbstractTableModel, QModelIndex, QObject, Signal, Slot, QThread, QUrl
from PySide6.QtGui import QIcon, QPixmap, QImage, QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget, QTableView,
    QLineEdit, QFormLayout, QSplitter, QTextEdit, QDoubleSpinBox, QSpinBox, QComboBox, QFileDialog,
    QMessageBox, QDateEdit, QProgressBar, QDialog, QDialogButtonBox, QHeaderView, QAbstractItemView,
    QCheckBox, QProgressDialog, QApplication, QInputDialog,
)
from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem

from sqlalchemy.orm import Session
from sqlalchemy import select, text

from kajovospend.utils.config import load_yaml, save_yaml, deep_set
from kajovospend.utils.paths import resolve_app_paths
from kajovospend.utils.logging_setup import setup_logging
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.db.migrate import init_db
from kajovospend.db.models import Supplier, Document, DocumentFile, LineItem
from kajovospend.db.queries import upsert_supplier, rebuild_fts_for_document
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico
from kajovospend.integrations.openai_fallback import list_models
from kajovospend.service.control_client import send_cmd
from kajovospend.ocr.pdf_render import render_pdf_to_images

from .styles import QSS
from . import db_api


class PdfPreviewView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pix_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pix_item)

        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)

    def clear(self):
        self._pix_item.setPixmap(QPixmap())
        self._scene.setSceneRect(0, 0, 1, 1)
        self.resetTransform()

    def set_pixmap(self, px: QPixmap):
        self._pix_item.setPixmap(px)
        if not px.isNull():
            self._scene.setSceneRect(px.rect())
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def zoom_in(self):
        self.scale(1.25, 1.25)

    def zoom_out(self):
        self.scale(0.8, 0.8)

    def reset_zoom(self):
        self.resetTransform()
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        # Ctrl+wheel for zoom; plain wheel = scroll/pan default
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)


class TableModel(QAbstractTableModel):
    def __init__(self, headers: List[str], rows: List[List[Any]]):
        super().__init__()
        self.headers = headers
        self.rows = rows

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.headers)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            v = self.rows[index.row()][index.column()]
            if isinstance(v, float):
                return f"{v:,.2f}".replace(",", " ")
            return "" if v is None else str(v)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self.headers):
                return self.headers[section]
        return str(section + 1)


class EditableItemsModel(QAbstractTableModel):
    """
    Editovatelný model položek (LineItem) – používá se pro ÚČTY i NEROZPOZNANÉ.
    Udržuje i interní ID položky, aby šlo ukládat změny zpět do DB.
    """

    COLS = [
        ("name", "Položka"),
        ("quantity", "Množství"),
        ("unit_price", "Jedn. cena"),
        ("line_total", "Celkem"),
        ("vat_rate", "DPH %"),
        ("ean", "EAN"),
        ("item_code", "Kód položky"),
    ]

    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        super().__init__()
        self._rows: List[Dict[str, Any]] = rows or []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.COLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self.COLS):
                return self.COLS[section][1]
        return str(section + 1)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    def _fmt(self, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:,.2f}".replace(",", " ")
        return str(v)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        key = self.COLS[index.column()][0]
        row = self._rows[index.row()]
        v = row.get(key)
        if role == Qt.DisplayRole:
            return self._fmt(v)
        if role == Qt.EditRole:
            # pro editování vrať raw hodnotu (bez formátování)
            return "" if v is None else v
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        key = self.COLS[index.column()][0]
        row = self._rows[index.row()]

        def _to_float(x):
            if x is None:
                return 0.0
            if isinstance(x, (int, float)):
                return float(x)
            s = str(x).strip().replace(" ", "").replace(",", ".")
            if not s:
                return 0.0
            try:
                return float(s)
            except Exception:
                return 0.0

        if key in ("quantity", "unit_price", "line_total", "vat_rate"):
            row[key] = _to_float(value)
            # pokud uživatel mění qty nebo unit_price a line_total je 0, dopočti
            if key in ("quantity", "unit_price"):
                q = float(row.get("quantity") or 0.0)
                up = float(row.get("unit_price") or 0.0)
                if (row.get("line_total") in (None, 0, 0.0)) and (q or up):
                    row["line_total"] = q * up
        else:
            row[key] = ("" if value is None else str(value)).strip()

        self._rows[index.row()] = row
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True

    def insertRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()) -> bool:
        if count <= 0:
            return False
        self.beginInsertRows(QModelIndex(), row, row + count - 1)
        for _ in range(count):
            self._rows.insert(
                row,
                {
                    "id": None,
                    "name": "",
                    "quantity": 1.0,
                    "unit_price": 0.0,
                    "line_total": 0.0,
                    "vat_rate": 0.0,
                    "ean": "",
                    "item_code": "",
                },
            )
        self.endInsertRows()
        return True

    def removeRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()) -> bool:
        if count <= 0 or row < 0 or row >= len(self._rows):
            return False
        last = min(row + count - 1, len(self._rows) - 1)
        self.beginRemoveRows(QModelIndex(), row, last)
        del self._rows[row:last + 1]
        self.endRemoveRows()
        return True

    def rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)


def pil_to_pixmap(img) -> QPixmap:
    return QPixmap.fromImage(pil_to_qimage(img))


def pil_to_qimage(img) -> QImage:
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    return QImage(data, img.width, img.height, QImage.Format_RGB888)


def _format_supplier_address(street: str | None, cp: str | None, co: str | None, city: str | None, zip_code: str | None) -> str | None:
    parts: list[str] = []
    s = (street or "").strip()
    cpv = (cp or "").strip()
    cov = (co or "").strip()
    if s or cpv or cov:
        num = cpv + (f"/{cov}" if cov else "")
        first = (s + " " + num).strip()
        if first:
            parts.append(first)
    if (city or "").strip():
        parts.append(city.strip())
    if (zip_code or "").strip():
        parts.append(zip_code.strip())
    return ", ".join(parts) if parts else None


class _Worker(QObject):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self):
        try:
            res = self._fn()
        except Exception as e:
            self.error.emit(str(e))
            return
        self.done.emit(res)


class StatusDialog(QDialog):
    def __init__(self, status: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle("STAV")
        lay = QVBoxLayout(self)
        txt = QTextEdit()
        txt.setReadOnly(True)
        lines = []
        for k in ["running", "queue_size", "last_success", "last_error", "last_error_at", "last_seen"]:
            lines.append(f"{k}: {status.get(k)}")
        txt.setText("\n".join(lines))
        lay.addWidget(txt)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class SupplierDialog(QDialog):
    def __init__(self, parent=None, initial: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("Dodavatel")
        self._initial = initial or {}
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.ed_ico = QLineEdit(self._initial.get("ico", "") or "")
        self.ed_name = QLineEdit(self._initial.get("name", "") or "")
        self.ed_dic = QLineEdit(self._initial.get("dic", "") or "")
        self.ed_legal_form = QLineEdit(self._initial.get("legal_form", "") or "")
        self.ed_street = QLineEdit(self._initial.get("street", "") or "")
        self.ed_street_number = QLineEdit(self._initial.get("street_number", "") or "")
        self.ed_orientation_number = QLineEdit(self._initial.get("orientation_number", "") or "")
        self.ed_city = QLineEdit(self._initial.get("city", "") or "")
        self.ed_zip = QLineEdit(self._initial.get("zip_code", "") or "")
        self.ed_addr = QLineEdit(self._initial.get("address", "") or "")
        self.cb_vat = QCheckBox("Plátce DPH")
        self.cb_vat.setChecked(bool(self._initial.get("is_vat_payer") is True))

        form.addRow("IČO", self.ed_ico)
        form.addRow("Název", self.ed_name)
        form.addRow("DIČ", self.ed_dic)
        form.addRow("Právní forma", self.ed_legal_form)
        form.addRow("Ulice", self.ed_street)
        form.addRow("Číslo popisné", self.ed_street_number)
        form.addRow("Číslo orientační", self.ed_orientation_number)
        form.addRow("Město", self.ed_city)
        form.addRow("PSČ", self.ed_zip)
        form.addRow("Adresa (řetězec)", self.ed_addr)
        form.addRow("", self.cb_vat)

        lay.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self) -> Dict[str, Any]:
        street = self.ed_street.text().strip() or None
        cp = self.ed_street_number.text().strip() or None
        co = self.ed_orientation_number.text().strip() or None
        city = self.ed_city.text().strip() or None
        zipc = self.ed_zip.text().strip() or None
        return {
            "ico": self.ed_ico.text().strip(),
            "name": self.ed_name.text().strip() or None,
            "dic": self.ed_dic.text().strip() or None,
            "legal_form": self.ed_legal_form.text().strip() or None,
            "street": street,
            "street_number": cp,
            "orientation_number": co,
            "city": city,
            "zip_code": zipc,
            "address": self.ed_addr.text().strip() or _format_supplier_address(street, cp, co, city, zipc),
            "is_vat_payer": True if self.cb_vat.isChecked() else False,
        }


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path, assets_dir: Path):
        super().__init__()
        self.config_path = config_path
        self.assets_dir = assets_dir
        self.cfg = self._load_or_create_config()
        # keep threads/workers alive
        self._threads: List[QThread] = []
        self._dialogs: List[QProgressDialog] = []
        self._workers: List[_Worker] = []
        self._timers: List[QTimer] = []
        self._sup_sel_connected = False
        self._sup_filter_timer = QTimer(self)
        self._sup_filter_timer.setSingleShot(True)

        # paging / cache for documents
        self._doc_page_size = int(self.cfg.get("performance", {}).get("docs_page_size", 500) or 500)
        self._doc_offset = 0
        self._doc_total = 0
        self._preview_cache: Dict[Tuple[str, int], QPixmap] = {}
        self._preview_dpi = int(self.cfg.get("performance", {}).get("preview_dpi", 120) or 120)

        # paging for per-item search tab
        self._items_page_size = int(self.cfg.get("performance", {}).get("items_page_size", 1000) or 1000)
        self._items_offset = 0
        self._items_total = 0
        self._items_rows: List[Dict[str, Any]] = []
        self._items_current_path: str | None = None

        # current selections (ÚČTY / NEROZPOZNANÉ)
        self._current_doc_id: int | None = None
        self._current_doc_file_id: int | None = None
        self._current_doc_path: str | None = None
        self._current_doc_items_model: EditableItemsModel | None = None
        self._current_unrec_doc_id: int | None = None
        self._current_unrec_file_id: int | None = None
        self._current_unrec_path: str | None = None
        self._current_unrec_items_model: EditableItemsModel | None = None

        self.paths = resolve_app_paths(
            self.cfg["app"].get("data_dir"),
            self.cfg["app"].get("db_path"),
            self.cfg["app"].get("log_dir"),
            self.cfg.get("ocr", {}).get("models_dir"),
        )
        self.log = setup_logging(self.paths.log_dir, name="kajovospend_gui")

        self.engine = make_engine(str(self.paths.db_path))
        init_db(self.engine)
        self.sf = make_session_factory(self.engine)

        self.setWindowTitle("KájovoSpend")
        ico = self.assets_dir / "app.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))

        self.setStyleSheet(QSS)

        self._build_ui()
        self._wire_timers()
        self.refresh_all_v2()

    def _load_or_create_config(self) -> Dict[str, Any]:
        if self.config_path.exists():
            cfg = load_yaml(self.config_path)
        else:
            cfg = load_yaml(self.config_path.with_name("config.example.yaml"))
            save_yaml(self.config_path, cfg)
        cfg.setdefault("app", {})
        cfg.setdefault("paths", {})
        cfg.setdefault("service", {})
        cfg.setdefault("ocr", {})
        cfg.setdefault("openai", {})
        cfg.setdefault("performance", {})
        return cfg

    def _run_with_busy(self, title: str, message: str, fn, on_done, on_error=None, timeout_ms: int | None = None):
        """
        Run fn() in background thread with a modal indeterminate progress dialog.
        """
        dlg = QProgressDialog(message, "", 0, 0, self)
        dlg.setWindowTitle(title)
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setAutoClose(True)
        dlg.setAutoReset(True)
        dlg.show()
        self._dialogs.append(dlg)

        th = QThread(self)
        wk = _Worker(fn)
        wk.moveToThread(th)
        th.started.connect(wk.run)
        completed = False
        timer = None

        self._workers.append(wk)

        def _dispatch_ui(fn) -> None:
            if QThread.currentThread() != self.thread():
                QTimer.singleShot(0, self, fn)
                return
            fn()

        def _finish_once() -> bool:
            nonlocal completed
            if completed:
                return False
            completed = True
            return True

        def _cleanup():
            try:
                if timer is not None:
                    timer.stop()
            except Exception:
                pass
            try:
                dlg.close()
            except Exception:
                pass
            try:
                th.quit()
                th.wait(2000)
            except Exception:
                pass
            try:
                self._threads.remove(th)
            except Exception:
                pass
            try:
                self._dialogs.remove(dlg)
            except Exception:
                pass
            try:
                self._workers.remove(wk)
            except Exception:
                pass
            try:
                if timer is not None:
                    self._timers.remove(timer)
            except Exception:
                pass

        def _ok(res):
            def _impl():
                if not _finish_once():
                    return
                _cleanup()
                try:
                    on_done(res)
                except Exception:
                    self.log.exception("UI on_done handler failed")
            _dispatch_ui(_impl)

        def _err(msg: str):
            def _impl():
                if not _finish_once():
                    return
                _cleanup()
                if on_error:
                    try:
                        on_error(msg)
                        return
                    except Exception:
                        self.log.exception("UI on_error handler failed")
                QMessageBox.critical(self, title, f"Chyba: {msg}")
            _dispatch_ui(_impl)

        wk.done.connect(_ok)
        wk.error.connect(_err)

        self._threads.append(th)
        th.start()

        if timeout_ms is not None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: _err("Operace překročila časový limit."))
            timer.start(int(timeout_ms))
            self._timers.append(timer)

    def _load_logo_pixmap(self) -> Optional[QPixmap]:
        # Prefer a dedicated logo if present, fallback to app.ico.
        for cand in ["logo.png", "logo.bmp", "logo.jpg"]:
            p = self.assets_dir / cand
            if p.exists():
                px = QPixmap(str(p))
                if not px.isNull():
                    return px
        ico = self.assets_dir / "app.ico"
        if ico.exists():
            ic = QIcon(str(ico))
            px = ic.pixmap(28, 28)
            if not px.isNull():
                return px
        return None

    def _build_ui(self):
        root = QWidget()
        v = QVBoxLayout(root)

        # Header
        header = QWidget()
        header.setObjectName("HeaderBar")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 12, 12, 6)

        self.logo = QLabel()
        self.logo.setObjectName("LogoLabel")
        self.logo.setFixedSize(28, 28)
        self.logo.setScaledContents(True)
        px = self._load_logo_pixmap()
        if px:
            self.logo.setPixmap(px)
        title = QLabel("KájovoSpend")
        title.setObjectName("TitleLabel")

        left = QWidget()
        left.setProperty("panel", True)
        lhl = QHBoxLayout(left)
        lhl.setContentsMargins(10, 6, 10, 6)
        lhl.setSpacing(10)
        lhl.addWidget(self.logo)
        lhl.addWidget(title)
        hl.addWidget(left)
        hl.addStretch(1)

        self.btn_status = QPushButton("STAV")
        self.btn_run = QPushButton("RUN")
        self.btn_stop = QPushButton("STOP")
        self.btn_restart = QPushButton("RESTART")
        self.btn_exit = QPushButton("EXIT")
        self.btn_exit.setObjectName("ExitButton")

        for b in [self.btn_status, self.btn_run, self.btn_stop, self.btn_restart]:
            hl.addWidget(b)
        hl.addStretch(1)
        hl.addWidget(self.btn_exit)

        v.addWidget(header)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        # Dashboard
        self.tab_dashboard = QWidget()
        dl = QVBoxLayout(self.tab_dashboard)
        cards = QWidget()
        cl = QHBoxLayout(cards)
        self.lbl_unprocessed = QLabel("Nezpracované: 0")
        self.lbl_processed = QLabel("Zpracované: 0")
        self.lbl_docs = QLabel("Účty: 0")
        self.lbl_suppliers = QLabel("Dodavatelé: 0")
        for lab in [self.lbl_unprocessed, self.lbl_processed, self.lbl_docs, self.lbl_suppliers]:
            lab.setMinimumWidth(180)
            lab.setProperty("card", True)
            cl.addWidget(lab)
        cl.addStretch(1)
        dl.addWidget(cards)

        self.lbl_service = QLabel("Služba: ?")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        dl.addWidget(self.lbl_service)
        dl.addWidget(self.progress)

        self.tabs.addTab(self.tab_dashboard, "DASHBOARD")

        # Provozní panel
        self.tab_ops = QWidget()
        ol = QVBoxLayout(self.tab_ops)
        self.ops_table = QTableView()
        self.ops_table.setAlternatingRowColors(True)
        self.ops_table.setShowGrid(True)
        self.ops_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ops_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ol.addWidget(self.ops_table)
        self.tabs.addTab(self.tab_ops, "PROVOZNÍ PANEL")

        # Podezřelé
        self.tab_susp = QWidget()
        sl = QVBoxLayout(self.tab_susp)
        self.susp_table = QTableView()
        self.susp_table.setAlternatingRowColors(True)
        self.susp_table.setShowGrid(True)
        self.susp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.susp_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        sl.addWidget(self.susp_table)
        self.tabs.addTab(self.tab_susp, "PODEZŘELÉ DOKLADY")

        # $
        self.tab_money = QWidget()
        ml = QVBoxLayout(self.tab_money)
        self.money_summary = QTextEdit()
        self.money_summary.setReadOnly(True)
        ml.addWidget(self.money_summary)
        self.tabs.addTab(self.tab_money, "$")

        # Nastavení
        self.tab_settings = QWidget()
        stl = QVBoxLayout(self.tab_settings)
        form = QFormLayout()

        self.ed_api_key = QLineEdit()
        self.ed_api_key.setPlaceholderText("OpenAI API key")
        self.cb_model = QComboBox()
        self.btn_load_models = QPushButton("Načíst modely")

        self.ed_input_dir = QLineEdit(self.cfg["paths"].get("input_dir", ""))
        self.btn_pick_input = QPushButton("Vybrat")
        self.ed_output_dir = QLineEdit(self.cfg["paths"].get("output_dir", ""))
        self.btn_pick_output = QPushButton("Vybrat")

        self.cb_openai_enabled = QComboBox()
        self.cb_openai_enabled.addItems(["false", "true"])
        self.cb_openai_enabled.setCurrentIndex(1 if self.cfg.get("openai", {}).get("enabled") else 0)

        row_api = QWidget(); r1 = QHBoxLayout(row_api); r1.setContentsMargins(0,0,0,0)
        r1.addWidget(self.ed_api_key); r1.addWidget(self.cb_openai_enabled)
        form.addRow("API-KEY / enabled", row_api)

        row_model = QWidget(); r2 = QHBoxLayout(row_model); r2.setContentsMargins(0,0,0,0)
        r2.addWidget(self.cb_model, 1); r2.addWidget(self.btn_load_models)
        form.addRow("Model OpenAI", row_model)

        row_in = QWidget(); r3 = QHBoxLayout(row_in); r3.setContentsMargins(0,0,0,0)
        r3.addWidget(self.ed_input_dir, 1); r3.addWidget(self.btn_pick_input)
        form.addRow("Input adresář", row_in)

        row_out = QWidget(); r4 = QHBoxLayout(row_out); r4.setContentsMargins(0,0,0,0)
        r4.addWidget(self.ed_output_dir, 1); r4.addWidget(self.btn_pick_output)
        form.addRow("Output adresář", row_out)

        self.btn_save_settings = QPushButton("Uložit nastavení")

        stl.addLayout(form)
        stl.addWidget(self.btn_save_settings)
        self.tabs.addTab(self.tab_settings, "NASTAVENÍ")

        # Dodavatelé
        self.tab_suppliers = QWidget()
        spl = QHBoxLayout(self.tab_suppliers)

        # Left side: table and filter
        left = QWidget()
        ll = QVBoxLayout(left)
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.sup_filter = QLineEdit()
        self.sup_filter.setPlaceholderText("Hledat (název, IČO, DIČ, město)...")
        self.btn_sup_refresh = QPushButton("Obnovit")
        self.btn_sup_add = QPushButton("Přidat")
        self.btn_sup_merge = QPushButton("Sloučit")
        self.btn_sup_merge.setEnabled(False)
        tl.addWidget(self.sup_filter, 1)
        tl.addWidget(self.btn_sup_refresh)
        tl.addWidget(self.btn_sup_add)
        tl.addWidget(self.btn_sup_merge)
        ll.addWidget(top)

        self.sup_table = QTableView()
        self.sup_table.setAlternatingRowColors(True)
        self.sup_table.setShowGrid(True)
        self.sup_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sup_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sup_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ll.addWidget(self.sup_table, 1)

        # Right side: detail form
        right = QWidget()
        rl = QVBoxLayout(right)

        hdr = QWidget()
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(0, 0, 0, 0)
        self.lbl_sup_detail = QLabel("Detail dodavatele")
        self.btn_sup_edit = QPushButton("Editovat")
        self.btn_sup_save = QPushButton("Uložit")
        self.btn_sup_save.setEnabled(False)
        hl.addWidget(self.lbl_sup_detail, 1)
        hl.addWidget(self.btn_sup_edit)
        hl.addWidget(self.btn_sup_save)
        rl.addWidget(hdr)

        formw = QWidget()
        form = QFormLayout(formw)
        self.sup_id = QLineEdit(); self.sup_id.setReadOnly(True)
        self.sup_ico = QLineEdit(); self.sup_ico.setReadOnly(True)
        self.btn_sup_ares_detail = QPushButton("ARES")
        ico_row = QWidget()
        ico_l = QHBoxLayout(ico_row); ico_l.setContentsMargins(0,0,0,0)
        ico_l.addWidget(self.sup_ico, 1)
        ico_l.addWidget(self.btn_sup_ares_detail)

        self.sup_name = QLineEdit(); self.sup_name.setReadOnly(True)
        self.sup_legal_form = QLineEdit(); self.sup_legal_form.setReadOnly(True)
        self.sup_dic = QLineEdit(); self.sup_dic.setReadOnly(True)
        self.sup_vat = QCheckBox("Plátce DPH"); self.sup_vat.setEnabled(False)

        self.sup_street = QLineEdit(); self.sup_street.setReadOnly(True)
        self.sup_street_number = QLineEdit(); self.sup_street_number.setReadOnly(True)
        self.sup_orientation_number = QLineEdit(); self.sup_orientation_number.setReadOnly(True)
        self.sup_city = QLineEdit(); self.sup_city.setReadOnly(True)
        self.sup_zip = QLineEdit(); self.sup_zip.setReadOnly(True)

        form.addRow("ID (KajovoSpend)", self.sup_id)
        form.addRow("IČO", ico_row)
        form.addRow("Název subjektu", self.sup_name)
        form.addRow("Právní forma podnikání", self.sup_legal_form)
        form.addRow("DIČ", self.sup_dic)
        form.addRow("", self.sup_vat)
        form.addRow("Ulice sídla", self.sup_street)
        form.addRow("Číslo popisné sídla", self.sup_street_number)
        form.addRow("Číslo orientační sídla", self.sup_orientation_number)
        form.addRow("Město sídla", self.sup_city)
        form.addRow("PSČ sídla", self.sup_zip)

        rl.addWidget(formw, 1)

        spl.addWidget(left, 1)
        spl.addWidget(right, 1)
        self.tabs.addTab(self.tab_suppliers, "DODAVATELÉ")

        # Položky (per-item search)
        self.tab_items = QWidget()
        items_layout = QVBoxLayout(self.tab_items)

        items_top = QWidget()
        items_top_l = QHBoxLayout(items_top)
        items_top_l.setContentsMargins(0, 0, 0, 0)
        self.items_filter = QLineEdit()
        self.items_filter.setPlaceholderText("Vyhledat v položkách (název, IČO, číslo dokladu...)")
        self.btn_items_search = QPushButton("Hledat")
        self.btn_items_more = QPushButton("Načíst další")
        self.lbl_items_page = QLabel("0 / 0")
        items_top_l.addWidget(self.items_filter, 1)
        items_top_l.addWidget(self.btn_items_search)
        items_top_l.addWidget(self.btn_items_more)
        items_top_l.addWidget(self.lbl_items_page)
        items_layout.addWidget(items_top)

        items_split = QSplitter()
        items_split.setOrientation(Qt.Horizontal)
        items_layout.addWidget(items_split, 1)

        items_left = QWidget()
        il = QVBoxLayout(items_left)
        self.items_table = QTableView()
        self.items_table.setAlternatingRowColors(True)
        self.items_table.setShowGrid(True)
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        il.addWidget(self.items_table, 1)
        items_split.addWidget(items_left)

        items_right = QWidget()
        ir = QVBoxLayout(items_right)
        src_row_items = QWidget()
        sri = QHBoxLayout(src_row_items)
        sri.setContentsMargins(0, 0, 0, 0)
        self.items_src = QLineEdit()
        self.items_src.setReadOnly(True)
        self.btn_items_open = QPushButton("Otevřít doklad")
        sri.addWidget(QLabel("Zdroj:"))
        sri.addWidget(self.items_src, 1)
        sri.addWidget(self.btn_items_open)
        ir.addWidget(src_row_items)

        self.lbl_items_doc = QLabel("")
        ir.addWidget(self.lbl_items_doc)

        self.items_preview = PdfPreviewView()
        ir.addWidget(self.items_preview, 1)
        items_split.addWidget(items_right)

        items_split.setStretchFactor(0, 2)
        items_split.setStretchFactor(1, 3)

        self.tabs.addTab(self.tab_items, "POLOŽKY")

        # Účty
        self.tab_docs = QWidget()
        dl2 = QVBoxLayout(self.tab_docs)
        filters = QWidget(); filters.setProperty("panel", True)
        fl = QHBoxLayout(filters); fl.setContentsMargins(10,6,10,6)
        doc_toolbar = QWidget()
        dtb = QHBoxLayout(doc_toolbar); dtb.setContentsMargins(0, 0, 0, 0)
        self.doc_filter = QLineEdit(); self.doc_filter.setPlaceholderText("Vyhledat v účtech (IČO, číslo dokladu, účet, text, položky)…")
        self.doc_date_from = QDateEdit(); self.doc_date_from.setCalendarPopup(True); self.doc_date_from.setDisplayFormat("dd.MM.yyyy")
        self.doc_date_to = QDateEdit(); self.doc_date_to.setCalendarPopup(True); self.doc_date_to.setDisplayFormat("dd.MM.yyyy")
        self.doc_date_from.setDate(dt.date.today() - dt.timedelta(days=365))
        self.doc_date_to.setDate(dt.date.today())
        self.cb_all_dates = QCheckBox("Bez filtru dat"); self.cb_all_dates.setChecked(True)
        self.btn_docs_search = QPushButton("Hledat")
        self.btn_docs_more = QPushButton("Načíst další")
        self.lbl_docs_page = QLabel("0 / 0")
        dtb.addWidget(self.doc_filter, 1)
        dtb.addWidget(QLabel("Od:"))
        dtb.addWidget(self.doc_date_from)
        dtb.addWidget(QLabel("Do:"))
        dtb.addWidget(self.doc_date_to)
        dtb.addWidget(self.cb_all_dates)
        dtb.addWidget(self.btn_docs_search)
        dtb.addWidget(self.btn_docs_more)
        dtb.addWidget(self.lbl_docs_page)
        dl2.addWidget(doc_toolbar)

        splitter = QSplitter()
        splitter.setOrientation(Qt.Horizontal)
        left = QWidget(); ll = QVBoxLayout(left)
        self.docs_table = QTableView()
        self.docs_table.setAlternatingRowColors(True)
        self.docs_table.setShowGrid(True)
        self.docs_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.docs_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.docs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ll.addWidget(self.docs_table, 1)
        splitter.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right)
        srcrow = QWidget(); sr = QHBoxLayout(srcrow); sr.setContentsMargins(0, 0, 0, 0)
        self.doc_src_line = QLineEdit(); self.doc_src_line.setReadOnly(True)
        self.btn_open_source = QPushButton("Otevřít soubor")
        self.btn_zoom_in = QPushButton("+"); self.btn_zoom_out = QPushButton("-"); self.btn_zoom_reset = QPushButton("Fit")
        sr.addWidget(QLabel("Zdroj:")); sr.addWidget(self.doc_src_line, 1); sr.addWidget(self.btn_open_source)
        sr.addWidget(self.btn_zoom_in); sr.addWidget(self.btn_zoom_out); sr.addWidget(self.btn_zoom_reset)
        rl.addWidget(srcrow)

        self.preview_view = PdfPreviewView()
        rl.addWidget(self.preview_view, 3)

        self.doc_items_table = QTableView()
        self.doc_items_table.setAlternatingRowColors(True)
        self.doc_items_table.setShowGrid(True)
        self.doc_items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.doc_items_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        rl.addWidget(self.doc_items_table, 2)

        items_bar = QWidget()
        ib = QHBoxLayout(items_bar); ib.setContentsMargins(0, 0, 0, 0)
        self.btn_items_add = QPushButton("Přidat položku")
        self.btn_items_del = QPushButton("Smazat položku")
        self.btn_items_save = QPushButton("Uložit změny")
        for b in (self.btn_items_add, self.btn_items_del, self.btn_items_save):
            b.setEnabled(False)
        ib.addWidget(self.btn_items_add)
        ib.addWidget(self.btn_items_del)
        ib.addStretch(1)
        ib.addWidget(self.btn_items_save)
        rl.addWidget(items_bar)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        dl2.addWidget(splitter, 1)
        self.tabs.addTab(self.tab_docs, "ÚČTY")

        # Nerozpoznané
        self.tab_unrec = QWidget()
        ul = QVBoxLayout(self.tab_unrec)

        split_u = QSplitter()
        split_u.setOrientation(Qt.Horizontal)

        # Left: seznam + editace hlavičky (IČO/…)
        left_u = QWidget(); lul = QVBoxLayout(left_u)
        self.unrec_table = QTableView()
        self.unrec_table.setAlternatingRowColors(True)
        self.unrec_table.setShowGrid(True)
        self.unrec_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.unrec_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.unrec_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        lul.addWidget(self.unrec_table, 2)

        editor = QWidget(); el = QFormLayout(editor)
        self.ed_u_ico = QLineEdit()
        self.btn_u_apply_ico = QPushButton("Načíst z ARES")
        ico_row_u = QWidget(); ico_l = QHBoxLayout(ico_row_u); ico_l.setContentsMargins(0, 0, 0, 0)
        ico_l.addWidget(self.ed_u_ico, 1)
        ico_l.addWidget(self.btn_u_apply_ico)

        self.ed_u_docno = QLineEdit()
        self.ed_u_bank = QLineEdit()
        self.ed_u_date = QDateEdit(); self.ed_u_date.setCalendarPopup(True); self.ed_u_date.setDisplayFormat("dd.MM.yyyy")
        self.ed_u_total = QDoubleSpinBox(); self.ed_u_total.setMaximum(1e12); self.ed_u_total.setDecimals(2)
        self.btn_u_save = QPushButton("Uložit jako hotové (vyjmout z karantény)")
        self.btn_u_save.setEnabled(False)

        el.addRow("IČO", ico_row_u)
        el.addRow("Číslo dokladu", self.ed_u_docno)
        el.addRow("Číslo účtu", self.ed_u_bank)
        el.addRow("Datum vystavení", self.ed_u_date)
        el.addRow("Cena celkem vč. DPH", self.ed_u_total)
        el.addRow(self.btn_u_save)
        lul.addWidget(editor, 1)

        split_u.addWidget(left_u)

        # Right: náhled + položky
        right_u = QWidget(); rul = QVBoxLayout(right_u)
        srcrow_u = QWidget(); sur = QHBoxLayout(srcrow_u); sur.setContentsMargins(0, 0, 0, 0)
        self.doc_src_line_u = QLineEdit(); self.doc_src_line_u.setReadOnly(True)
        self.btn_open_source_u = QPushButton("Otevřít soubor")
        self.btn_zoom_in_u = QPushButton("+"); self.btn_zoom_out_u = QPushButton("-"); self.btn_zoom_reset_u = QPushButton("Fit")
        sur.addWidget(QLabel("Zdroj:")); sur.addWidget(self.doc_src_line_u, 1); sur.addWidget(self.btn_open_source_u)
        sur.addWidget(self.btn_zoom_in_u); sur.addWidget(self.btn_zoom_out_u); sur.addWidget(self.btn_zoom_reset_u)
        rul.addWidget(srcrow_u)

        self.preview_view_u = PdfPreviewView()
        rul.addWidget(self.preview_view_u, 3)

        self.items_table_u = QTableView()
        self.items_table_u.setAlternatingRowColors(True)
        self.items_table_u.setShowGrid(True)
        self.items_table_u.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items_table_u.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        rul.addWidget(self.items_table_u, 2)

        items_bar_u = QWidget()
        iub = QHBoxLayout(items_bar_u); iub.setContentsMargins(0, 0, 0, 0)
        self.btn_u_item_add = QPushButton("Přidat položku")
        self.btn_u_item_del = QPushButton("Smazat položku")
        for b in (self.btn_u_item_add, self.btn_u_item_del):
            b.setEnabled(False)
        iub.addWidget(self.btn_u_item_add)
        iub.addWidget(self.btn_u_item_del)
        iub.addStretch(1)
        rul.addWidget(items_bar_u)

        split_u.addWidget(right_u)
        split_u.setStretchFactor(0, 2)
        split_u.setStretchFactor(1, 3)
        ul.addWidget(split_u, 1)
        self.tabs.addTab(self.tab_unrec, "NEROZPOZNANÉ")

        self.setCentralWidget(root)

        # Actions
        self.btn_exit.clicked.connect(self.close)
        self.btn_status.clicked.connect(self.on_show_status)
        self.btn_run.clicked.connect(self.on_run_service)
        self.btn_stop.clicked.connect(self.on_stop_service)
        self.btn_restart.clicked.connect(self.on_restart_service)

        self.btn_pick_input.clicked.connect(lambda: self._pick_dir(self.ed_input_dir))
        self.btn_pick_output.clicked.connect(lambda: self._pick_dir(self.ed_output_dir))
        self.btn_save_settings.clicked.connect(self.on_save_settings)
        self.btn_load_models.clicked.connect(self.on_load_models)

        self.btn_sup_refresh.clicked.connect(self.refresh_suppliers)
        self._sup_filter_timer.timeout.connect(self.refresh_suppliers)
        self.sup_filter.textChanged.connect(self._on_sup_filter_changed)
        self.btn_sup_add.clicked.connect(self.on_add_supplier)
        self.btn_sup_merge.clicked.connect(self.on_merge_suppliers)
        self.btn_sup_edit.clicked.connect(self.on_edit_supplier)
        self.btn_sup_save.clicked.connect(self.on_save_supplier)
        self.btn_sup_ares_detail.clicked.connect(self.on_supplier_ares)
        self.sup_table.clicked.connect(self.on_supplier_selected)

        # POLOŽKY (per-item search)
        self.btn_items_search.clicked.connect(self._items_new_search_v2)
        self.items_filter.returnPressed.connect(self._items_new_search_v2)
        self.btn_items_more.clicked.connect(self._items_load_more_v2)
        self.btn_items_open.clicked.connect(self._items_open_selected_v2)
        self.items_table.doubleClicked.connect(self._items_open_from_doubleclick_v2)

        # ÚČTY – nová logika (plně editovatelný detail položek)
        self.doc_filter.returnPressed.connect(self._docs_new_search_v2)
        self.btn_docs_search.clicked.connect(self._docs_new_search_v2)
        self.btn_docs_more.clicked.connect(self._docs_load_more_v2)
        self.cb_all_dates.stateChanged.connect(self._docs_new_search_v2)
        self.docs_table.clicked.connect(self._on_doc_selected_v2)
        self.btn_open_source.clicked.connect(self._open_selected_source_v2)
        self.btn_zoom_in.clicked.connect(self.preview_view.zoom_in)
        self.btn_zoom_out.clicked.connect(self.preview_view.zoom_out)
        self.btn_zoom_reset.clicked.connect(self.preview_view.reset_zoom)

        self.btn_items_add.clicked.connect(self._docs_item_add_v2)
        self.btn_items_del.clicked.connect(self._docs_item_del_v2)
        self.btn_items_save.clicked.connect(self._docs_items_save_v2)

        # NEROZPOZNANÉ – stejná struktura + ruční dopisování položek + dodavatel podle IČO
        self.unrec_table.clicked.connect(self._on_unrec_selected_v2)
        self.btn_u_apply_ico.clicked.connect(self._unrec_apply_ico_v2)
        self.btn_u_save.clicked.connect(self._unrec_save_done_v2)
        self.btn_open_source_u.clicked.connect(self._open_selected_unrec_source_v2)
        self.btn_zoom_in_u.clicked.connect(self.preview_view_u.zoom_in)
        self.btn_zoom_out_u.clicked.connect(self.preview_view_u.zoom_out)
        self.btn_zoom_reset_u.clicked.connect(self.preview_view_u.reset_zoom)
        self.btn_u_item_add.clicked.connect(self._unrec_item_add_v2)
        self.btn_u_item_del.clicked.connect(self._unrec_item_del_v2)

    def _wire_timers(self):
        self.timer = QTimer(self)
        self.timer.setInterval(int(self.cfg.get("performance", {}).get("ui_refresh_ms") or 1000))
        self.timer.timeout.connect(self._refresh_from_queue)
        self.timer.start()

    def _refresh_from_queue(self):
        try:
            self.refresh_dashboard()
        except Exception:
            pass

    def _pick_dir(self, line_edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Vyber adresář", line_edit.text() or str(Path.home()))
        if d:
            line_edit.setText(d)

    def _toggle_doc_dates(self, all_dates: bool) -> None:
        # UI helper: datumové filtry zbytečně matou, pokud je zvoleno „Vše“
        try:
            self.doc_date_from.setEnabled(not all_dates)
            self.doc_date_to.setEnabled(not all_dates)
        except Exception:
            pass

    # ---------------------------
    # POLOŽKY (per-item search)
    # ---------------------------

    def _items_new_search_v2(self) -> None:
        self._items_offset = 0
        self._items_rows = []
        self._items_current_path = None
        self._load_items_page_v2(reset=True)

    def _items_load_more_v2(self) -> None:
        if self._items_offset >= (self._items_total or 0):
            return
        self._load_items_page_v2(reset=False)

    def _load_items_page_v2(self, *, reset: bool) -> None:
        q = (self.items_filter.text() or "").strip()
        limit = int(self._items_page_size)
        offset = int(self._items_offset or 0)

        with self.sf() as session:
            total = db_api.count_items(session, q=q)
            rows = db_api.list_items(session, q=q, limit=limit, offset=offset)

        if reset:
            self._items_rows = []
        self._items_rows.extend(rows)
        self._items_total = int(total or 0)
        self._items_offset = len(self._items_rows)

        headers = ["Datum", "Dodavatel", "Položka", "Množství", "DPH %", "Celkem", "Doklad", "IČO"]
        trows = []
        for r in self._items_rows:
            issue = r.get("issue_date")
            if hasattr(issue, "strftime"):
                issue_s = issue.strftime("%Y-%m-%d")
            else:
                issue_s = str(issue or "")
            supplier = (r.get("supplier_name") or "").strip() or "(neznámý)"
            item_name = (r.get("item_name") or "").strip()
            qty = r.get("quantity")
            vat = r.get("vat_rate")
            total_ln = r.get("line_total")
            dn = (r.get("doc_number") or "").strip()
            ico = (r.get("supplier_ico") or "").strip()
            trows.append([issue_s, supplier, item_name, qty, vat, total_ln, dn, ico])

        self.items_table.setModel(TableModel(headers, trows))
        self.items_table.resizeColumnsToContents()
        self.lbl_items_page.setText(f"{self._items_offset} / {self._items_total}")
        self.btn_items_more.setEnabled(self._items_offset < self._items_total)

        try:
            sel_model = self.items_table.selectionModel()
            if sel_model:
                try:
                    sel_model.selectionChanged.connect(
                        self._items_selection_changed_v2,
                        Qt.ConnectionType.UniqueConnection,  # prevent duplicate connections when model resets
                    )
                except Exception:
                    # Some Qt bindings raise if already connected; ignore.
                    pass
        except Exception:
            pass

        if reset and self._items_rows:
            try:
                idx = self.items_table.model().index(0, 0)
                self.items_table.setCurrentIndex(idx)
                self.items_table.selectRow(0)
            except Exception:
                pass
        self._items_selection_changed_v2(None, None)

    def _items_selection_changed_v2(self, selected, deselected) -> None:
        try:
            sm = self.items_table.selectionModel()
            if not sm:
                return
            idxs = sm.selectedRows()
            if not idxs:
                self.items_preview.clear()
                self.items_src.setText("")
                self.lbl_items_doc.setText("")
                return
            row = int(idxs[0].row())
            meta = self._items_rows[row]
        except Exception:
            return

        path = meta.get("current_path")
        self._items_current_path = path
        self.items_src.setText(path or "")

        issue = meta.get("issue_date")
        if hasattr(issue, "strftime"):
            issue_s = issue.strftime("%Y-%m-%d")
        else:
            issue_s = str(issue or "")
        supplier = (meta.get("supplier_name") or "").strip() or "(neznámý)"
        ico = (meta.get("supplier_ico") or "").strip()
        dn = (meta.get("doc_number") or "").strip()
        doc_total = meta.get("doc_total_with_vat")
        self.lbl_items_doc.setText(f"{issue_s} | {supplier} | IČO {ico} | Doklad {dn} | Celkem {doc_total}")

        if path:
            QTimer.singleShot(0, lambda: self._load_preview(self.items_preview, path))
        else:
            self.items_preview.clear()

    def _items_open_selected_v2(self) -> None:
        self._open_file_path(self._items_current_path)

    def _items_open_from_doubleclick_v2(self, index) -> None:
        try:
            row = int(index.row())
            meta = self._items_rows[row]
        except Exception:
            return
        self._open_file_path(meta.get("current_path"))

    # ---------------------------
    # V2: ÚČTY + NEROZPOZNANÉ
    # ---------------------------

    def refresh_all_v2(self) -> None:
        # zachovej dashboard a dodavatele (pokud existují původní metody), ale listy účtů řídí V2
        try:
            self.refresh_dashboard()
        except Exception:
            pass
        try:
            self.refresh_suppliers()
        except Exception:
            pass
        try:
            self._items_new_search_v2()
        except Exception:
            pass
        self._docs_new_search_v2()
        self._refresh_unrec_v2()

    def _safe_unique_path(self, target: Path) -> Path:
        if not target.exists():
            return target
        stem = target.stem
        suf = target.suffix
        parent = target.parent
        for i in range(1, 10_000):
            cand = parent / f"{stem}_{i}{suf}"
            if not cand.exists():
                return cand
        return parent / f"{stem}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}{suf}"

    def _load_preview(self, view: PdfPreviewView, path_str: str | None) -> None:
        view.clear()
        if not path_str:
            return
        p = Path(path_str)
        if not p.exists():
            return
        try:
            if p.suffix.lower() == ".pdf":
                key = (str(p), int(self._preview_dpi))
                px = self._preview_cache.get(key)
                if px is None:
                    raw = self._render_pdf_preview_bytes(p, dpi=int(self._preview_dpi))
                    if not raw:
                        return
                    img = QImage.fromData(raw, "PNG")
                    px = QPixmap.fromImage(img)
                    self._preview_cache[key] = px
                view.set_pixmap(px)
            else:
                px = QPixmap(str(p))
                if not px.isNull():
                    view.set_pixmap(px)
        except Exception:
            # preview error: do not crash GUI
            self.log.exception("Preview load failed for %s", p)

    def _open_file_path(self, path_str: str | None) -> None:
        if not path_str:
            return
        p = Path(path_str)
        if not p.exists():
            QMessageBox.warning(self, "Soubor", "Soubor neexistuje.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    # ---- ÚČTY (listing + detail) ----

    def _docs_new_search_v2(self) -> None:
        self._doc_offset = 0
        self._toggle_doc_dates(bool(self.cb_all_dates.isChecked()))
        self._load_docs_page_v2(reset=True)

    def _docs_load_more_v2(self) -> None:
        self._load_docs_page_v2(reset=False)

    def _load_docs_page_v2(self, *, reset: bool) -> None:
        q = (self.doc_filter.text() or "").strip()
        date_from = None
        date_to = None
        if not self.cb_all_dates.isChecked():
            try:
                date_from = self.doc_date_from.date().toPython()
                date_to = self.doc_date_to.date().toPython()
            except Exception:
                date_from = None
                date_to = None

        with self.sf() as session:
            self._doc_total = db_api.count_documents(session, q=q, date_from=date_from, date_to=date_to)
            rows = db_api.list_documents(
                session,
                q=q,
                date_from=date_from,
                date_to=date_to,
                limit=int(self._doc_page_size),
                offset=int(self._doc_offset),
            )

            if reset:
                self._docs_listing: List[Dict[str, Any]] = []
            if not hasattr(self, "_docs_listing"):
                self._docs_listing = []

            doc_ids = [int(d.id) for d, _f in rows]
            # counts items per doc
            counts = {}
            if doc_ids:
                for did, c in session.execute(
                    select(LineItem.document_id, text("COUNT(*)")).where(LineItem.document_id.in_(doc_ids)).group_by(LineItem.document_id)
                ).all():
                    counts[int(did)] = int(c)
            # supplier names
            sup_ids = list({int(d.supplier_id) for d, _f in rows if d.supplier_id})
            sup_names: Dict[int, str] = {}
            if sup_ids:
                for s in session.execute(select(Supplier).where(Supplier.id.in_(sup_ids))).scalars().all():
                    sup_names[int(s.id)] = (s.name or s.ico or "").strip()

            for d, f in rows:
                did = int(d.id)
                self._docs_listing.append(
                    {
                        "doc_id": did,
                        "file_id": int(f.id),
                        "path": f.current_path,
                        "date": d.issue_date.isoformat() if d.issue_date else "",
                        "total": float(d.total_with_vat or 0.0) if d.total_with_vat is not None else 0.0,
                        "supplier": (sup_names.get(int(d.supplier_id)) if d.supplier_id else "") or (d.supplier_ico or "") or "",
                        "items_count": counts.get(did, 0),
                        "status": f.status or "",
                    }
                )

        # render table
        headers = ["Datum", "Celkem vč. DPH", "Dodavatel", "Počet položek", "Stav"]
        trows = [[r["date"], r["total"], r["supplier"], r["items_count"], r["status"]] for r in self._docs_listing]
        self.docs_table.setModel(TableModel(headers, trows))
        self._doc_offset = len(self._docs_listing)
        self.lbl_docs_page.setText(f"{self._doc_offset} / {self._doc_total}")

        # enable/disable "more"
        self.btn_docs_more.setEnabled(self._doc_offset < self._doc_total)

        # auto-select first row on reset and hook selection change
        try:
            sm = self.docs_table.selectionModel()
            if sm:
                sm.selectionChanged.connect(lambda *_: self._on_doc_selected_v2(sm.currentIndex()))
            if reset and self._docs_listing:
                idx = self.docs_table.model().index(0, 0)
                self.docs_table.setCurrentIndex(idx)
                self.docs_table.selectRow(0)
                self._on_doc_selected_v2(idx)
        except Exception:
            pass

    def _on_doc_selected_v2(self, index: QModelIndex) -> None:
        try:
            row = int(index.row())
        except Exception:
            return
        if not hasattr(self, "_docs_listing") or row < 0 or row >= len(self._docs_listing):
            return
        meta = self._docs_listing[row]
        doc_id = int(meta["doc_id"])
        with self.sf() as session:
            det = db_api.get_document_detail(session, doc_id)
            doc: Document = det["doc"]
            f: DocumentFile = det["file"]
            items: List[LineItem] = det["items"]

        self._current_doc_id = int(doc.id)
        self._current_doc_file_id = int(f.id) if f else None
        self._current_doc_path = f.current_path if f else None
        self.doc_src_line.setText(self._current_doc_path or "")

        rows = []
        for it in items:
            qty_val = float(it.quantity or 0.0)
            denom = qty_val if qty_val else 1.0
            try:
                unit_price_val = float(getattr(it, "unit_price", None) or (float(it.line_total or 0.0) / denom))
            except Exception:
                unit_price_val = 0.0
            rows.append(
                {
                    "id": int(it.id),
                    "name": it.name or "",
                    "quantity": float(it.quantity or 0.0),
                    "unit_price": unit_price_val,
                    "line_total": float(it.line_total or 0.0),
                    "vat_rate": float(it.vat_rate or 0.0),
                    "ean": getattr(it, "ean", "") or "",
                    "item_code": getattr(it, "item_code", "") or "",
                }
            )
        model = EditableItemsModel(rows)
        self._current_doc_items_model = model
        self.doc_items_table.setModel(model)
        self.doc_items_table.setSelectionMode(QAbstractItemView.SingleSelection)

        for b in (self.btn_items_add, self.btn_items_del, self.btn_items_save):
            b.setEnabled(True)

        # load preview after table is ready to avoid blocking UI
        QTimer.singleShot(0, lambda: self._load_preview(self.preview_view, self._current_doc_path))

    def _open_selected_source_v2(self) -> None:
        self._open_file_path(self._current_doc_path)

    def _docs_item_add_v2(self) -> None:
        if not self._current_doc_items_model:
            return
        self._current_doc_items_model.insertRows(self._current_doc_items_model.rowCount(), 1)

    def _docs_item_del_v2(self) -> None:
        if not self._current_doc_items_model:
            return
        sel = self.doc_items_table.selectionModel()
        if not sel or not sel.hasSelection():
            return
        row = int(sel.selectedRows()[0].row())
        self._current_doc_items_model.removeRows(row, 1)

    def _docs_items_save_v2(self) -> None:
        if not self._current_doc_id or not self._current_doc_items_model:
            return
        doc_id = int(self._current_doc_id)
        rows = self._current_doc_items_model.rows()
        with self.sf() as session:
            doc = session.get(Document, doc_id)
            if not doc:
                return
            existing = {int(it.id): it for it in session.execute(select(LineItem).where(LineItem.document_id == doc_id)).scalars().all()}

            # update + create
            new_items: List[LineItem] = []
            line_no = 1
            total_sum = 0.0
            keep_ids = set()
            for r in rows:
                rid = r.get("id")
                name = (r.get("name") or "").strip()
                if not name:
                    name = f"Položka {line_no}"
                qty = float(r.get("quantity") or 0.0)
                up = float(r.get("unit_price") or 0.0)
                lt = float(r.get("line_total") or 0.0)
                if not lt and (qty or up):
                    lt = qty * up
                vr = float(r.get("vat_rate") or 0.0)
                ean = (r.get("ean") or "").strip() or None
                code = (r.get("item_code") or "").strip() or None
                total_sum += float(lt or 0.0)

                if rid and int(rid) in existing:
                    it = existing[int(rid)]
                    it.line_no = line_no
                    it.name = name[:512]
                    it.quantity = qty
                    it.line_total = lt
                    it.vat_rate = vr
                    if hasattr(it, "ean"):
                        it.ean = ean
                    if hasattr(it, "item_code"):
                        it.item_code = code
                    session.add(it)
                    keep_ids.add(int(it.id))
                else:
                    it = LineItem(
                        document_id=doc_id,
                        line_no=line_no,
                        name=name[:512],
                        quantity=qty,
                        line_total=lt,
                        vat_rate=vr,
                    )
                    if hasattr(it, "ean"):
                        it.ean = ean
                    if hasattr(it, "item_code"):
                        it.item_code = code
                    session.add(it)
                    new_items.append(it)
                line_no += 1

            # delete removed
            for it_id, it in existing.items():
                if it_id not in keep_ids:
                    session.delete(it)

            # update total (volitelně)
            if total_sum and (doc.total_with_vat is None or abs(float(doc.total_with_vat or 0.0) - total_sum) > 0.01):
                doc.total_with_vat = float(total_sum)
                session.add(doc)

            session.flush()
            # rebuild FTS (text = položky)
            full_text = "\n".join([(r.get("name") or "").strip() for r in rows if (r.get("name") or "").strip()])
            rebuild_fts_for_document(session, doc_id, full_text=full_text)
            session.commit()

        QMessageBox.information(self, "Účty", "Položky byly uloženy.")
        self._docs_new_search_v2()
        try:
            self._items_new_search_v2()
        except Exception:
            pass

    # ---- NEROZPOZNANÉ ----

    def _refresh_unrec_v2(self) -> None:
        with self.sf() as session:
            rows = db_api.list_quarantine(session)
            doc_ids = [int(d.id) for d, _f in rows]
            counts = {}
            if doc_ids:
                for did, c in session.execute(
                    select(LineItem.document_id, text("COUNT(*)")).where(LineItem.document_id.in_(doc_ids)).group_by(LineItem.document_id)
                ).all():
                    counts[int(did)] = int(c)

        self._unrec_listing: List[Dict[str, Any]] = []
        for d, f in rows:
            did = int(d.id)
            self._unrec_listing.append(
                {
                    "doc_id": did,
                    "file_id": int(f.id),
                    "path": f.current_path,
                    "date": d.issue_date.isoformat() if d.issue_date else "",
                    "total": float(d.total_with_vat or 0.0) if d.total_with_vat is not None else 0.0,
                    "supplier_ico": (d.supplier_ico or "") or "",
                    "items_count": counts.get(did, 0),
                    "status": f.status or "",
                }
            )

        headers = ["Datum", "Celkem vč. DPH", "Dodavatel (IČO)", "Počet položek", "Stav"]
        trows = [[r["date"], r["total"], r["supplier_ico"], r["items_count"], r["status"]] for r in self._unrec_listing]
        self.unrec_table.setModel(TableModel(headers, trows))

        # clear detail
        self._current_unrec_doc_id = None
        self._current_unrec_file_id = None
        self._current_unrec_path = None
        self._current_unrec_items_model = None
        self.doc_src_line_u.setText("")
        self.preview_view_u.clear()
        self.items_table_u.setModel(TableModel([], []))
        self.btn_u_save.setEnabled(False)
        for b in (self.btn_u_item_add, self.btn_u_item_del):
            b.setEnabled(False)

        # auto-select first row and hook selection change
        try:
            sm = self.unrec_table.selectionModel()
            if sm:
                sm.selectionChanged.connect(lambda *_: self._on_unrec_selected_v2(sm.currentIndex()))
            if self._unrec_listing:
                idx = self.unrec_table.model().index(0, 0)
                self.unrec_table.setCurrentIndex(idx)
                self.unrec_table.selectRow(0)
                self._on_unrec_selected_v2(idx)
        except Exception:
            pass

    def _on_unrec_selected_v2(self, index: QModelIndex) -> None:
        try:
            row = int(index.row())
        except Exception:
            return
        if not hasattr(self, "_unrec_listing") or row < 0 or row >= len(self._unrec_listing):
            return
        meta = self._unrec_listing[row]
        doc_id = int(meta["doc_id"])
        with self.sf() as session:
            det = db_api.get_document_detail(session, doc_id)
            doc: Document = det["doc"]
            f: DocumentFile = det["file"]
            items: List[LineItem] = det["items"]

        self._current_unrec_doc_id = int(doc.id)
        self._current_unrec_file_id = int(f.id) if f else None
        self._current_unrec_path = f.current_path if f else None

        # fill header fields (jen co se povedlo vytěžit)
        self.ed_u_ico.setText(doc.supplier_ico or "")
        self.ed_u_docno.setText(doc.doc_number or "")
        self.ed_u_bank.setText(doc.bank_account or "")
        if doc.issue_date:
            try:
                self.ed_u_date.setDate(doc.issue_date)
            except Exception:
                pass
        self.ed_u_total.setValue(float(doc.total_with_vat or 0.0))

        self.doc_src_line_u.setText(self._current_unrec_path or "")

        rows = []
        for it in items:
            qty_val = float(it.quantity or 0.0)
            denom = qty_val if qty_val else 1.0
            try:
                unit_price_val = float(getattr(it, "unit_price", None) or (float(it.line_total or 0.0) / denom))
            except Exception:
                unit_price_val = 0.0
            rows.append(
                {
                    "id": int(it.id),
                    "name": it.name or "",
                    "quantity": float(it.quantity or 0.0),
                    "unit_price": unit_price_val,
                    "line_total": float(it.line_total or 0.0),
                    "vat_rate": float(it.vat_rate or 0.0),
                    "ean": getattr(it, "ean", "") or "",
                    "item_code": getattr(it, "item_code", "") or "",
                }
            )
        model = EditableItemsModel(rows)
        self._current_unrec_items_model = model
        self.items_table_u.setModel(model)
        self.items_table_u.setSelectionMode(QAbstractItemView.SingleSelection)

        self.btn_u_save.setEnabled(True)
        for b in (self.btn_u_item_add, self.btn_u_item_del):
            b.setEnabled(True)

        QTimer.singleShot(0, lambda: self._load_preview(self.preview_view_u, self._current_unrec_path))

    def _open_selected_unrec_source_v2(self) -> None:
        self._open_file_path(self._current_unrec_path)

    def _unrec_item_add_v2(self) -> None:
        if not self._current_unrec_items_model:
            return
        self._current_unrec_items_model.insertRows(self._current_unrec_items_model.rowCount(), 1)

    def _unrec_item_del_v2(self) -> None:
        if not self._current_unrec_items_model:
            return
        sel = self.items_table_u.selectionModel()
        if not sel or not sel.hasSelection():
            return
        row = int(sel.selectedRows()[0].row())
        self._current_unrec_items_model.removeRows(row, 1)

    def _unrec_apply_ico_v2(self) -> None:
        if not self._current_unrec_doc_id:
            return
        ico_raw = (self.ed_u_ico.text() or "").strip()
        if not ico_raw:
            QMessageBox.warning(self, "Dodavatel", "Zadejte IČO.")
            return
        ico = normalize_ico(ico_raw) or ico_raw

        def _fetch():
            return fetch_by_ico(ico)

        def _apply(data):
            with self.sf() as session:
                doc = session.get(Document, int(self._current_unrec_doc_id))
                if not doc:
                    return
                sup = None
                if data:
                    sup = upsert_supplier(
                        session,
                        ico=ico,
                        name=data.get("name"),
                        dic=data.get("dic"),
                        address=data.get("address"),
                        is_vat_payer=data.get("is_vat_payer"),
                        ares_last_sync=dt.datetime.utcnow(),
                        legal_form=data.get("legal_form"),
                        street=data.get("street"),
                        street_number=data.get("street_number"),
                        orientation_number=data.get("orientation_number"),
                        city=data.get("city"),
                        zip_code=data.get("zip_code"),
                        overwrite=True,
                    )
                else:
                    # minimální založení (pokud ARES nedá data)
                    sup = upsert_supplier(session, ico=ico, overwrite=False)

                doc.supplier_id = int(sup.id) if sup else None
                doc.supplier_ico = ico
                session.add(doc)
                session.flush()

                full_text = ""
                try:
                    if self._current_unrec_items_model:
                        full_text = "\n".join(
                            [(r.get("name") or "").strip() for r in self._current_unrec_items_model.rows() if (r.get("name") or "").strip()]
                        )
                except Exception:
                    full_text = ""
                rebuild_fts_for_document(session, int(doc.id), full_text=full_text)
                session.commit()

            QMessageBox.information(self, "Dodavatel", "Dodavatel byl aktualizován podle IČO.")
            self._docs_new_search_v2()

        self._run_with_busy("ARES", "Načítám dodavatele podle IČO…", _fetch, _apply)

    def _unrec_save_done_v2(self) -> None:
        if not self._current_unrec_doc_id or not self._current_unrec_file_id:
            return
        doc_id = int(self._current_unrec_doc_id)
        file_id = int(self._current_unrec_file_id)
        rows = self._current_unrec_items_model.rows() if self._current_unrec_items_model else []

        with self.sf() as session:
            doc = session.get(Document, doc_id)
            f = session.get(DocumentFile, file_id)
            if not doc or not f:
                return

            # update header fields
            doc.supplier_ico = (normalize_ico((self.ed_u_ico.text() or "").strip()) or (self.ed_u_ico.text() or "").strip()) or None
            doc.doc_number = (self.ed_u_docno.text() or "").strip() or None
            doc.bank_account = (self.ed_u_bank.text() or "").strip() or None
            try:
                doc.issue_date = self.ed_u_date.date().toPython()
            except Exception:
                pass
            doc.total_with_vat = float(self.ed_u_total.value())

            # items: update/create/delete
            existing = {int(it.id): it for it in session.execute(select(LineItem).where(LineItem.document_id == doc_id)).scalars().all()}
            keep_ids = set()
            line_no = 1
            total_sum = 0.0
            for r in rows:
                rid = r.get("id")
                name = (r.get("name") or "").strip() or f"Položka {line_no}"
                qty = float(r.get("quantity") or 0.0)
                up = float(r.get("unit_price") or 0.0)
                lt = float(r.get("line_total") or 0.0)
                if not lt and (qty or up):
                    lt = qty * up
                vr = float(r.get("vat_rate") or 0.0)
                ean = (r.get("ean") or "").strip() or None
                code = (r.get("item_code") or "").strip() or None
                total_sum += float(lt or 0.0)

                if rid and int(rid) in existing:
                    it = existing[int(rid)]
                    it.line_no = line_no
                    it.name = name[:512]
                    it.quantity = qty
                    it.line_total = lt
                    it.vat_rate = vr
                    if hasattr(it, "ean"):
                        it.ean = ean
                    if hasattr(it, "item_code"):
                        it.item_code = code
                    session.add(it)
                    keep_ids.add(int(it.id))
                else:
                    it = LineItem(
                        document_id=doc_id,
                        line_no=line_no,
                        name=name[:512],
                        quantity=qty,
                        line_total=lt,
                        vat_rate=vr,
                    )
                    if hasattr(it, "ean"):
                        it.ean = ean
                    if hasattr(it, "item_code"):
                        it.item_code = code
                    session.add(it)
                line_no += 1

            for it_id, it in existing.items():
                if it_id not in keep_ids:
                    session.delete(it)

            if total_sum and (doc.total_with_vat is None or abs(float(doc.total_with_vat or 0.0) - total_sum) > 0.01):
                doc.total_with_vat = float(total_sum)

            session.add(doc)
            session.flush()

            # rebuild FTS
            full_text = "\n".join([(r.get("name") or "").strip() for r in rows if (r.get("name") or "").strip()])
            rebuild_fts_for_document(session, doc_id, full_text=full_text)

            # move file out of quarantine to output_dir
            src = Path(f.current_path or "")
            out_dir = Path(self.cfg.get("paths", {}).get("output_dir") or "")
            if not out_dir:
                out_dir = self.paths.data_dir / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            dst = self._safe_unique_path(out_dir / src.name)
            try:
                shutil.move(str(src), str(dst))
            except Exception as exc:
                QMessageBox.critical(self, "Karanténa", f"Nepodařilo se přesunout soubor:\n{exc}")
                session.rollback()
                return

            f.current_path = str(dst)
            f.status = "PROCESSED"
            session.add(f)
            session.commit()

        QMessageBox.information(self, "Karanténa", "Doklad byl uložen jako hotový a přesunut z karantény.")
        self._refresh_unrec_v2()
        self._docs_new_search_v2()
        try:
            self._items_new_search_v2()
        except Exception:
            pass

    def _render_pdf_preview_bytes(self, path: Path, dpi: int = 160) -> bytes | None:
        """
        Render první stránku PDF. Dříve se dělalo multiprocesově, ale na Windows
        se spawn+join blokoval UI a padal na pickling, proto nyní přímo
        (rychlejší a stabilnější) s malou cache.
        """
        try:
            imgs = render_pdf_to_images(path, dpi=int(dpi), max_pages=1)
            if not imgs:
                return None
            buf = BytesIO()
            imgs[0].save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:
            self.log.exception("Preview render failed for %s: %s", path, exc)
            return None

    def _export_with_busy(self, fmt: str) -> None:
        """
        Export může obsahovat UI interakce (např. dialog pro výběr souboru),
        proto zůstává v UI vlákně. Použijeme modal „busy“ progress, aby uživatel
        dostal jasnou odezvu.
        """
        dlg = QProgressDialog("Exportuji…", None, 0, 0, self)
        dlg.setWindowTitle("Export")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.show()
        QApplication.processEvents()
        try:
            self._export(fmt)
        finally:
            try:
                dlg.close()
            except Exception:
                pass

    def _service_status(self) -> Dict[str, Any]:
        try:
            return send_cmd(self.cfg["service"].get("host", "127.0.0.1"), int(self.cfg["service"].get("port", 8765)), "status")
        except Exception as e:
            # Never crash UI because the service is down.
            self.log.warning("service status failed: %s", e)
            return {
                "ok": False,
                "running": False,
                "queue_size": None,
                "last_success": None,
                "last_error": str(e),
                "last_error_at": dt.datetime.utcnow().isoformat(),
                "last_seen": None,
            }

    def _start_service_process(self) -> bool:
        # Start service as detached process
        python = sys.executable
        cmd = [python, str(Path(__file__).resolve().parents[3] / "service_main.py"), "--config", str(self.config_path)]
        try:
            if os.name == "nt":
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS, close_fds=True)
            else:
                subprocess.Popen(cmd, start_new_session=True, close_fds=True)
            return True
        except Exception as e:
            QMessageBox.critical(self, "Chyba", f"Nelze spustit službu: {e}")
            return False

    def on_run_service(self):
        st = self._service_status()
        if st.get("ok") is not False and st.get("running"):
            QMessageBox.information(self, "RUN", "Služba už běží.")
            return
        if self._start_service_process():
            QMessageBox.information(self, "RUN", "Služba spuštěna.")

    def on_stop_service(self):
        try:
            send_cmd(self.cfg["service"].get("host", "127.0.0.1"), int(self.cfg["service"].get("port", 8765)), "stop")
        except Exception as e:
            # Stop should not crash if service is already down.
            QMessageBox.warning(self, "STOP", f"Službu se nepodařilo zastavit (možná neběží): {e}")

    def on_restart_service(self):
        self.on_stop_service()
        QTimer.singleShot(800, self.on_run_service)

    def on_show_status(self):
        st = self._service_status()
        d = StatusDialog(st, self)
        d.exec()

    def on_save_settings(self):
        deep_set(self.cfg, ["paths", "input_dir"], self.ed_input_dir.text().strip())
        deep_set(self.cfg, ["paths", "output_dir"], self.ed_output_dir.text().strip())
        enabled = self.cb_openai_enabled.currentText().lower() == "true"
        deep_set(self.cfg, ["openai", "enabled"], enabled)
        deep_set(self.cfg, ["openai", "api_key"], self.ed_api_key.text().strip())
        deep_set(self.cfg, ["openai", "model"], self.cb_model.currentText().strip())
        save_yaml(self.config_path, self.cfg)
        QMessageBox.information(self, "Nastavení", "Uloženo.")

    def on_load_models(self):
        api_key = self.ed_api_key.text().strip() or str(self.cfg.get("openai", {}).get("api_key") or "")
        if not api_key:
            QMessageBox.warning(self, "Modely", "Nejdřív zadej API-KEY.")
            return
        def work():
            return list_models(api_key)
        def done(models: List[str]):
            self.cb_model.clear()
            self.cb_model.addItems(models)
            QMessageBox.information(self, "Modely", f"Načteno: {len(models)}")
        self._run_with_busy("Modely", "Načítám dostupné modely…", work, done)

    def refresh_all(self):
        self.refresh_dashboard()
        self.refresh_suppliers()
        self.refresh_documents()
        self.refresh_unrecognized()
        self.refresh_ops()
        self.refresh_suspicious()
        self.refresh_money()

    def refresh_dashboard(self):
        try:
            with self.sf() as session:
                c = db_api.counts(session)
            self.lbl_unprocessed.setText(f"Nezpracované: {c['unprocessed']}")
            self.lbl_processed.setText(f"Zpracované: {c['processed']}")
            self.lbl_docs.setText(f"Účty: {c['documents']}")
            self.lbl_suppliers.setText(f"Dodavatelé: {c['suppliers']}")
        except Exception:
            self.log.exception("refresh_dashboard counts failed")
        st = self._service_status()
        running = bool(st.get("running"))
        self.lbl_service.setText(f"Služba: {'běží' if running else 'neběží'} | fronta: {st.get('queue_size')}")
        qsz = st.get("queue_size") or 0
        self.progress.setValue(0 if qsz == 0 else min(95, int(100 / (qsz + 1))))

    def refresh_suppliers(self):
        q = self.sup_filter.text()
        try:
            with self.sf() as session:
                sups = db_api.list_suppliers(session, q=q)
        except Exception as e:
            QMessageBox.critical(self, "Dodavatelé", f"Nepodařilo se načíst seznam dodavatelů: {e}")
            return

        keep_id = self._selected_supplier_id()
        rows: List[List[str]] = []
        for s in sups:
            rows.append([
                str(s.id),
                s.name or "",
                s.ico,
                s.dic or "",
                s.city or "",
            ])
        self.sup_model = TableModel(["ID", "Název", "IČO", "DIČ", "Místo sídla"], rows)
        self.sup_table.setModel(self.sup_model)
        self.sup_table.setColumnHidden(0, True)

        sm = self.sup_table.selectionModel()
        if sm and not self._sup_sel_connected:
            sm.selectionChanged.connect(self._on_sup_selection_changed)
            self._sup_sel_connected = True
        elif sm is None:
            self._sup_sel_connected = False

        if keep_id is not None:
            self._select_supplier_in_table(keep_id)
        else:
            self._on_sup_selection_changed()

    def _selected_supplier_ids(self) -> List[int]:
        sm = self.sup_table.selectionModel()
        if sm is None:
            return []
        ids: List[int] = []
        for r in sm.selectedRows():
            try:
                ids.append(int(self.sup_model.rows[r.row()][0]))
            except Exception:
                pass
        return sorted(set(ids))

    def _selected_supplier_id(self) -> Optional[int]:
        ids = self._selected_supplier_ids()
        return ids[0] if len(ids) == 1 else None

    def _select_supplier_in_table(self, supplier_id: int) -> None:
        for i, row in enumerate(self.sup_model.rows):
            if str(supplier_id) == str(row[0]):
                idx = self.sup_model.index(i, 1)
                self.sup_table.setCurrentIndex(idx)
                self.sup_table.selectRow(i)
                break

    def _on_sup_selection_changed(self, *args, **kwargs):
        self.on_supplier_selected()

    def on_supplier_selected(self):
        ids = self._selected_supplier_ids()
        self.btn_sup_merge.setEnabled(len(ids) >= 2)
        if len(ids) != 1:
            self._clear_supplier_detail(
                note=("Nevybrán žádný dodavatel." if not ids else f"Vybráno {len(ids)} dodavatelů (pro detail vyber přesně 1).")
            )
            return
        self._load_supplier_detail(ids[0])

    def on_add_supplier(self):
        dlg = SupplierDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()
        ico = (vals.get("ico") or "").strip()
        if not ico:
            QMessageBox.warning(self, "Dodavatelé", "IČO je povinné.")
            return
        try:
            with self.sf() as session:
                s = session.execute(select(Supplier).where(Supplier.ico == ico)).scalar_one_or_none()
                if not s:
                    s = Supplier(ico=ico)
                s.name = vals.get("name")
                s.dic = vals.get("dic")
                s.legal_form = vals.get("legal_form")
                s.street = vals.get("street")
                s.street_number = vals.get("street_number")
                s.orientation_number = vals.get("orientation_number")
                s.city = vals.get("city")
                s.zip_code = vals.get("zip_code")
                s.address = vals.get("address")
                s.is_vat_payer = bool(vals.get("is_vat_payer"))
                session.add(s)
                session.commit()
                new_id = int(s.id)
        except Exception as e:
            QMessageBox.critical(self, "Dodavatelé", f"Nelze uložit: {e}")
            return
        self.refresh_suppliers()
        try:
            self._select_supplier_in_table(new_id)
        except Exception:
            pass

    def _set_supplier_editing(self, enabled: bool) -> None:
        for w in [
            self.sup_name,
            self.sup_dic,
            self.sup_legal_form,
            self.sup_street,
            self.sup_street_number,
            self.sup_orientation_number,
            self.sup_city,
            self.sup_zip,
        ]:
            w.setReadOnly(not enabled)
        self.sup_vat.setEnabled(enabled)
        self.btn_sup_save.setEnabled(enabled)

    def on_supplier_ares(self):
        sid = self._selected_supplier_id()
        if sid is None:
            QMessageBox.warning(self, "ARES", "Vyber přesně jednoho dodavatele v tabulce.")
            return
        ico_raw = self.sup_ico.text().strip()
        try:
            ico = normalize_ico(ico_raw)
        except Exception as e:
            QMessageBox.warning(self, "ARES", f"Neplatné IČO: {e}")
            return
        if not ico:
            QMessageBox.warning(self, "ARES", "Dodavatel nemá IČO.")
            return

        def work():
            return fetch_by_ico(ico)

        def done(rec):
            with self.sf() as session:
                s = upsert_supplier(
                    session,
                    rec.ico,
                    name=rec.name,
                    dic=rec.dic,
                    address=rec.address,
                    is_vat_payer=rec.is_vat_payer,
                    ares_last_sync=rec.fetched_at,
                    legal_form=rec.legal_form,
                    street=rec.street,
                    street_number=rec.street_number,
                    orientation_number=rec.orientation_number,
                    city=rec.city,
                    zip_code=rec.zip_code,
                    overwrite=True,
                )
                session.commit()
                sid_loc = int(s.id)
            self.refresh_suppliers()
            self._select_supplier_in_table(sid_loc)

        self._run_with_busy("ARES", "Načítám data z ARES…", work, done, timeout_ms=20000)

    def on_edit_supplier(self):
        sid = self._selected_supplier_id()
        if sid is None:
            QMessageBox.information(self, "Dodavatelé", "Vyber přesně jednoho dodavatele.")
            return
        self._set_supplier_editing(True)

    def on_save_supplier(self):
        sid = self._selected_supplier_id()
        if sid is None:
            QMessageBox.information(self, "Dodavatelé", "Vyber přesně jednoho dodavatele.")
            return
        try:
            with self.sf() as session:
                s = session.get(Supplier, sid)
                if not s:
                    raise KeyError(sid)
                s.name = self.sup_name.text().strip() or None
                s.dic = self.sup_dic.text().strip() or None
                s.legal_form = self.sup_legal_form.text().strip() or None
                s.street = self.sup_street.text().strip() or None
                s.street_number = self.sup_street_number.text().strip() or None
                s.orientation_number = self.sup_orientation_number.text().strip() or None
                s.city = self.sup_city.text().strip() or None
                s.zip_code = self.sup_zip.text().strip() or None
                s.is_vat_payer = bool(self.sup_vat.isChecked())
                s.address = _format_supplier_address(s.street, s.street_number, s.orientation_number, s.city, s.zip_code)
                session.add(s)
                session.commit()
            self._set_supplier_editing(False)
            self.refresh_suppliers()
            self._select_supplier_in_table(int(sid))
        except Exception as e:
            QMessageBox.critical(self, "Dodavatelé", f"Nelze uložit: {e}")

    def on_merge_suppliers(self):
        ids = self._selected_supplier_ids()
        if len(ids) < 2:
            QMessageBox.information(self, "Sloučit", "Vyber alespoň dva dodavatele.")
            return
        try:
            with self.sf() as session:
                sups = session.execute(select(Supplier).where(Supplier.id.in_(ids))).scalars().all()
                by_id = {int(s.id): s for s in sups}
                items = [f"{by_id[i].name or ''} ({by_id[i].ico}) [ID {i}]" for i in ids if i in by_id]
            choice, ok = QInputDialog.getItem(
                self,
                "Sloučit dodavatele",
                "Vyber cílového dodavatele (na něj se přesunou všechny doklady):",
                items,
                0,
                False,
            )
            if not ok or not choice:
                return
            keep_id = int(choice.split("[ID", 1)[1].split("]", 1)[0].strip())
            other_ids = [i for i in ids if i != keep_id]
            if QMessageBox.question(
                self,
                "Potvrdit sloučení",
                f"Přesunout doklady na ID {keep_id} a smazat {len(other_ids)} dodavatele?",
            ) != QMessageBox.Yes:
                return
            with self.sf() as session:
                db_api.merge_suppliers(session, keep_id=keep_id, merge_ids=ids)
                session.commit()
            self.refresh_suppliers()
            self._select_supplier_in_table(keep_id)
        except Exception as e:
            QMessageBox.critical(self, "Sloučit", f"Sloučení selhalo: {e}")

    def _load_supplier_detail(self, supplier_id: int) -> None:
        with self.sf() as session:
            s = session.get(Supplier, supplier_id)
            if not s:
                self._clear_supplier_detail(note="Dodavatel nenalezen.")
                return
            self.lbl_sup_detail.setText(f"Detail dodavatele (ID {s.id})")
            self.sup_id.setText(str(s.id))
            self.sup_ico.setText(s.ico or "")
            self.sup_name.setText(s.name or "")
            self.sup_dic.setText(s.dic or "")
            self.sup_legal_form.setText(s.legal_form or "")
            self.sup_vat.setChecked(bool(s.is_vat_payer))
            self.sup_street.setText(s.street or "")
            self.sup_street_number.setText(s.street_number or "")
            self.sup_orientation_number.setText(s.orientation_number or "")
            self.sup_city.setText(s.city or "")
            self.sup_zip.setText(s.zip_code or "")
        self._set_supplier_editing(False)

    def _clear_supplier_detail(self, note: str = "") -> None:
        self.lbl_sup_detail.setText(note or "Detail dodavatele")
        for w in [
            self.sup_id,
            self.sup_ico,
            self.sup_name,
            self.sup_dic,
            self.sup_legal_form,
            self.sup_street,
            self.sup_street_number,
            self.sup_orientation_number,
            self.sup_city,
            self.sup_zip,
        ]:
            w.setText("")
        self.sup_vat.setChecked(False)
        self._set_supplier_editing(False)

    def _on_sup_filter_changed(self, _=None):
        ms = int(self.cfg.get("performance", {}).get("supplier_debounce_ms", 250) or 250)
        try:
            self._sup_filter_timer.stop()
        except Exception:
            pass
        self._sup_filter_timer.start(ms)

    def _current_doc_filters(self):
        q = (self.doc_filter.text() or "").strip()
        if self.cb_all_dates.isChecked():
            return q, None, None
        df = self.doc_date_from.date().toPython()
        dtv = self.doc_date_to.date().toPython()
        if df and dtv and df > dtv:
            df, dtv = dtv, df
        return q, df, dtv

    def _docs_new_search(self):
        self._doc_offset = 0
        self._preview_cache.clear()
        self.preview_view.clear()
        self._refresh_documents_page(reset=True)

    def _docs_load_more(self):
        if self._doc_offset >= self._doc_total:
            return
        self._refresh_documents_page(reset=False)

    def _refresh_documents_page(self, reset: bool):
        self._toggle_doc_dates(self.cb_all_dates.isChecked())
        q, dfrom, dto = self._current_doc_filters()
        with self.sf() as session:
            self._doc_total = db_api.count_documents(session, q=q, date_from=dfrom, date_to=dto)
            docs = db_api.list_documents(
                session,
                q=q,
                date_from=dfrom,
                date_to=dto,
                limit=self._doc_page_size,
                offset=self._doc_offset,
            )

        shown = min(self._doc_offset + len(docs), self._doc_total)
        self.lbl_docs_page.setText(f"{shown} / {self._doc_total}")

        headers = ["ID", "Datum", "IČO", "Číslo", "Účet", "Celkem", "Měna", "Kontrola"]
        new_rows: List[List[Any]] = []
        for d, f in docs:
            new_rows.append([
                d.id,
                d.issue_date.isoformat() if d.issue_date else "",
                d.supplier_ico or "",
                d.doc_number or "",
                d.bank_account or "",
                d.total_with_vat or 0.0,
                d.currency or "",
                "ANO" if d.requires_review else "",
            ])

        if reset or self.docs_table.model() is None:
            model = TableModel(headers, new_rows)
        else:
            model = self.docs_table.model()
            try:
                model.rows.extend(new_rows)  # type: ignore[attr-defined]
                model.layoutChanged.emit()
            except Exception:
                model = TableModel(headers, new_rows)
        self.docs_table.setModel(model)
        self.docs_table.resizeColumnsToContents()

        # connect selection change for new model and optionally preselect first row on reset
        try:
            sm = self.docs_table.selectionModel()
            if sm is not None:
                sm.selectionChanged.connect(lambda *_: self._on_doc_selected_fast())
            if reset and getattr(model, "rows", None):
                idx = model.index(0, 0)
                self.docs_table.setCurrentIndex(idx)
                self.docs_table.selectRow(0)
                self._on_doc_selected_fast()
        except Exception:
            pass

        self._doc_offset += len(docs)

    def refresh_documents(self):
        # backward compatibility for existing call sites
        self._docs_new_search()

    def _open_selected_source(self):
        p = (self.doc_src_line.text() or "").strip()
        if not p:
            return
        fp = Path(p)
        if not fp.exists():
            QMessageBox.warning(self, "Soubor", "Soubor neexistuje.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(fp)))

    def _on_doc_selected_fast(self, *_):
        model = self.docs_table.model()
        if model is None:
            return
        sm = self.docs_table.selectionModel()
        idx = self.docs_table.currentIndex()
        if (not idx.isValid()) and sm is not None:
            rows = sm.selectedRows()
            if rows:
                idx = rows[0]
        if not idx.isValid():
            return
        try:
            doc_id = int(model.rows[idx.row()][0])  # type: ignore[attr-defined]
        except Exception:
            return

        with self.sf() as session:
            try:
                detail = db_api.get_document_detail(session, doc_id)
                d: Document = detail["doc"]
                f: DocumentFile = detail["file"]
                items: List[LineItem] = detail["items"]
            except Exception:
                return

        src = f.current_path if f else ""
        self.doc_src_line.setText(src or "")

        item_headers = ["#", "Název", "Množství", "DPH", "Cena"]
        item_rows: List[List[Any]] = []
        for it in items:
            item_rows.append([it.line_no, it.name, it.quantity, it.vat_rate, it.line_total])
        self.doc_items_table.setModel(TableModel(item_headers, item_rows))
        self.doc_items_table.resizeColumnsToContents()

        self.preview_view.clear()
        if src and src.lower().endswith(".pdf") and Path(src).exists():
            key = (src, self._preview_dpi)
            px = self._preview_cache.get(key)
            if px is None:
                try:
                    imgs = render_pdf_to_images(Path(src), dpi=self._preview_dpi, max_pages=1)
                    if imgs:
                        px = pil_to_pixmap(imgs[0])
                        self._preview_cache[key] = px
                except Exception:
                    px = QPixmap()
            if px and not px.isNull():
                self.preview_view.set_pixmap(px)

    def refresh_unrecognized(self):
        with self.sf() as session:
            ql = db_api.list_quarantine(session)
            rows = []
            for d, f in ql:
                rows.append([d.id, d.issue_date.isoformat() if d.issue_date else "", d.supplier_ico or "", d.doc_number or "", d.total_with_vat or "", f.current_path, d.review_reasons or ""]) 
        self.unrec_model = TableModel(["ID", "Datum", "IČO", "Číslo", "Celkem", "Soubor", "Důvod"], rows)
        self.unrec_table.setModel(self.unrec_model)

    def on_unrec_selected(self, index):
        try:
            doc_id = int(self.unrec_model.rows[index.row()][0])
        except Exception:
            return
        with self.sf() as session:
            detail = db_api.get_document_detail(session, doc_id)
            d: Document = detail["doc"]
        self._current_unrec_doc_id = doc_id
        self.ed_u_ico.setText(d.supplier_ico or "")
        self.ed_u_docno.setText(d.doc_number or "")
        self.ed_u_bank.setText(d.bank_account or "")
        if d.issue_date:
            self.ed_u_date.setDate(d.issue_date)
        self.ed_u_total.setValue(float(d.total_with_vat or 0.0))

    def on_unrec_save(self):
        doc_id = getattr(self, "_current_unrec_doc_id", None)
        if not doc_id:
            return
        with self.sf() as session:
            d = session.get(Document, doc_id)
            if not d:
                return
            f = session.get(DocumentFile, d.file_id)
            if not f:
                return
            d.supplier_ico = self.ed_u_ico.text().strip() or None
            d.doc_number = self.ed_u_docno.text().strip() or None
            d.bank_account = self.ed_u_bank.text().strip() or None
            d.issue_date = self.ed_u_date.date().toPython()
            d.total_with_vat = float(self.ed_u_total.value())
            d.extraction_method = "manual"
            d.extraction_confidence = 1.0
            d.requires_review = False
            d.review_reasons = None
            # move file from quarantine to output root
            out_dir = Path(self.cfg["paths"]["output_dir"])
            p = Path(f.current_path)
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                new_p = out_dir / p.name
                if new_p.exists():
                    new_p = out_dir / f"{p.stem}_{f.sha256[:8]}{p.suffix}"
                os.rename(p, new_p)
                f.current_path = str(new_p)
                f.status = "PROCESSED"
                session.add(f)
            except Exception as e:
                QMessageBox.critical(self, "Chyba", f"Přesun selhal: {e}")
                return
            session.add(d)
            session.commit()
        QMessageBox.information(self, "Uloženo", "Doklad uložen a vyjmut z karantény.")
        self.refresh_unrecognized()
        self.refresh_documents()

    def closeEvent(self, event):
        # Make sure background threads are stopped to avoid PySide warnings.
        for th in list(self._threads):
            try:
                if th.isRunning():
                    th.quit()
                    th.wait(2000)
            except Exception:
                pass
        super().closeEvent(event)

    def refresh_ops(self):
        with self.sf() as session:
            jobs = db_api.service_jobs(session, limit=200)
            rows = []
            for j in jobs:
                rows.append([j.created_at.isoformat(sep=" ", timespec="seconds"), j.status, j.path, j.error or "", j.finished_at.isoformat(sep=" ", timespec="seconds") if j.finished_at else ""]) 
        self.ops_table.setModel(TableModel(["Vytvořeno", "Status", "Soubor", "Chyba", "Dokončeno"], rows))

    def refresh_suspicious(self):
        with self.sf() as session:
            stmt = select(Document, DocumentFile).join(DocumentFile, DocumentFile.id == Document.file_id).where(Document.requires_review == True)  # noqa
            data = session.execute(stmt).all()
            rows = []
            for d, f in data:
                rows.append([d.id, d.issue_date.isoformat() if d.issue_date else "", d.supplier_ico or "", d.doc_number or "", d.total_with_vat or "", f.status, d.review_reasons or ""]) 
        self.susp_table.setModel(TableModel(["ID", "Datum", "IČO", "Číslo", "Celkem", "Status", "Důvod"], rows))

    def refresh_money(self):
        with self.sf() as session:
            # simple aggregates by month and vat rate
            rows = session.execute(
                text(
                    """
                    SELECT strftime('%Y-%m', issue_date) as ym, currency, sum(total_with_vat) as total, count(*) as cnt
                    FROM documents
                    WHERE issue_date IS NOT NULL
                    GROUP BY ym, currency
                    ORDER BY ym DESC
                    LIMIT 24
                    """
                )
            ).fetchall()
            txt = ["Rozpad výdajů (měsíc):"]
            for ym, cur, total, cnt in rows:
                total_val = float(total or 0)
                txt.append(f"{ym} {cur}: {total_val:,.2f} ({int(cnt or 0)} dokladů)".replace(",", " "))
            top = session.execute(
                text(
                    """
                    SELECT supplier_ico, sum(total_with_vat) as total, count(*) as cnt
                    FROM documents
                    WHERE supplier_ico IS NOT NULL
                    GROUP BY supplier_ico
                    ORDER BY total DESC
                    LIMIT 10
                    """
                )
            ).fetchall()
            txt.append("\nTop dodavatelé (objem):")
            for ico, total, cnt in top:
                total_val = float(total or 0)
                txt.append(f"{ico}: {total_val:,.2f} ({int(cnt or 0)})".replace(",", " "))
            qcnt = session.execute(select(Document).where(Document.requires_review == True)).scalars().all()  # noqa
            txt.append(f"\nVyžaduje kontrolu: {len(qcnt)}")
        self.money_summary.setText("\n".join(txt))

    def _export(self, kind: str):
        # export current filtered documents (same filter as in UI)
        q, dfrom, dto = self._current_doc_filters()

        default_name = f"kajovospend_export_{dt.date.today().isoformat()}.{kind}"
        path, _ = QFileDialog.getSaveFileName(self, f"Uložit {kind.upper()}", str(Path.home() / default_name))
        if not path:
            return

        with self.sf() as session:
            docs = db_api.list_documents(session, q=q, date_from=dfrom, date_to=dto)
            # Flatten for export including line items.
            rows: List[Dict[str, Any]] = []
            for d, f in docs:
                items = session.execute(select(LineItem).where(LineItem.document_id == d.id).order_by(LineItem.line_no)).scalars().all()
                if not items:
                    rows.append({
                        "document_id": d.id,
                        "issue_date": d.issue_date.isoformat() if d.issue_date else None,
                        "supplier_ico": d.supplier_ico,
                        "doc_number": d.doc_number,
                        "bank_account": d.bank_account,
                        "total_with_vat": d.total_with_vat,
                        "currency": d.currency,
                        "requires_review": bool(d.requires_review),
                        "review_reasons": d.review_reasons,
                        "file_path": f.current_path,
                        "item_line_no": None,
                        "item_name": None,
                        "item_quantity": None,
                        "item_vat_rate": None,
                        "item_line_total": None,
                    })
                else:
                    for it in items:
                        rows.append({
                            "document_id": d.id,
                            "issue_date": d.issue_date.isoformat() if d.issue_date else None,
                            "supplier_ico": d.supplier_ico,
                            "doc_number": d.doc_number,
                            "bank_account": d.bank_account,
                            "total_with_vat": d.total_with_vat,
                            "currency": d.currency,
                            "requires_review": bool(d.requires_review),
                            "review_reasons": d.review_reasons,
                            "file_path": f.current_path,
                            "item_line_no": it.line_no,
                            "item_name": it.name,
                            "item_quantity": it.quantity,
                            "item_vat_rate": it.vat_rate,
                            "item_line_total": it.line_total,
                        })

        try:
            if kind == "csv":
                import csv
                with open(path, "w", newline="", encoding="utf-8") as fp:
                    w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()) if rows else [])
                    w.writeheader()
                    for r in rows:
                        w.writerow(r)
            elif kind == "xlsx":
                import pandas as pd
                df = pd.DataFrame(rows)
                df.to_excel(path, index=False)
            else:
                raise ValueError(f"unknown export kind: {kind}")
        except Exception as e:
            QMessageBox.critical(self, "Export", f"Export selhal: {e}")
            return
        QMessageBox.information(self, "Export", f"Uloženo: {path}")
