from __future__ import annotations

import datetime as dt
import os
import queue
import subprocess
import sys
import multiprocessing as mp
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QAbstractTableModel, QModelIndex, QObject, Signal, Slot, QThread
from PySide6.QtGui import QIcon, QPixmap, QImage
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget, QTableView,
    QLineEdit, QFormLayout, QSplitter, QTextEdit, QDoubleSpinBox, QSpinBox, QComboBox, QFileDialog,
    QMessageBox, QDateEdit, QProgressBar, QDialog, QDialogButtonBox, QHeaderView, QAbstractItemView,
    QCheckBox, QProgressDialog, QApplication, QInputDialog,
)

from sqlalchemy.orm import Session
from sqlalchemy import select, text

from kajovospend.utils.config import load_yaml, save_yaml, deep_set
from kajovospend.utils.paths import resolve_app_paths
from kajovospend.utils.logging_setup import setup_logging
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.db.migrate import init_db
from kajovospend.db.models import Supplier, Document, DocumentFile, LineItem
from kajovospend.db.queries import upsert_supplier
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico
from kajovospend.integrations.openai_fallback import list_models
from kajovospend.service.control_client import send_cmd
from kajovospend.ocr.pdf_render import render_pdf_to_images

from .styles import QSS
from . import db_api


class TableModel(QAbstractTableModel):
    def __init__(self, headers: List[str], rows: List[List[Any]]):
        super().__init__()
        self.headers = headers
        self.rows = rows

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
        self.refresh_all()

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

        # Účty
        self.tab_docs = QWidget()
        dl2 = QVBoxLayout(self.tab_docs)
        filters = QWidget(); filters.setProperty("panel", True)
        fl = QHBoxLayout(filters); fl.setContentsMargins(10,6,10,6)
        self.ed_doc_search = QLineEdit(); self.ed_doc_search.setPlaceholderText("Číslo, VS/KS/SS, text…")
        self.cb_all_dates = QCheckBox("Vše"); self.cb_all_dates.setToolTip("Ignorovat datumové omezení a zobrazit všechny doklady/účty"); self.cb_all_dates.setChecked(True)
        self.dt_from = QDateEdit(); self.dt_from.setCalendarPopup(True); self.dt_from.setDisplayFormat("dd.MM.yyyy")
        self.dt_to = QDateEdit(); self.dt_to.setCalendarPopup(True); self.dt_to.setDisplayFormat("dd.MM.yyyy")
        self.dt_from.setDate(dt.date.today() - dt.timedelta(days=365))
        self.dt_to.setDate(dt.date.today())
        self.btn_doc_search = QPushButton("Hledat")
        self.btn_export_csv = QPushButton("Export CSV")
        self.btn_export_xlsx = QPushButton("Export XLSX")
        fl.addWidget(self.ed_doc_search, 1)
        fl.addWidget(self.cb_all_dates)
        fl.addWidget(self.dt_from)
        fl.addWidget(self.dt_to)
        fl.addWidget(self.btn_doc_search)
        fl.addWidget(self.btn_export_csv)
        fl.addWidget(self.btn_export_xlsx)
        dl2.addWidget(filters)
        # start state
        self._toggle_doc_dates(True)

        splitter = QSplitter()
        left = QWidget(); ll = QVBoxLayout(left)
        self.doc_table = QTableView()
        self.doc_table.setAlternatingRowColors(True)
        self.doc_table.setShowGrid(True)
        self.doc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.doc_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.doc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ll.addWidget(self.doc_table, 1)
        splitter.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right)
        self.preview = QLabel("Náhled")
        self.preview.setObjectName("PreviewBox")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(420)
        self.items_table = QTableView()
        self.items_table.setAlternatingRowColors(True)
        self.items_table.setShowGrid(True)
        self.items_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        rl.addWidget(self.preview, 3)
        rl.addWidget(self.items_table, 2)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        dl2.addWidget(splitter, 1)
        self.tabs.addTab(self.tab_docs, "ÚČTY")

        # Nerozpoznané
        self.tab_unrec = QWidget()
        ul = QVBoxLayout(self.tab_unrec)
        self.unrec_table = QTableView()
        self.unrec_table.setAlternatingRowColors(True)
        self.unrec_table.setShowGrid(True)
        self.unrec_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.unrec_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.unrec_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ul.addWidget(self.unrec_table, 2)

        editor = QWidget(); el = QFormLayout(editor)
        self.ed_u_ico = QLineEdit(); self.ed_u_docno = QLineEdit(); self.ed_u_bank = QLineEdit()
        self.ed_u_date = QDateEdit(); self.ed_u_date.setCalendarPopup(True); self.ed_u_date.setDisplayFormat("dd.MM.yyyy")
        self.ed_u_total = QDoubleSpinBox(); self.ed_u_total.setMaximum(1e12); self.ed_u_total.setDecimals(2)
        self.btn_u_save = QPushButton("Uložit a vyjmout z karantény")
        el.addRow("IČO", self.ed_u_ico)
        el.addRow("Číslo dokladu", self.ed_u_docno)
        el.addRow("Číslo účtu", self.ed_u_bank)
        el.addRow("Datum vystavení", self.ed_u_date)
        el.addRow("Cena celkem vč. DPH", self.ed_u_total)
        el.addRow(self.btn_u_save)

        ul.addWidget(editor, 1)
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
        self.sup_filter.textChanged.connect(lambda _=None: self.refresh_suppliers())
        self.btn_sup_add.clicked.connect(self.on_add_supplier)
        self.btn_sup_merge.clicked.connect(self.on_merge_suppliers)
        self.btn_sup_edit.clicked.connect(self.on_edit_supplier)
        self.btn_sup_save.clicked.connect(self.on_save_supplier)
        self.btn_sup_ares_detail.clicked.connect(self.on_supplier_ares)
        self.sup_table.clicked.connect(self.on_supplier_selected)

        self.btn_doc_search.clicked.connect(self.refresh_documents)
        self.ed_doc_search.returnPressed.connect(self.refresh_documents)
        self.cb_all_dates.toggled.connect(self._toggle_doc_dates)
        self.cb_all_dates.toggled.connect(lambda _checked: self.refresh_documents())
        self.doc_table.clicked.connect(self.on_doc_selected)
        self.btn_export_csv.clicked.connect(lambda: self._export_with_busy("csv"))
        self.btn_export_xlsx.clicked.connect(lambda: self._export_with_busy("xlsx"))

        self.unrec_table.clicked.connect(self.on_unrec_selected)
        self.btn_u_save.clicked.connect(self.on_unrec_save)

    def _wire_timers(self):
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_dashboard)
        self.timer.start()

    def _pick_dir(self, line_edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Vyber adresář", line_edit.text() or str(Path.home()))
        if d:
            line_edit.setText(d)

    def _toggle_doc_dates(self, all_dates: bool) -> None:
        # UI helper: datumové filtry zbytečně matou, pokud je zvoleno „Vše“
        try:
            self.dt_from.setEnabled(not all_dates)
            self.dt_to.setEnabled(not all_dates)
        except Exception:
            pass

    def _render_pdf_preview_bytes(self, path: Path, dpi: int = 160) -> bytes | None:
        """
        Render první stránku PDF v samostatném procesu, aby případné chyby
        v pdfiu/pillow nesestřelily celé GUI. Vrací PNG byty nebo None.
        """
        ctx = mp.get_context("spawn")
        q: mp.Queue = ctx.Queue(maxsize=1)

        def _worker(pdf_path: str, dpi_val: int, out_q):
            try:
                from pathlib import Path
                from kajovospend.ocr.pdf_render import render_pdf_to_images

                imgs = render_pdf_to_images(Path(pdf_path), dpi=dpi_val, max_pages=1)
                if not imgs:
                    out_q.put(None)
                    return
                buf = BytesIO()
                imgs[0].save(buf, format="PNG")
                out_q.put(buf.getvalue())
            except Exception as exc:
                try:
                    out_q.put({"error": str(exc)})
                except Exception:
                    pass

        proc = ctx.Process(target=_worker, args=(str(path), dpi, q))
        proc.start()
        proc.join(10)
        if proc.is_alive():
            proc.terminate()
            proc.join(2)
            self.log.warning("Preview render timed out; killed worker for %s", path)
        try:
            res = q.get_nowait()
        except queue.Empty:
            res = None

        if isinstance(res, dict) and "error" in res:
            raise RuntimeError(res["error"])
        return res if isinstance(res, (bytes, bytearray)) else None

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

    def refresh_documents(self):
        q = self.ed_doc_search.text().strip()
        dfrom = self.dt_from.date().toPython()
        dto = self.dt_to.date().toPython()
        if getattr(self, "cb_all_dates", None) is not None and self.cb_all_dates.isChecked():
            dfrom = None
            dto = None
        else:
            if dfrom and dto and dfrom > dto:
                dfrom, dto = dto, dfrom
        with self.sf() as session:
            docs = db_api.list_documents(session, q=q, date_from=dfrom, date_to=dto)
            rows = []
            for d, f in docs:
                rows.append([d.id, d.issue_date.isoformat() if d.issue_date else "", d.supplier_ico or "", d.doc_number or "", d.total_with_vat or "", d.currency, "ANO" if d.requires_review else "", f.status])
        self.doc_model = TableModel(["ID", "Datum", "IČO", "Číslo", "Celkem", "Měna", "Kontrola", "Status"], rows)
        self.doc_table.setModel(self.doc_model)

    def on_doc_selected(self, index):
        try:
            doc_id = int(self.doc_model.rows[index.row()][0])
        except Exception:
            return
        with self.sf() as session:
            detail = db_api.get_document_detail(session, doc_id)
            f: DocumentFile = detail["file"]
            items: List[LineItem] = detail["items"]
        it_rows = [[i.line_no, i.name, i.quantity, i.vat_rate, i.line_total] for i in items]
        self.items_table.setModel(TableModel(["#", "Název", "Počet", "DPH %", "Cena"], it_rows))

        # preview in background (PDF render / image load can block)
        self.preview.setText("Načítám náhled…")
        self.preview.setPixmap(QPixmap())
        QApplication.processEvents()

        p = Path(f.current_path)

        def work():
            if not p.exists():
                raise FileNotFoundError(p)
            if p.suffix.lower() == ".pdf":
                return self._render_pdf_preview_bytes(p, dpi=160)
            from PIL import Image
            with Image.open(p) as img:
                # ensure underlying file handle is released (Windows)
                return pil_to_qimage(img.copy())

        def done(res):
            if res is None:
                self.preview.setText("Nelze načíst náhled")
                return
            qimg = res
            if isinstance(res, (bytes, bytearray)):
                qimg = QImage.fromData(res, "PNG")
            if qimg is None or (hasattr(qimg, "isNull") and qimg.isNull()):
                self.preview.setText("Náhled se nepodařilo vytvořit")
                return
            pix = QPixmap.fromImage(qimg)
            self.preview.setPixmap(pix.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

        self._run_with_busy("Náhled", "Generuji náhled dokladu…", work, done)

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
        q = self.ed_doc_search.text().strip()
        dfrom = self.dt_from.date().toPython()
        dto = self.dt_to.date().toPython()

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
