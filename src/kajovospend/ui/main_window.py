from __future__ import annotations

import datetime as dt
import os
from io import BytesIO
from pathlib import Path
import shutil
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageFilter, ImageOps

from PySide6.QtCore import Qt, QTimer, QAbstractTableModel, QModelIndex, QObject, Signal, Slot, QThread, QUrl, QPointF, QRectF
from PySide6.QtGui import QIcon, QPixmap, QImage, QDesktopServices, QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QTabWidget, QTableView,
    QLineEdit, QFormLayout, QSplitter, QTextEdit, QDoubleSpinBox, QSpinBox, QComboBox, QFileDialog,
    QMessageBox, QDateEdit, QDialog, QDialogButtonBox, QHeaderView, QAbstractItemView,
    QCheckBox, QProgressDialog, QApplication, QInputDialog, QScrollArea, QStyledItemDelegate,
)
from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem

from sqlalchemy import select, text

from kajovospend.utils.config import load_yaml, save_yaml, deep_set
import requests
import requests
from kajovospend.utils.paths import resolve_app_paths
from kajovospend.utils.logging_setup import setup_logging
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.db.migrate import init_db
from kajovospend.db.models import Supplier, Document, DocumentFile, LineItem, ImportJob
from kajovospend.db.queries import upsert_supplier, rebuild_fts_for_document
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico
from kajovospend.integrations.openai_fallback import (
    OpenAIConfig,
    extract_with_openai_fallback,
    list_models,
)
from kajovospend.service.processor import Processor, safe_move
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



class TrafficLightDelegate(QStyledItemDelegate):
    """Renders a simple traffic-light dot based on model value ('green'/'orange'/'red')."""

    def paint(self, painter: QPainter, option, index):
        v = index.data(Qt.DisplayRole)
        # default color
        col = QColor("#64748B")  # slate
        if isinstance(v, str):
            vv = v.lower()
            if vv == "green":
                col = QColor("#22C55E")
            elif vv in ("orange", "amber", "yellow"):
                col = QColor("#F59E0B")
            elif vv == "red":
                col = QColor("#EF4444")

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        r = option.rect
        d = min(r.width(), r.height(), 18)
        cx = r.x() + r.width() // 2
        cy = r.y() + r.height() // 2
        circle = QRectF(cx - d / 2, cy - d / 2, d, d)

        painter.setPen(QPen(QColor("#0B1220"), 1))
        painter.setBrush(col)
        painter.drawEllipse(circle)
        painter.restore()

    def displayText(self, value, locale):
        return ""


def _make_icon_pixmap(name: str, size: int = 44) -> QPixmap:
    """Creates a small, consistent pictogram without external assets."""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    stroke = QColor("#93C5FD")
    fill = QColor("#1F2937")
    accent = QColor("#2563EB")

    def pen(w: int = 2, c: QColor | None = None) -> QPen:
        q = QPen(c or stroke)
        q.setWidth(w)
        q.setCapStyle(Qt.RoundCap)
        q.setJoinStyle(Qt.RoundJoin)
        return q

    s = size
    if name == "inbox":
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawRoundedRect(6, 10, s - 12, s - 16, 8, 8)
        p.setBrush(Qt.NoBrush)
        p.drawLine(10, 16, s - 10, 16)
        p.setPen(pen(3, accent))
        p.drawLine(s // 2, 6, s // 2, 18)
        p.drawLine(s // 2, 18, s // 2 - 6, 12)
        p.drawLine(s // 2, 18, s // 2 + 6, 12)
    elif name == "quarantine":
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawRoundedRect(8, 10, s - 16, s - 16, 8, 8)
        p.setPen(pen(3, QColor("#F59E0B")))
        p.drawLine(s // 2, 16, s // 2, s - 16)
        p.drawPoint(s // 2, s - 12)
    elif name == "duplicate":
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawRoundedRect(10, 10, s - 18, s - 18, 8, 8)
        p.drawRoundedRect(6, 6, s - 18, s - 18, 8, 8)
    elif name == "status":
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawRoundedRect(8, 8, s - 16, s - 16, 10, 10)
        p.setPen(pen(3, QColor("#22C55E")))
        p.drawLine(14, s // 2, s - 14, s // 2)
    elif name == "clock":
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawEllipse(8, 8, s - 16, s - 16)
        p.setPen(pen(3, accent))
        p.drawLine(s // 2, s // 2, s // 2, 14)
        p.drawLine(s // 2, s // 2, s - 16, s // 2)
    elif name == "db":
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawRoundedRect(10, 10, s - 20, s - 20, 10, 10)
        p.setPen(pen(2, accent))
        p.drawLine(14, 18, s - 14, 18)
        p.drawLine(14, 26, s - 14, 26)
        p.drawLine(14, 34, s - 14, 34)
    else:
        p.setPen(pen(2))
        p.setBrush(fill)
        p.drawRoundedRect(8, 8, s - 16, s - 16, 10, 10)

    p.end()
    return QPixmap.fromImage(img)


class DashboardTile(QWidget):
    def __init__(self, title: str, *, icon: str, parent=None):
        super().__init__(parent)
        self.setObjectName("DashTile")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        self.icon = QLabel()
        self.icon.setFixedSize(46, 46)
        self.icon.setPixmap(_make_icon_pixmap(icon, 44))
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setObjectName("DashIcon")
        lay.addWidget(self.icon)

        mid = QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(2)

        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("DashTitle")
        self.lbl_value = QLabel("-")
        self.lbl_value.setObjectName("DashValue")
        self.lbl_value.setTextInteractionFlags(Qt.TextSelectableByMouse)

        mid.addWidget(self.lbl_title)
        mid.addWidget(self.lbl_value)
        lay.addLayout(mid, 1)

    def set_value(self, text: str) -> None:
        self.lbl_value.setText(text)


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
    Editovatelný model položek (LineItem) – používá se pro Účty i NEROZPOZNANÉ.
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


class _ImportWorker(QObject):
    progress = Signal(str)
    done = Signal(dict)
    error = Signal(str)

    def __init__(self, cfg: Dict[str, Any], sf, processor: Processor):
        super().__init__()
        self.cfg = cfg
        self.sf = sf
        self.processor = processor

    @Slot()
    def run(self):
        try:
            input_dir = Path(self.cfg["paths"]["input_dir"])
            if not input_dir.exists():
                self.done.emit({"imported": 0, "message": "Adresář INPUT neexistuje."})
                return

            exts = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
            out_base = Path(self.cfg["paths"]["output_dir"])
            quarantine_dir = out_base / self.cfg["paths"].get("quarantine_dir_name", "KARANTENA")

            files: list[Path] = []
            for p in input_dir.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() in exts:
                    files.append(p)
                else:
                    try:
                        moved = safe_move(p, quarantine_dir, p.name)
                        self.processor.log.warning("Nepodporovaný soubor %s přesunut do karantény jako %s", p, moved)
                    except Exception as exc:
                        self.processor.log.exception("Nelze přesunout nepodporovaný soubor %s: %s", p, exc)

            files.sort(key=lambda p: (p.stat().st_mtime, p.name, str(p)))

            if not files:
                self.done.emit({"imported": 0, "message": "V INPUT nejsou žádné soubory."})
                return

            imported = 0
            total = len(files)

            for i, p in enumerate(files, start=1):
                self.progress.emit(f"Zpracovávám {i}/{total}: {p.name}")
                try:
                    with self.sf() as session:
                        job = ImportJob(path=str(p), status="RUNNING", started_at=dt.datetime.utcnow())
                        session.add(job)
                        session.commit()

                        res = self.processor.process_path(session, p, status_cb=self.progress.emit)
                        job.sha256 = res.get("sha256")
                        job.status = str(res.get("status") or "DONE")
                        job.finished_at = dt.datetime.utcnow()
                        session.add(job)
                        session.commit()
                        imported += 1
                except Exception as e:
                    try:
                        with self.sf() as session:
                            job = ImportJob(
                                path=str(p),
                                status="ERROR",
                                started_at=dt.datetime.utcnow(),
                                finished_at=dt.datetime.utcnow(),
                                error=str(e),
                            )
                            session.add(job)
                            session.commit()
                    except Exception:
                        pass
                    self.progress.emit(f"Chyba: {p.name}: {e}")

            # vyčistit prázdné podadresáře
            dirs = sorted([d for d in input_dir.rglob("*") if d.is_dir()], key=lambda d: len(d.parts), reverse=True)
            for d in dirs:
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except Exception:
                    pass

            self.done.emit({"imported": imported, "total": total, "message": "Hotovo."})
        except Exception as e:
            self.error.emit(str(e))


class _SilentRunner:
    """
    Lightweight background runner (no modal progress).
    Keeps thread/worker objects alive via MainWindow lists to avoid Qt GC issues.
    """

    @staticmethod
    def run(window: "MainWindow", fn, on_done, on_error=None, *, timeout_ms: int | None = None):
        th = QThread(window)
        wk = _Worker(fn)
        wk.moveToThread(th)
        th.started.connect(wk.run)

        completed = False
        timer: QTimer | None = None

        window._workers.append(wk)
        window._threads.append(th)

        def _dispatch_ui(cb) -> None:
            if QThread.currentThread() != window.thread():
                QTimer.singleShot(0, window, cb)
                return
            cb()

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
                th.quit()
                th.wait(2000)
            except Exception:
                pass
            try:
                window._threads.remove(th)
            except Exception:
                pass
            try:
                window._workers.remove(wk)
            except Exception:
                pass
            try:
                if timer is not None:
                    window._timers.remove(timer)
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
                    window.log.exception("SilentRunner on_done failed")
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
                        window.log.exception("SilentRunner on_error failed")
                # default: do not spam modal errors in background refresh
                window.log.warning("SilentRunner error: %s", msg)
            _dispatch_ui(_impl)

        wk.done.connect(_ok)
        wk.error.connect(_err)

        th.start()

        if timeout_ms is not None:
            timer = QTimer(window)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: _err("Operace překročila časový limit."))
            timer.start(int(timeout_ms))
            window._timers.append(timer)


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

        # background RUN stats refresh
        self._dash_refresh_inflight = False
        self._dash_last_counts: Dict[str, Any] | None = None
        self._import_running = False
        self._import_status = "Připraveno."
        self._last_run_error: str | None = None
        self._last_run_success: str | None = None

        # selection model guards to prevent duplicate signal connections after model resets
        self._docs_sel_model = None
        self._docs_sel_connected = False
        self._unrec_sel_model = None
        self._unrec_sel_connected = False

        # paging for per-item search tab
        self._items_page_size = int(self.cfg.get("performance", {}).get("items_page_size", 1000) or 1000)
        self._items_offset = 0
        self._items_total = 0
        self._items_rows: List[Dict[str, Any]] = []
        self._items_current_path: str | None = None

        # current selections (Účty / NEROZPOZNANÉ)
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
        self.processor = Processor(self.cfg, self.paths, self.log)

        self.setWindowTitle("KájovoSpend")
        ico = self.assets_dir / "app.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(QSS)
        self.setStyleSheet(QSS)

        # start maximized on primary monitor
        self.showMaximized()

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
        cfg.setdefault("ocr", {})
        cfg.setdefault("openai", {})
        openai_cfg = cfg.get("openai") or {}
        if isinstance(openai_cfg, dict):
            openai_cfg.setdefault("enabled", False)
            openai_cfg.setdefault("auto_enable", True)
            openai_cfg.setdefault("primary_enabled", True)
            openai_cfg.setdefault("fallback_enabled", True)
            openai_cfg.setdefault("model", "auto")
            openai_cfg.setdefault("fallback_model", "")
            openai_cfg.setdefault("use_json_schema", True)
            openai_cfg.setdefault("temperature", 0.0)
            openai_cfg.setdefault("max_output_tokens", 2000)
            openai_cfg.setdefault("timeout_sec", 60)
            openai_cfg.setdefault("image_dpi", 300)
            openai_cfg.setdefault("image_max_pages", 3)
            openai_cfg.setdefault("image_enhance", True)
            openai_cfg.setdefault("image_variants", 2)
            openai_cfg.setdefault("allow_synthetic_items", False)
            cfg["openai"] = openai_cfg
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

    def _stat_number_font(self, size: int, *, bold: bool = False) -> QFont:
        f = QFont()
        f.setPointSize(int(size))
        f.setBold(bool(bold))
        return f

    def _icon_pixmap(self, kind: str, size: int) -> QPixmap:
        """Create simple monochrome pictograms (no text)."""
        px = QPixmap(int(size), int(size))
        px.fill(Qt.transparent)

        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = QColor("#E5E7EB")
        pen = QPen(col)
        pen.setWidth(max(2, int(size) // 14))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        s = float(size)
        m = s * 0.12
        x0, y0 = m, m
        w = s - 2 * m
        h = s - 2 * m

        def rect(x, y, ww, hh, r=0.0):
            if r and r > 0:
                p.drawRoundedRect(int(x), int(y), int(ww), int(hh), float(r), float(r))
            else:
                p.drawRect(int(x), int(y), int(ww), int(hh))

        k = (kind or "").lower().strip()

        if k in ("receipt", "doc"):
            rect(x0, y0, w, h, r=s * 0.08)
            p.drawLine(int(x0 + w * 0.65), int(y0), int(x0 + w), int(y0 + h * 0.35))
            p.drawLine(int(x0 + w * 0.65), int(y0), int(x0 + w * 0.65), int(y0 + h * 0.35))
            p.drawLine(int(x0 + w * 0.65), int(y0 + h * 0.35), int(x0 + w), int(y0 + h * 0.35))
            for i in range(3):
                yy = y0 + h * (0.45 + i * 0.16)
                p.drawLine(int(x0 + w * 0.15), int(yy), int(x0 + w * 0.85), int(yy))

        elif k == "duplicate":
            rect(x0 + w * 0.14, y0 + h * 0.05, w * 0.78, h * 0.78, r=s * 0.08)
            rect(x0 + w * 0.05, y0 + h * 0.14, w * 0.78, h * 0.78, r=s * 0.08)

        elif k == "quarantine":
            p.drawPolygon([
                QPointF(x0 + w * 0.5, y0),
                QPointF(x0 + w, y0 + h),
                QPointF(x0, y0 + h),
            ])
            p.drawLine(int(x0 + w * 0.5), int(y0 + h * 0.3), int(x0 + w * 0.5), int(y0 + h * 0.68))
            p.drawPoint(int(x0 + w * 0.5), int(y0 + h * 0.82))

        elif k == "items":
            rect(x0, y0, w, h, r=s * 0.08)
            for i in range(4):
                yy = y0 + h * (0.2 + i * 0.18)
                p.drawLine(int(x0 + w * 0.18), int(yy), int(x0 + w * 0.82), int(yy))
                p.drawPoint(int(x0 + w * 0.12), int(yy))

        elif k == "suppliers":
            base_y = y0 + h * 0.75
            p.drawLine(int(x0), int(base_y), int(x0 + w), int(base_y))
            rect(x0 + w * 0.1, y0 + h * 0.35, w * 0.75, h * 0.4, r=s * 0.05)
            p.drawLine(int(x0 + w * 0.1), int(y0 + h * 0.35), int(x0 + w * 0.3), int(y0 + h * 0.2))
            p.drawLine(int(x0 + w * 0.3), int(y0 + h * 0.2), int(x0 + w * 0.5), int(y0 + h * 0.35))
            p.drawLine(int(x0 + w * 0.5), int(y0 + h * 0.35), int(x0 + w * 0.7), int(y0 + h * 0.2))
            p.drawLine(int(x0 + w * 0.7), int(y0 + h * 0.2), int(x0 + w * 0.85), int(y0 + h * 0.35))
            rect(x0 + w * 0.78, y0 + h * 0.12, w * 0.12, h * 0.23, r=s * 0.03)

        elif k == "offline":
            rect(x0 + w * 0.15, y0 + h * 0.15, w * 0.7, h * 0.7, r=s * 0.1)
            rect(x0 + w * 0.32, y0 + h * 0.32, w * 0.36, h * 0.36, r=s * 0.06)
            for i in range(4):
                xx = x0 + w * (0.22 + i * 0.19)
                p.drawLine(int(xx), int(y0), int(xx), int(y0 + h * 0.15))
                p.drawLine(int(xx), int(y0 + h * 0.85), int(xx), int(y0 + h))

        elif k == "api":
            p.drawEllipse(int(x0 + w * 0.18), int(y0 + h * 0.38), int(w * 0.38), int(h * 0.38))
            p.drawEllipse(int(x0 + w * 0.38), int(y0 + h * 0.25), int(w * 0.38), int(h * 0.45))
            p.drawEllipse(int(x0 + w * 0.52), int(y0 + h * 0.4), int(w * 0.34), int(h * 0.34))
            p.drawLine(int(x0 + w * 0.18), int(y0 + h * 0.62), int(x0 + w * 0.86), int(y0 + h * 0.62))

        elif k == "manual":
            p.drawLine(int(x0 + w * 0.2), int(y0 + h * 0.8), int(x0 + w * 0.8), int(y0 + h * 0.2))
            p.drawLine(int(x0 + w * 0.72), int(y0 + h * 0.12), int(x0 + w * 0.88), int(y0 + h * 0.28))
            p.drawLine(int(x0 + w * 0.12), int(y0 + h * 0.72), int(x0 + w * 0.28), int(y0 + h * 0.88))

        elif k in ("sum_wo_vat", "sum_w_vat", "avg_receipt", "avg_item", "avg_items", "minmax", "max_item"):
            if k == "minmax":
                p.drawLine(int(x0 + w * 0.5), int(y0 + h * 0.1), int(x0 + w * 0.5), int(y0 + h * 0.9))
                p.drawLine(int(x0 + w * 0.35), int(y0 + h * 0.25), int(x0 + w * 0.5), int(y0 + h * 0.1))
                p.drawLine(int(x0 + w * 0.65), int(y0 + h * 0.25), int(x0 + w * 0.5), int(y0 + h * 0.1))
                p.drawLine(int(x0 + w * 0.35), int(y0 + h * 0.75), int(x0 + w * 0.5), int(y0 + h * 0.9))
                p.drawLine(int(x0 + w * 0.65), int(y0 + h * 0.75), int(x0 + w * 0.5), int(y0 + h * 0.9))
            elif k == "max_item":
                cx, cy = x0 + w * 0.5, y0 + h * 0.5
                pts = [
                    (cx, y0),
                    (x0 + w * 0.62, y0 + h * 0.38),
                    (x0 + w, y0 + h * 0.4),
                    (x0 + w * 0.7, y0 + h * 0.62),
                    (x0 + w * 0.8, y0 + h),
                    (cx, y0 + h * 0.78),
                    (x0 + w * 0.2, y0 + h),
                    (x0 + w * 0.3, y0 + h * 0.62),
                    (x0, y0 + h * 0.4),
                    (x0 + w * 0.38, y0 + h * 0.38),
                ]
                p.drawPolygon([QPointF(a, b) for a, b in pts])
            else:
                p.drawEllipse(int(x0 + w * 0.15), int(y0 + h * 0.2), int(w * 0.7), int(h * 0.6))
                p.drawLine(int(x0 + w * 0.2), int(y0 + h * 0.5), int(x0 + w * 0.8), int(y0 + h * 0.5))
                if k == "sum_w_vat":
                    p.drawLine(int(x0 + w * 0.5), int(y0 + h * 0.28), int(x0 + w * 0.5), int(y0 + h * 0.72))
                if k == "avg_item":
                    p.drawEllipse(int(x0 + w * 0.42), int(y0 + h * 0.38), int(w * 0.16), int(h * 0.16))
                if k == "avg_receipt":
                    p.drawRect(int(x0 + w * 0.32), int(y0 + h * 0.28), int(w * 0.36), int(h * 0.44))

        else:
            p.drawEllipse(int(x0), int(y0), int(w), int(h))

        p.end()
        return px

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

        self.btn_exit = QPushButton("EXIT")
        self.btn_exit.setObjectName("ExitButton")

        hl.addStretch(1)
        hl.addWidget(self.btn_exit)

        v.addWidget(header)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

                
        # RUN
        self.tab_run = QWidget()
        rl = QVBoxLayout(self.tab_run)
        rl.setContentsMargins(12, 12, 12, 12)
        rl.setSpacing(12)

        # Top row: Import button + high-level status
        top = QWidget()
        top_l = QHBoxLayout(top)
        top_l.setContentsMargins(0, 0, 0, 0)
        top_l.setSpacing(12)

        self.btn_import = QPushButton("IMPORT")
        self.btn_import.setToolTip("Zařadí soubory z INPUT do fronty zpracování")

        self.lbl_run_status = QLabel(self._import_status)
        self.lbl_run_status.setWordWrap(True)
        self.lbl_run_status.setObjectName("DashHeadline")

        top_l.addWidget(self.btn_import)
        top_l.addWidget(self.lbl_run_status, 1)

        rl.addWidget(top)

        # Dashboard grid (fills most of the RUN tab)
        dash_wrap = QWidget()
        dash_grid = QGridLayout(dash_wrap)
        dash_grid.setContentsMargins(0, 0, 0, 0)
        dash_grid.setHorizontalSpacing(12)
        dash_grid.setVerticalSpacing(12)

        self._dash_tiles: dict[str, DashboardTile] = {}
        self._stat_labels = {}  # reused by _apply_dashboard (DB stats)

        def add_tile(key: str, title: str, icon: str, r: int, c: int, rs: int = 1, cs: int = 1):
            t = DashboardTile(title, icon=icon)
            dash_grid.addWidget(t, r, c, rs, cs)
            self._dash_tiles[key] = t
            return t

        add_tile("in_waiting", "IN čeká na zpracování", "inbox", 0, 0)
        add_tile("quarantine_total", "Karanténa celkem", "quarantine", 0, 1)
        add_tile("quarantine_dup", "Z toho duplicita", "duplicate", 0, 2)
        add_tile("quarantine_fail", "Z toho nevytěženo", "quarantine", 1, 0)

        add_tile("import_power", "Import (zapnuto/vypnuto)", "status", 1, 1)
        add_tile("import_activity", "Co se děje teď", "status", 1, 2)

        add_tile("import_next", "Co následuje", "status", 2, 0)
        add_tile("import_eta", "Odhad času do konce", "clock", 2, 1)
        add_tile("queue", "Fronta / běží / zbývá", "status", 2, 2)

        t_docs = add_tile("receipts", "Kompletně vytěžené účty", "db", 3, 0)
        t_sups = add_tile("suppliers", "Dodavatelé v DB", "db", 3, 1)
        t_items = add_tile("items", "Položky v DB", "db", 3, 2)
        t_sum = add_tile("sum_items_w_vat", "Hodnota položek s DPH", "db", 4, 0, 1, 2)
        t_sum_wo = add_tile("sum_items_wo_vat", "Hodnota položek bez DPH", "db", 4, 2)

        # Map tiles to old stat updater keys
        self._stat_labels["receipts"] = t_docs.lbl_value
        self._stat_labels["suppliers"] = t_sups.lbl_value
        self._stat_labels["items"] = t_items.lbl_value
        self._stat_labels["sum_items_w_vat"] = t_sum.lbl_value
        self._stat_labels["sum_items_wo_vat"] = t_sum_wo.lbl_value

        rl.addWidget(dash_wrap, 2)

        # Optional run log (keeps previous behaviour)
        self.run_log = QTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setPlaceholderText("Průběh zpracování…")
        rl.addWidget(self.run_log, 1)

        self.tabs.addTab(self.tab_run, "RUN")

        # Provozní panel
        self.tab_ops = QWidget()
        ol = QVBoxLayout(self.tab_ops)
        self.ops_table = QTableView()
        self.ops_table.setAlternatingRowColors(True)
        self.ops_table.setShowGrid(True)
        self.ops_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ops_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.ops_table.setItemDelegateForColumn(0, TrafficLightDelegate(self.ops_table))
        self.ops_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.ops_table.setColumnWidth(0, 46)
        ol.addWidget(self.ops_table)
        self.tabs.addTab(self.tab_ops, "PROVOZNÍ PANEL")

        # NEZPRACOVANÉ (sloučené podezřelé + nerozpoznané)
        self.tab_unprocessed = QWidget()
        ul = QVBoxLayout(self.tab_unprocessed)
        self.unproc_table = QTableView()
        self.unproc_table.setAlternatingRowColors(True)
        self.unproc_table.setShowGrid(True)
        self.unproc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.unproc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ul.addWidget(self.unproc_table)

        row_actions = QWidget()
        rah = QHBoxLayout(row_actions)
        rah.setContentsMargins(0, 0, 0, 0)
        self.btn_unproc_openai = QPushButton("Odeslat na OpenAI")
        self.btn_unproc_manual = QPushButton("Ručně vytěžit")
        rah.addWidget(self.btn_unproc_openai)
        rah.addWidget(self.btn_unproc_manual)
        rah.addStretch(1)
        ul.addWidget(row_actions)

        self.tabs.addTab(self.tab_unprocessed, "NEZPRACOVANÉ")

        # $

        # $
        self.tab_money = QWidget()
        ml = QVBoxLayout(self.tab_money)
        self.money_summary = QTextEdit()
        self.money_summary.setReadOnly(True)
        ml.addWidget(self.money_summary)
        self.tabs.addTab(self.tab_money, "$")

        # Nastavení
        # Nastavení
        self.tab_settings = QWidget()
        stl = QVBoxLayout(self.tab_settings)
        form = QFormLayout()

        self.ed_input_dir = QLineEdit(self.cfg["paths"].get("input_dir", ""))
        self.btn_pick_input = QPushButton("Vybrat")
        self.ed_output_dir = QLineEdit(self.cfg["paths"].get("output_dir", ""))
        self.btn_pick_output = QPushButton("Vybrat")

        row_in = QWidget(); r3 = QHBoxLayout(row_in); r3.setContentsMargins(0,0,0,0)
        r3.addWidget(self.ed_input_dir, 1); r3.addWidget(self.btn_pick_input)
        form.addRow("Input adresář", row_in)

        row_out = QWidget(); r4 = QHBoxLayout(row_out); r4.setContentsMargins(0,0,0,0)
        r4.addWidget(self.ed_output_dir, 1); r4.addWidget(self.btn_pick_output)
        form.addRow("Output adresář", row_out)

        # OpenAI nastaveni
        openai_cfg = self.cfg.get("openai", {}) if isinstance(self.cfg, dict) else {}
        if not isinstance(openai_cfg, dict):
            openai_cfg = {}
        self.ed_api_key = QLineEdit(openai_cfg.get("api_key", ""))
        self.ed_api_key.setEchoMode(QLineEdit.Password)
        self.btn_api_show = QPushButton("Zobrazit")
        self.btn_api_clear = QPushButton("Smazat")
        self.btn_api_models = QPushButton("Načíst modely")
        api_row = QWidget(); ar = QHBoxLayout(api_row); ar.setContentsMargins(0,0,0,0)
        ar.addWidget(self.ed_api_key, 1); ar.addWidget(self.btn_api_show); ar.addWidget(self.btn_api_clear); ar.addWidget(self.btn_api_models)
        form.addRow("OpenAI API key", api_row)

        self.cb_openai_enabled = QCheckBox("Zapnout OpenAI")
        self.cb_openai_enabled.setChecked(bool(openai_cfg.get("enabled", False)))
        self.cb_openai_auto = QCheckBox("Auto-zapnout pri vyplnenem API key")
        self.cb_openai_auto.setChecked(bool(openai_cfg.get("auto_enable", True)))
        self.cb_openai_primary = QCheckBox("Primarni online extrakce (OpenAI)")
        self.cb_openai_primary.setChecked(bool(openai_cfg.get("primary_enabled", True)))
        self.cb_openai_fallback = QCheckBox("Povolit OpenAI fallback")
        self.cb_openai_fallback.setChecked(bool(openai_cfg.get("fallback_enabled", True)))

        self.ed_primary_model = QLineEdit(openai_cfg.get("model", "auto"))
        self.ed_primary_model.setPlaceholderText("auto")
        self.ed_fallback_model = QLineEdit(openai_cfg.get("fallback_model", ""))
        self.ed_fallback_model.setPlaceholderText("auto / prazdne = stejny model")

        self.cb_use_json_schema = QCheckBox("Vynutit JSON schema (strict)")
        self.cb_use_json_schema.setChecked(bool(openai_cfg.get("use_json_schema", True)))

        self.sp_temperature = QDoubleSpinBox()
        self.sp_temperature.setDecimals(2)
        self.sp_temperature.setSingleStep(0.05)
        self.sp_temperature.setRange(0.0, 2.0)
        self.sp_temperature.setValue(float(openai_cfg.get("temperature", 0.0) or 0.0))

        self.sp_max_tokens = QSpinBox()
        self.sp_max_tokens.setRange(200, 16000)
        self.sp_max_tokens.setSingleStep(100)
        self.sp_max_tokens.setValue(int(openai_cfg.get("max_output_tokens", 2000) or 2000))

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(10, 300)
        self.sp_timeout.setSingleStep(5)
        self.sp_timeout.setValue(int(openai_cfg.get("timeout_sec", 60) or 60))

        self.sp_image_dpi = QSpinBox()
        self.sp_image_dpi.setRange(100, 600)
        self.sp_image_dpi.setSingleStep(50)
        self.sp_image_dpi.setValue(int(openai_cfg.get("image_dpi", 300) or 300))

        self.sp_image_max_pages = QSpinBox()
        self.sp_image_max_pages.setRange(1, 5)
        self.sp_image_max_pages.setValue(int(openai_cfg.get("image_max_pages", 3) or 3))

        self.cb_image_enhance = QCheckBox("Zlepsit obraz pred odeslanim")
        self.cb_image_enhance.setChecked(bool(openai_cfg.get("image_enhance", True)))

        self.sp_image_variants = QSpinBox()
        self.sp_image_variants.setRange(1, 3)
        self.sp_image_variants.setValue(int(openai_cfg.get("image_variants", 2) or 2))

        self.cb_allow_synth = QCheckBox("Povolit synteticke polozky jako posledni zachrana")
        self.cb_allow_synth.setChecked(bool(openai_cfg.get("allow_synthetic_items", False)))

        form.addRow("Primarni model", self.ed_primary_model)
        form.addRow("Fallback model", self.ed_fallback_model)
        form.addRow("Teplota", self.sp_temperature)
        form.addRow("Max output tokens", self.sp_max_tokens)
        form.addRow("Timeout (s)", self.sp_timeout)
        form.addRow("Image DPI", self.sp_image_dpi)
        form.addRow("Max stranky", self.sp_image_max_pages)
        form.addRow("Varianta obrazu", self.sp_image_variants)
        form.addRow(self.cb_use_json_schema)
        form.addRow(self.cb_openai_enabled)
        form.addRow(self.cb_openai_auto)
        form.addRow(self.cb_openai_primary)
        form.addRow(self.cb_openai_fallback)
        form.addRow(self.cb_image_enhance)
        form.addRow(self.cb_allow_synth)

        # Tooltips (obsahle napovedy pro nastaveni)
        self.ed_input_dir.setToolTip(
            "Slozka pro nove dokumenty (skeny/uctenky).\n"
            "Aplikace ji sleduje a nove soubory postupne zpracuje.\n"
            "Pouzij stabilni cestu s pravem zapisu."
        )
        self.btn_pick_input.setToolTip("Vyber slozku pro vstupni dokumenty.")
        self.ed_output_dir.setToolTip(
            "Slozka pro vystupy z extrakce (napr. JSON, kopie, logy).\n"
            "Program do ni zapisuje, musi byt zapisovatelna.\n"
            "Doporuceno oddelit od vstupni slozky."
        )
        self.btn_pick_output.setToolTip("Vyber slozku pro vystupy zpracovani.")
        self.ed_api_key.setToolTip(
            "OpenAI API key pro online extrakci.\n"
            "Ulozi se do lokalniho configu. Bez klice se online rezimy nespousti.\n"
            "Vkladej cely klic, bez mezer."
        )
        self.btn_api_show.setToolTip(
            "Prepina zobrazeni/skryti API key v poli.\n"
            "Pozor na sdileni obrazovky."
        )
        self.btn_api_clear.setToolTip(
            "Vymaze API key z pole (a po ulozeni i z configu)."
        )
        self.btn_api_models.setToolTip(
            "Nacte seznam modelu z /v1/models pres zadany API key.\n"
            "Vybrany model lze ulozit jako primarni nebo fallback."
        )
        self.ed_primary_model.setToolTip(
            "Model pro primarni online extrakci.\n"
            "Hodnota 'auto' vybere nejvhodnejsi dostupny model.\n"
            "Muzes zadat i presny identifikator modelu."
        )
        self.ed_fallback_model.setToolTip(
            "Model pro fallback pri nekompletni extrakci.\n"
            "Prazdne = pouzije se primarni model.\n"
            "Vhodne je nastavit robustnejsi (ale drazsi) model."
        )
        self.cb_openai_enabled.setToolTip(
            "Manualni prepinac online extrakce.\n"
            "Kdyz je zapnute auto-zapnuti, rozhoduje API key.\n"
            "Kdyz auto vypnes, timto prepinacem OpenAI povolis/zakazes."
        )
        self.cb_openai_auto.setToolTip(
            "Automaticky zapne OpenAI, pokud je vyplnen API key.\n"
            "Vhodne pro bezobsluzny rezim bez rucniho prepinani."
        )
        self.cb_openai_primary.setToolTip(
            "Primarni online extrakce pred heuristikami.\n"
            "Zvyssuje presnost u tezko citelnych skenu, ale prodluzuje cas."
        )
        self.cb_openai_fallback.setToolTip(
            "Druhe kolo OpenAI, kdyz data stale nejsou kompletni.\n"
            "Typicky kdyz chybi polozky nebo soucty."
        )
        self.cb_use_json_schema.setToolTip(
            "Vynuti striktni JSON schema pro vystup.\n"
            "Zlepsuje spolehlivost struktury, ale muze byt prisnejsi."
        )
        self.sp_temperature.setToolTip(
            "Teplota generovani (0.0-2.0).\n"
            "Nizsi = deterministicke, vyssi = vice variace.\n"
            "Prilis vysoka teplota muze zvysit riziko halucinaci."
        )
        self.sp_max_tokens.setToolTip(
            "Limit velikosti odpovedi z OpenAI.\n"
            "Prilis nizky limit muze orezat JSON.\n"
            "Vyssi limit zvysuje cas i cenu."
        )
        self.sp_timeout.setToolTip(
            "Maximalni doba cekani na OpenAI odpoved (sekundy).\n"
            "Po vyprseni timeoutu se pouzije fallback/karantena."
        )
        self.sp_image_dpi.setToolTip(
            "DPI pro rasterizaci PDF/obrazku pred odeslanim.\n"
            "Vyssi DPI = lepsi cteni drobneho textu, ale vetsi data."
        )
        self.sp_image_max_pages.setToolTip(
            "Maximalni pocet stranek odeslanych na OpenAI.\n"
            "Zbytek stran se ignoruje (zrychleni a nizsi cena)."
        )
        self.cb_image_enhance.setToolTip(
            "Aplikovat autokontrast a doostreni pred odeslanim.\n"
            "Pomaha u bledych nebo zmuchlanych skenu."
        )
        self.sp_image_variants.setToolTip(
            "Kolik variant obrazu poslat (1=jen original).\n"
            "Vyssi hodnota = vice dat a vyssi sance na uspech."
        )
        self.btn_save_settings = QPushButton("Uložit nastavení")

        self.btn_backup_program = QPushButton("ZÁLOHOVAT PROGRAM")
        self.btn_backup_program.setToolTip(
            "Vytvoří kompletní zálohu (IN, OUT, databáze, config) do jednoho souboru."
        )

        self.btn_restore_program = QPushButton("OBNOVIT PROGRAM")
        self.btn_restore_program.setToolTip(
            "Načte dříve vytvořenou zálohu a přepíše aktuální data (vyžaduje heslo)."
        )

        self.btn_reset_program = QPushButton("RESET PROGRAMU")
        self.btn_reset_program.setToolTip(
            "Smaže databázi i obsah adresářů IN/OUT a znovu ji inicializuje.\n"
            "Vyžaduje heslo a potvrzení; akce je nevratná."
        )
        self.btn_reset_program.setStyleSheet(
            "QPushButton {background-color:#b91c1c; color:white; font-weight:bold;} "
            "QPushButton:hover {background-color:#dc2626;}"
        )

        self.cb_allow_synth.setToolTip(
            "Povoli vytvoreni synteticke polozky z totalu jako posledni zachranu.\n"
            "Pouzivej jen pokud je to akceptovatelne z pohledu kvality."
        )
        self.btn_save_settings.setToolTip(
            "Ulozi nastaveni do configu.\n"
            "Zmeny se projevi pro nove zpracovani."
        )

        stl.addLayout(form)
        stl.addWidget(self.btn_save_settings)
        stl.addWidget(self.btn_backup_program)
        stl.addWidget(self.btn_restore_program)
        stl.addWidget(self.btn_reset_program)
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
        self.tabs.addTab(self.tab_docs, "Účty")

        self.setCentralWidget(root)

        # Actions
        self.btn_exit.clicked.connect(self.close)
        self.btn_import.clicked.connect(self.on_import_clicked)

        self.btn_pick_input.clicked.connect(lambda: self._pick_dir(self.ed_input_dir))
        self.btn_pick_output.clicked.connect(lambda: self._pick_dir(self.ed_output_dir))
        self.btn_save_settings.clicked.connect(self.on_save_settings)
        self.btn_backup_program.clicked.connect(self._on_backup_program)
        self.btn_restore_program.clicked.connect(self._on_restore_program)
        self.btn_reset_program.clicked.connect(self._on_reset_program)
        self.btn_api_show.clicked.connect(self._on_api_show_toggle)
        self.btn_api_clear.clicked.connect(self._on_api_clear)
        self.btn_api_models.clicked.connect(self._on_api_models)

        self.btn_sup_refresh.clicked.connect(self.refresh_suppliers)
        self._sup_filter_timer.timeout.connect(self.refresh_suppliers)
        self.sup_filter.textChanged.connect(self._on_sup_filter_changed)
        self.btn_sup_add.clicked.connect(self.on_add_supplier)
        self.btn_sup_merge.clicked.connect(self.on_merge_suppliers)
        self.btn_sup_edit.clicked.connect(self.on_edit_supplier)
        self.btn_sup_save.clicked.connect(self.on_save_supplier)
        self.btn_sup_ares_detail.clicked.connect(self.on_supplier_ares)
        self.sup_table.clicked.connect(self.on_supplier_selected)

        self.btn_unproc_openai.clicked.connect(self.on_unproc_openai)
        self.btn_unproc_manual.clicked.connect(self.on_unproc_manual)

        # POLOŽKY (per-item search)
        self.btn_items_search.clicked.connect(self._items_new_search_v2)
        self.items_filter.returnPressed.connect(self._items_new_search_v2)
        self.btn_items_more.clicked.connect(self._items_load_more_v2)
        self.btn_items_open.clicked.connect(self._items_open_selected_v2)
        self.items_table.doubleClicked.connect(self._items_open_from_doubleclick_v2)

        # Účty – nová logika (plně editovatelný detail položek)
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

    def _docs_selection_changed_v2(self, *_args) -> None:
        try:
            sm = self.docs_table.selectionModel()
            if not sm:
                return
            idx = sm.currentIndex()
            if idx and idx.isValid():
                self._on_doc_selected_v2(idx)
        except Exception:
            pass

    def _wire_timers(self):
        self.timer = QTimer(self)
        # Periodic lightweight refresh for RUN tab.
        interval = int(self.cfg.get("performance", {}).get("ui_refresh_ms") or 1000)
        # keep it reasonable even if config is too aggressive
        interval = max(2000, interval)
        self.timer.setInterval(interval)
        self.timer.timeout.connect(self._refresh_from_queue)
        self.timer.start()

    def _refresh_from_queue(self):
        try:
            # never block UI thread here
            self._refresh_dashboard_async()
        except Exception:
            pass
        for fn in (self.refresh_run_state, self.refresh_ops, self.refresh_unprocessed):
            try:
                fn()
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
    # V2: Účty + NEROZPOZNANÉ
    # ---------------------------

    def refresh_all_v2(self) -> None:
        # zachovej dashboard a dodavatele (pokud existují původní metody), ale listy účtů řídí V2
        try:
            self._refresh_dashboard_async()
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
        try:
            self.refresh_unprocessed()
        except Exception:
            pass
        try:
            self.refresh_run_state()
        except Exception:
            pass

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

    # ---- Účty (listing + detail) ----

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
                # odpoj předchozí model jen pokud byl skutečně připojen
                if self._docs_sel_model is not sm:
                    if self._docs_sel_connected and self._docs_sel_model is not None:
                        try:
                            self._docs_sel_model.selectionChanged.disconnect(self._docs_selection_changed_v2)
                        except Exception:
                            pass
                        self._docs_sel_connected = False
                    self._docs_sel_model = sm
                if not self._docs_sel_connected:
                    sm.selectionChanged.connect(self._docs_selection_changed_v2)
                    self._docs_sel_connected = True
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
        """Zachováno kvůli zpětné kompatibilitě, aktuálně se nepoužívá."""
        return

    def _on_unrec_selected_v2(self, index: QModelIndex) -> None:  # pragma: no cover - legacy stub
        return

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

    def on_save_settings(self):
        deep_set(self.cfg, ["paths", "input_dir"], self.ed_input_dir.text().strip())
        deep_set(self.cfg, ["paths", "output_dir"], self.ed_output_dir.text().strip())
        deep_set(self.cfg, ["openai", "api_key"], "")  # API klíč se nikdy neukládá do YAML; použijte ENV KAJOVOSPEND_OPENAI_API_KEY
        deep_set(self.cfg, ["openai", "enabled"], self.cb_openai_enabled.isChecked())
        deep_set(self.cfg, ["openai", "auto_enable"], self.cb_openai_auto.isChecked())
        deep_set(self.cfg, ["openai", "primary_enabled"], self.cb_openai_primary.isChecked())
        deep_set(self.cfg, ["openai", "fallback_enabled"], self.cb_openai_fallback.isChecked())
        deep_set(self.cfg, ["openai", "model"], self.ed_primary_model.text().strip())
        deep_set(self.cfg, ["openai", "fallback_model"], self.ed_fallback_model.text().strip())
        deep_set(self.cfg, ["openai", "use_json_schema"], self.cb_use_json_schema.isChecked())
        deep_set(self.cfg, ["openai", "temperature"], float(self.sp_temperature.value()))
        deep_set(self.cfg, ["openai", "max_output_tokens"], int(self.sp_max_tokens.value()))
        deep_set(self.cfg, ["openai", "timeout_sec"], int(self.sp_timeout.value()))
        deep_set(self.cfg, ["openai", "image_dpi"], int(self.sp_image_dpi.value()))
        deep_set(self.cfg, ["openai", "image_max_pages"], int(self.sp_image_max_pages.value()))
        deep_set(self.cfg, ["openai", "image_enhance"], self.cb_image_enhance.isChecked())
        deep_set(self.cfg, ["openai", "image_variants"], int(self.sp_image_variants.value()))
        deep_set(self.cfg, ["openai", "allow_synthetic_items"], self.cb_allow_synth.isChecked())
        save_yaml(self.config_path, self.cfg)
        QMessageBox.information(self, "Nastavení", "Uloženo.")

    # --- Backup / restore / reset ---

    def _ask_password(self, title: str, prompt: str) -> bool:
        pwd, ok = QInputDialog.getText(self, title, prompt, QLineEdit.Password)
        return bool(ok and pwd.strip() == "+Sin8glov8")

    def _db_files(self) -> List[Path]:
        files = [self.paths.db_path]
        for suf in (".wal", ".shm"):
            cand = self.paths.db_path.with_suffix(self.paths.db_path.suffix + suf)
            files.append(cand)
        return files

    def _clear_dir(self, path_str: str, errors: List[str]) -> None:
        if not path_str:
            return
        p = Path(path_str)
        if not p.exists():
            return
        for child in p.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except Exception as exc:
                errors.append(f"{child}: {exc}")

    def _ask_backup_save_path(self, suffix: str = "") -> Optional[Path]:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"kajovospend-backup{('-' + suffix) if suffix else ''}-{ts}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Uložit zálohu", base, "Záloha (*.zip)")
        return Path(path) if path else None

    def _backup_to_archive(self, dest: Path) -> Tuple[bool, str]:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # uzavřít DB, aby se zapsal WAL
            try:
                self.engine.dispose()
            except Exception:
                pass

            in_dir = Path(self.ed_input_dir.text().strip() or self.cfg.get("paths", {}).get("input_dir") or "")
            out_dir = Path(self.ed_output_dir.text().strip() or self.cfg.get("paths", {}).get("output_dir") or "")

            with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                if in_dir.exists():
                    for p in in_dir.rglob("*"):
                        if p.is_file():
                            zf.write(p, Path("IN") / p.relative_to(in_dir))
                if out_dir.exists():
                    for p in out_dir.rglob("*"):
                        if p.is_file():
                            zf.write(p, Path("OUT") / p.relative_to(out_dir))
                for db_file in self._db_files():
                    if db_file.exists():
                        zf.write(db_file, Path("DB") / db_file.name)
                if self.config_path.exists():
                    zf.write(self.config_path, Path("config") / self.config_path.name)
            return True, f"Záloha uložena do {dest}"
        except Exception as exc:
            return False, str(exc)

    def _restore_from_archive(self, archive: Path, errors: List[str]) -> None:
        with tempfile.TemporaryDirectory(prefix="kajovospend_restore_") as tmp:
            tmp_path = Path(tmp)
            try:
                with zipfile.ZipFile(archive, "r") as zf:
                    zf.extractall(tmp_path)
            except Exception as exc:
                errors.append(f"Čtení zálohy selhalo: {exc}")
                return

            orig_in_dir = Path(self.ed_input_dir.text().strip() or self.cfg.get("paths", {}).get("input_dir") or "")
            orig_out_dir = Path(self.ed_output_dir.text().strip() or self.cfg.get("paths", {}).get("output_dir") or "")
            orig_db_files = list(self._db_files())

            # zavřít DB
            try:
                self.engine.dispose()
            except Exception as exc:
                errors.append(f"DB dispose: {exc}")

            # vrátit config (pokud je v záloze) dřív, aby se načetly nové cesty
            cfg_src = tmp_path / "config" / self.config_path.name
            if cfg_src.exists():
                try:
                    shutil.copy2(cfg_src, self.config_path)
                except Exception as exc:
                    errors.append(f"Obnova configu: {exc}")

            # načti případně nové cesty
            try:
                self.cfg = self._load_or_create_config()
            except Exception as exc:
                errors.append(f"Načtení configu po obnově: {exc}")

            self._apply_cfg_to_fields()
            new_in_dir = Path(self.ed_input_dir.text().strip() or self.cfg.get("paths", {}).get("input_dir") or "")
            new_out_dir = Path(self.ed_output_dir.text().strip() or self.cfg.get("paths", {}).get("output_dir") or "")

            # vymazat staré i nové cesty, aby nic nezůstalo
            for d in {orig_in_dir, orig_out_dir, new_in_dir, new_out_dir}:
                self._clear_dir(str(d), errors)

            # smazat staré DB soubory (původní i nové umístění)
            db_paths_to_clear = set(orig_db_files)
            # po reloadu configu můžou být jiné cesty
            try:
                self.paths = resolve_app_paths(
                    self.cfg["app"].get("data_dir"),
                    self.cfg["app"].get("db_path"),
                    self.cfg["app"].get("log_dir"),
                    self.cfg.get("ocr", {}).get("models_dir"),
                )
            except Exception as exc:
                errors.append(f"Resolve paths po obnově: {exc}")
            else:
                db_paths_to_clear.update(self._db_files())

            for db_file in db_paths_to_clear:
                try:
                    if db_file.exists():
                        db_file.unlink()
                except Exception as exc:
                    errors.append(f"Smazání DB souboru {db_file}: {exc}")

            # obnovit IN/OUT
            for name, target_dir in (("IN", new_in_dir), ("OUT", new_out_dir)):
                src = tmp_path / name
                if src.exists():
                    try:
                        target_dir.mkdir(parents=True, exist_ok=True)
                        for p in src.rglob("*"):
                            if p.is_file():
                                dest = target_dir / p.relative_to(src)
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(p, dest)
                    except Exception as exc:
                        errors.append(f"Obnova {name}: {exc}")

            # obnovit DB soubory
            db_src_dir = tmp_path / "DB"
            if db_src_dir.exists():
                for p in db_src_dir.iterdir():
                    try:
                        self.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, self.paths.db_path.parent / p.name)
                    except Exception as exc:
                        errors.append(f"Obnova DB {p.name}: {exc}")

            # znovu inicializovat engine + processor
            try:
                self.engine = make_engine(str(self.paths.db_path))
                init_db(self.engine)
                self.sf = make_session_factory(self.engine)
                self.processor = Processor(self.cfg, self.paths, self.log)
            except Exception as exc:
                errors.append(f"Reinit po obnově: {exc}")

    def _has_existing_data(self) -> bool:
        in_dir = Path(self.ed_input_dir.text().strip() or self.cfg.get("paths", {}).get("input_dir") or "")
        out_dir = Path(self.ed_output_dir.text().strip() or self.cfg.get("paths", {}).get("output_dir") or "")
        if in_dir.exists() and any(in_dir.iterdir()):
            return True
        if out_dir.exists() and any(out_dir.iterdir()):
            return True
        if self.paths.db_path.exists():
            return True
        return False

    def _apply_cfg_to_fields(self) -> None:
        paths_cfg = self.cfg.get("paths", {}) if isinstance(self.cfg, dict) else {}
        openai_cfg = self.cfg.get("openai", {}) if isinstance(self.cfg, dict) else {}
        self.ed_input_dir.setText(str(paths_cfg.get("input_dir", "")))
        self.ed_output_dir.setText(str(paths_cfg.get("output_dir", "")))

        if isinstance(openai_cfg, dict):
            self.cb_openai_enabled.setChecked(bool(openai_cfg.get("enabled", False)))
            self.cb_openai_auto.setChecked(bool(openai_cfg.get("auto_enable", True)))
            self.cb_openai_primary.setChecked(bool(openai_cfg.get("primary_enabled", True)))
            self.cb_openai_fallback.setChecked(bool(openai_cfg.get("fallback_enabled", True)))
            self.ed_primary_model.setText(openai_cfg.get("model", "auto"))
            self.ed_fallback_model.setText(openai_cfg.get("fallback_model", ""))
            self.cb_use_json_schema.setChecked(bool(openai_cfg.get("use_json_schema", True)))
            self.sp_temperature.setValue(float(openai_cfg.get("temperature", 0.0) or 0.0))
            self.sp_max_tokens.setValue(int(openai_cfg.get("max_output_tokens", 2000) or 2000))
            self.sp_timeout.setValue(int(openai_cfg.get("timeout_sec", 60) or 60))
            self.sp_image_dpi.setValue(int(openai_cfg.get("image_dpi", 300) or 300))
            self.sp_image_max_pages.setValue(int(openai_cfg.get("image_max_pages", 3) or 3))
            self.sp_image_variants.setValue(int(openai_cfg.get("image_variants", 2) or 2))
            self.cb_image_enhance.setChecked(bool(openai_cfg.get("image_enhance", True)))
            self.cb_allow_synth.setChecked(bool(openai_cfg.get("allow_synthetic_items", False)))
    def _on_reset_program(self):
        if self._import_running:
            QMessageBox.warning(self, "RESET PROGRAMU", "Nejprve dokonči probíhající import.")
            return

        if not self._ask_password("RESET PROGRAMU", "Zadej heslo pro reset:"):
            QMessageBox.warning(self, "RESET PROGRAMU", "Chybné heslo nebo zrušeno.")
            return

        # volitelná záloha před smazáním
        backup_choice = QMessageBox.question(
            self,
            "RESET PROGRAMU",
            "Chceš před resetem uložit kompletní data (IN, OUT, DB, config) do jednoho souboru?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if backup_choice == QMessageBox.Yes:
            dest = self._ask_backup_save_path("pred-resetem")
            if not dest:
                QMessageBox.information(self, "RESET PROGRAMU", "Reset zrušen (nevybrán soubor pro zálohu).")
                return
            ok, msg = self._backup_to_archive(dest)
            if not ok:
                QMessageBox.critical(self, "RESET PROGRAMU", f"Záloha selhala: {msg}")
                return

        confirm = QMessageBox.question(
            self,
            "RESET PROGRAMU",
            "Tato akce smaže databázi a vyprázdní IN/OUT. Pokračovat?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        errors: list[str] = []

        in_dir = self.ed_input_dir.text().strip() or str(self.cfg.get("paths", {}).get("input_dir") or "")
        out_dir = self.ed_output_dir.text().strip() or str(self.cfg.get("paths", {}).get("output_dir") or "")

        self._clear_dir(in_dir, errors)
        self._clear_dir(out_dir, errors)

        try:
            self.engine.dispose()
        except Exception as exc:
            errors.append(f"DB dispose: {exc}")

        try:
            if self.paths.db_path.exists():
                self.paths.db_path.unlink()
        except Exception as exc:
            errors.append(f"Smazání DB souboru: {exc}")

        try:
            self.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = make_engine(str(self.paths.db_path))
            init_db(self.engine)
            self.sf = make_session_factory(self.engine)
            self.processor = Processor(self.cfg, self.paths, self.log)
        except Exception as exc:
            errors.append(f"Init nové DB: {exc}")

        self._import_status = "Připraveno."
        try:
            self.lbl_run_status.setText(self._import_status)
        except Exception:
            pass

        try:
            self.refresh_all_v2()
        except Exception as exc:
            errors.append(f"Refresh UI: {exc}")

        if errors:
            QMessageBox.warning(self, "RESET PROGRAMU", "Hotovo s výjimkami:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "RESET PROGRAMU", "Program byl vyresetován.")

    def _on_backup_program(self):
        if self._import_running:
            QMessageBox.warning(self, "ZÁLOHOVAT PROGRAM", "Nejprve dokonči probíhající import.")
            return
        dest = self._ask_backup_save_path()
        if not dest:
            return
        ok, msg = self._backup_to_archive(dest)
        if ok:
            QMessageBox.information(self, "ZÁLOHOVAT PROGRAM", msg)
        else:
            QMessageBox.critical(self, "ZÁLOHOVAT PROGRAM", f"Záloha selhala: {msg}")

    def _on_restore_program(self):
        if self._import_running:
            QMessageBox.warning(self, "OBNOVIT PROGRAM", "Nejprve dokonči probíhající import.")
            return
        if not self._ask_password("OBNOVIT PROGRAM", "Zadej heslo pro obnovu:"):
            QMessageBox.warning(self, "OBNOVIT PROGRAM", "Chybné heslo nebo zrušeno.")
            return

        path_str, _ = QFileDialog.getOpenFileName(self, "Vyber zálohu", "", "Záloha (*.zip)")
        if not path_str:
            return
        archive = Path(path_str)

        if self._has_existing_data():
            resp = QMessageBox.question(
                self,
                "OBNOVIT PROGRAM",
                "Program obsahuje data, která budou přepsána. Uložit aktuální stav před obnovou?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if resp == QMessageBox.Yes:
                backup_path = self._ask_backup_save_path("pred-obnovou")
                if not backup_path:
                    QMessageBox.information(self, "OBNOVIT PROGRAM", "Obnova zrušena (nevybrán soubor pro zálohu).")
                    return
                ok, msg = self._backup_to_archive(backup_path)
                if not ok:
                    QMessageBox.critical(self, "OBNOVIT PROGRAM", f"Záloha před obnovou selhala: {msg}")
                    return

        errors: list[str] = []
        self._restore_from_archive(archive, errors)

        self._import_status = "Připraveno."
        try:
            self.lbl_run_status.setText(self._import_status)
        except Exception:
            pass

        try:
            self.refresh_all_v2()
        except Exception as exc:
            errors.append(f"Refresh UI: {exc}")

        if errors:
            QMessageBox.warning(self, "OBNOVIT PROGRAM", "Obnova hotova s výjimkami:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "OBNOVIT PROGRAM", "Program byl obnoven ze zálohy.")

    def _on_api_show_toggle(self):
        if self.ed_api_key.echoMode() == QLineEdit.Password:
            self.ed_api_key.setEchoMode(QLineEdit.Normal)
            self.btn_api_show.setText("Skrýt")
        else:
            self.ed_api_key.setEchoMode(QLineEdit.Password)
            self.btn_api_show.setText("Zobrazit")

    def _on_api_clear(self):
        self.ed_api_key.clear()

    def _on_api_models(self):
        api_key = self.ed_api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "Modely OpenAI", "Nejdriv vypln API key.")
            return
        dlg = QProgressDialog("Nacitam modely...", "", 0, 0, self)
        dlg.setWindowTitle("OpenAI")
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.show()
        QApplication.processEvents()
        try:
            ids = list_models(api_key)
        except Exception as e:
            dlg.close()
            QMessageBox.critical(self, "Modely OpenAI", f"Nelze nacist modely: {e}")
            return
        dlg.close()
        if not ids:
            QMessageBox.information(self, "Modely OpenAI", "Zadne modely nenalezeny.")
            return
        items = [i for i in ids if i.startswith("gpt")]
        if not items:
            items = ids
        choice, ok = QInputDialog.getItem(self, "Modely OpenAI", "Vyber model:", items, 0, False)
        if not ok or not choice:
            return

        target = None
        if self.ed_primary_model.hasFocus():
            target = "primary"
        elif self.ed_fallback_model.hasFocus():
            target = "fallback"
        if target is None:
            tchoice, tok = QInputDialog.getItem(
                self,
                "Pouzit model",
                "Kam model ulozit?",
                ["Primarni model", "Fallback model"],
                0,
                False,
            )
            if not tok:
                return
            target = "primary" if "Primarni" in tchoice else "fallback"

        if target == "fallback":
            self.ed_fallback_model.setText(choice)
        else:
            self.ed_primary_model.setText(choice)

    def _on_import_progress(self, msg: str) -> None:
        self._import_status = msg
        try:
            self.lbl_run_status.setText(msg)
        except Exception:
            pass

    def _on_import_done(self, res: dict) -> None:
        imported = int(res.get("imported") or 0)
        total = int(res.get("total") or imported)
        msg = str(res.get("message") or "Hotovo.")
        self._import_status = f"{msg} ({imported}/{total})"
        self._import_running = False
        try:
            self.btn_import.setEnabled(True)
            self.lbl_run_status.setText(self._import_status)
        except Exception:
            pass

        # refresh views (counts/stats + lists)
        try:
            self.refresh_all_v2()
        except Exception:
            pass
        try:
            self.refresh_ops()
            self.refresh_unprocessed()
            self.refresh_money()
        except Exception:
            pass

    def _on_import_error(self, msg: str) -> None:
        self._import_running = False
        self._import_status = f"Chyba: {msg}"
        try:
            self.btn_import.setEnabled(True)
            self.lbl_run_status.setText(self._import_status)
        except Exception:
            pass
        QMessageBox.critical(self, "IMPORTUJ", msg)

    def on_import_clicked(self) -> None:
        if self._import_running:
            return
        input_dir_val = self.ed_input_dir.text().strip() or str(self.cfg.get("paths", {}).get("input_dir") or "")
        if not input_dir_val:
            QMessageBox.warning(self, "IMPORTUJ", "Není nastaven INPUT adresář (Nastavení → Input adresář).")
            return
        output_dir_val = self.ed_output_dir.text().strip() or str(self.cfg.get("paths", {}).get("output_dir") or "")
        deep_set(self.cfg, ["paths", "input_dir"], input_dir_val)
        if output_dir_val:
            deep_set(self.cfg, ["paths", "output_dir"], output_dir_val)

        self._import_running = True
        self._import_status = "Načítám INPUT…"
        try:
            self.btn_import.setEnabled(False)
            self.lbl_run_status.setText(self._import_status)
        except Exception:
            pass

        th = QThread(self)
        wk = _ImportWorker(self.cfg, self.sf, self.processor)
        wk.moveToThread(th)
        th.started.connect(wk.run)
        wk.progress.connect(self._on_import_progress)
        wk.done.connect(self._on_import_done)
        wk.error.connect(self._on_import_error)

        cleaned = False

        def _cleanup_thread():
            nonlocal cleaned
            if cleaned:
                return
            # nikdy nečekej na sebe sama; přeposlat do UI vlákna, pokud běžíme v pracovním vlákně
            if QThread.currentThread() is th:
                QTimer.singleShot(0, self, _cleanup_thread)
                return
            cleaned = True
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
                self._workers.remove(wk)
            except Exception:
                pass

        wk.done.connect(lambda _res: _cleanup_thread())
        wk.error.connect(lambda _msg: _cleanup_thread())
        th.finished.connect(_cleanup_thread)

        # Keep references alive
        self._threads.append(th)
        self._workers.append(wk)  # type: ignore

        th.start()

    def refresh_suppliers(self):
        q = self.sup_filter.text()
        try:
            with self.sf() as session:
                sups = db_api.list_suppliers(session, q=q)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Dodavatelé",
                f"Nepodařilo se načíst seznam dodavatelů: {exc}",
            )
            return

        keep_id = self._selected_supplier_id()
        rows: List[List[str]] = []
        for s in sups:
            rows.append(
                [
                    str(s.id),
                    s.name or "",
                    s.ico,
                    s.dic or "",
                    s.city or "",
                ]
            )
        self.sup_model = TableModel(
            ["ID", "Název", "IČO", "DIČ", "Místo sídla"],
            rows,
        )
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

    def _on_sup_filter_changed(self, _=None):
        ms = int(self.cfg.get("performance", {}).get("supplier_debounce_ms", 250) or 250)
        try:
            self._sup_filter_timer.stop()
        except Exception:
            pass
        self._sup_filter_timer.start(ms)

    def refresh_all(self):
        self.refresh_dashboard()
        self.refresh_suppliers()
        self.refresh_documents()
        self.refresh_unprocessed()
        self.refresh_ops()
        self.refresh_money()

    def refresh_dashboard(self):
        # Keep API compatibility, but do not block UI thread.
        self._refresh_dashboard_async()

    
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
                model.rows.extend(new_rows)
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
            doc_id = int(model.rows[idx.row()][0])
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

    def refresh_unprocessed(self):
        """Sloučený seznam nedokončených dokladů (duplikáty, karanténa, requires_review, chyby)."""
        with self.sf() as session:
            data = db_api.list_pending(session)
            rows = []
            for d, f in data:
                status = f.status
                if status == "PROCESSED" and d.requires_review:
                    status = "REVIEW"
                if status == "PROCESSED":
                    badge = "🟢"
                elif status in ("NEW", "QUEUED", "RUNNING", "REVIEW"):
                    badge = "🟠"
                else:
                    badge = "🔴"
                registered = f.created_at.isoformat(sep=" ", timespec="seconds") if getattr(f, "created_at", None) else ""
                finished = f.processed_at.isoformat(sep=" ", timespec="seconds") if getattr(f, "processed_at", None) else ""
                reason = d.review_reasons or f.last_error or ""
                rows.append([
                    badge,
                    status,
                    registered,
                    finished,
                    d.issue_date.isoformat() if d.issue_date else "",
                    d.supplier_ico or "",
                    d.doc_number or "",
                    d.total_with_vat or 0.0,
                    f.current_path,
                    reason,
                    d.id,
                ])
        headers = ["Stav", "Status", "Zaregistrováno", "Dokončeno", "Datum", "IČO", "Číslo", "Celkem", "Soubor", "Důvod", "ID"]
        self.unproc_table.setModel(TableModel(headers, rows))
        self.unproc_table.resizeColumnsToContents()

    

    def refresh_run_state(self):
        with self.sf() as session:
            st = db_api.service_state(session)
        if not st:
            return

        def _set(key: str, val: str) -> None:
            try:
                t = getattr(self, "_dash_tiles", {}).get(key)
                if t:
                    t.set_value(val)
            except Exception:
                pass

        try:
            queue = int(st.queue_size or 0)
            inflight = int(st.inflight or 0)
            remaining = max(0, queue + inflight)

            phase = (st.current_phase or "idle").strip()
            current = Path(st.current_path).name if st.current_path else "-"

            running_txt = "Zapnuto" if bool(st.running) else "Vypnuto"
            if getattr(st, "stuck", False):
                running_txt += " (zaseknuto)"
            _set("import_power", running_txt)

            prog = ""
            if st.current_progress is not None:
                try:
                    prog = f"{float(st.current_progress):.0f} %"
                except Exception:
                    prog = ""
            now_txt = f"{phase} • {current}" if current and current != "-" else phase
            if prog:
                now_txt += f" • {prog}"
            _set("import_activity", now_txt)

            if remaining <= 0:
                next_txt = "Nic. Fronta je prázdná."
            elif inflight > 0:
                next_txt = "Dokončení běžících úloh a pokračování dalšími soubory z fronty."
            else:
                next_txt = "Start dalšího souboru z fronty."
            _set("import_next", next_txt)

            _set("queue", f"{queue} / {inflight} / {remaining}")

            avg = float(getattr(self, "_avg_job_seconds", 0.0) or 0.0)
            if remaining > 0 and avg > 1.0:
                eta_sec = int(remaining * avg)
                mm, ss = divmod(eta_sec, 60)
                hh, mm = divmod(mm, 60)
                if hh:
                    eta_txt = f"{hh} h {mm} min"
                elif mm:
                    eta_txt = f"{mm} min"
                else:
                    eta_txt = f"{ss} s"
            elif remaining > 0:
                eta_txt = "–"
            else:
                eta_txt = "0"
            _set("import_eta", eta_txt)

            if remaining == 0 and not st.running:
                self.lbl_run_status.setText("Vše hotovo.")
            elif st.running:
                self.lbl_run_status.setText("Import běží.")
            else:
                self.lbl_run_status.setText("Import je vypnutý.")

            if st.last_error:
                last_msg = f"Chyba: {st.last_error}"
                if st.last_error != self._last_run_error:
                    self._last_run_error = st.last_error
                    self._last_run_success = None
                    self.run_log.append(last_msg)
            elif st.last_success:
                last_ok = st.last_success.isoformat(sep=" ", timespec="seconds") if isinstance(st.last_success, dt.datetime) else str(st.last_success)
                last_msg = f"OK: {last_ok}"
                if last_ok != self._last_run_success:
                    self._last_run_success = last_ok
                    self._last_run_error = None
                    self.run_log.append(last_msg)
        except Exception:
            pass


    def refresh_ops(self):
        # Provozní panel: 1 řádek = 1 soubor (ImportJob), doplněno o velikost/mtime + semafor.
        with self.sf() as session:
            jobs = session.execute(
                select(ImportJob)
                .order_by(ImportJob.created_at.desc())
                .limit(500)
            ).scalars().all()

        rows = []
        for j in jobs:
            status = str(j.status or "").upper()
            if status == "PROCESSED":
                light = "green"
                result = "Kompletně vytěženo"
            elif status in ("NEW", "QUEUED", "RUNNING"):
                light = "orange"
                result = "Čeká / zpracovává se"
            else:
                light = "red"
                if status == "DUPLICATE":
                    result = "Duplicita (odloženo)"
                elif status == "QUARANTINE":
                    result = "Nekompletní – karanténa"
                else:
                    result = "Chyba"

            size = ""
            mtime = ""
            doc_path = Path(str(j.path or ""))
            try:
                if doc_path.exists():
                    st = doc_path.stat()
                    size = (
                        f"{st.st_size/1024.0:.0f} KB"
                        if st.st_size < 1024 * 1024
                        else f"{st.st_size/1024/1024:.2f} MB"
                    )
                    mtime = dt.datetime.fromtimestamp(st.st_mtime).isoformat(sep=" ", timespec="seconds")
                else:
                    out_base = Path(self.cfg.get("paths", {}).get("output_dir", "") or "")
                    qdir = out_base / self.cfg.get("paths", {}).get("quarantine_dir_name", "KARANTENA")
                    ddir = out_base / self.cfg.get("paths", {}).get("duplicate_dir_name", "DUPLICITY")
                    for cand in (out_base / doc_path.name, qdir / doc_path.name, ddir / doc_path.name):
                        if cand.exists():
                            st = cand.stat()
                            size = (
                                f"{st.st_size/1024.0:.0f} KB"
                                if st.st_size < 1024 * 1024
                                else f"{st.st_size/1024/1024:.2f} MB"
                            )
                            mtime = dt.datetime.fromtimestamp(st.st_mtime).isoformat(sep=" ", timespec="seconds")
                            break
            except Exception:
                pass

            started = j.started_at.isoformat(sep=" ", timespec="seconds") if j.started_at else ""
            finished = j.finished_at.isoformat(sep=" ", timespec="seconds") if j.finished_at else ""
            err = (j.error or "").strip()

            rows.append(
                [
                    light,
                    doc_path.name,
                    size,
                    mtime,
                    started,
                    finished,
                    status,
                    result,
                    err,
                ]
            )

        headers = [
            "",
            "Soubor",
            "Velikost",
            "Posl. úprava",
            "Start",
            "Konec",
            "Status",
            "Výsledek",
            "Důvod",
        ]
        self.ops_table.setModel(TableModel(headers, rows))
        try:
            self.ops_table.resizeColumnsToContents()
        except Exception:
            pass



    def on_unproc_openai(self):
        ids = self._unproc_selected_ids()
        if not ids:
            QMessageBox.information(self, "OpenAI", "Vyber řádky v tabulce.")
            return
        api_key = self.ed_api_key.text().strip()
        model = self.ed_fallback_model.text().strip() or self.ed_primary_model.text().strip() or "auto"
        if not api_key:
            QMessageBox.warning(self, "OpenAI", "Není vyplněn API key (Nastavení).")
            return
        success_ids: list[int] = []
        errors: list[str] = []
        with self.sf() as session:
            for doc_id in ids:
                d = session.get(Document, doc_id)
                if not d:
                    continue
                f = session.get(DocumentFile, d.file_id)
                if not f or not Path(f.current_path).exists():
                    errors.append(f"ID {doc_id}: soubor nenalezen")
                    continue
                try:
                    ok, raw_resp, used_model = self._call_openai_responses(api_key, model, Path(f.current_path))
                    if ok:
                        f.status = "PROCESSED"
                        f.processed_at = dt.datetime.utcnow()
                        d.requires_review = False
                        d.review_reasons = None
                        d.extraction_method = "openai"
                        d.extraction_confidence = max(0.7, float(d.extraction_confidence or 0.0))
                        d.openai_model = used_model or model
                        d.openai_raw_response = raw_resp
                        session.add(f)
                        session.add(d)
                        success_ids.append(doc_id)
                    else:
                        errors.append(f"ID {doc_id}: OpenAI odpověď není úspěšná")
                except Exception as e:
                    errors.append(f"ID {doc_id}: {e}")
            session.commit()
        self.refresh_unprocessed()
        if success_ids:
            QMessageBox.information(self, "OpenAI", f"Odesláno a zpracováno: {len(success_ids)} dokladů.")
        if errors:
            QMessageBox.warning(self, "OpenAI", "\n".join(errors))

    def on_unproc_manual(self):
        ids = self._unproc_selected_ids()
        if not ids:
            QMessageBox.information(self, "Ruční vytěžení", "Vyber řádky v tabulce.")
            return
        with self.sf() as session:
            for doc_id in ids:
                d = session.get(Document, doc_id)
                if not d:
                    continue
                f = session.get(DocumentFile, d.file_id)
                if f:
                    f.status = "PROCESSED"
                    f.processed_at = dt.datetime.utcnow()
                    session.add(f)
                d.requires_review = False
                d.review_reasons = None
                d.extraction_method = "manual"
                d.extraction_confidence = 1.0
                session.add(d)
            session.commit()
        self.refresh_unprocessed()
        QMessageBox.information(self, "Ruční vytěžení", "Označeno jako ručně zpracované.")

    def _unproc_selected_ids(self) -> list[int]:
        model = self.unproc_table.model()
        sm = self.unproc_table.selectionModel()
        if not model or not sm:
            return []
        ids = []
        for idx in sm.selectedRows():
            try:
                doc_id = int(model.rows[idx.row()][10])
                ids.append(doc_id)
            except Exception:
                continue
        return ids

    def _enhance_for_openai(self, image: Image.Image) -> Image.Image:
        img = image.convert("RGB")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
        return img

    def _prepare_openai_images(self, file_path: Path) -> List[Tuple[str, bytes]]:
        cfg = self.cfg.get("openai") if isinstance(self.cfg, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}
        dpi = int(cfg.get("image_dpi", 300) or 300)
        max_pages = int(cfg.get("image_max_pages", 3) or 3)
        enhance = bool(cfg.get("image_enhance", True))
        variants = int(cfg.get("image_variants", 2) or 2)
        if variants < 1:
            variants = 1

        images_payload: List[Tuple[str, bytes]] = []
        try:
            if file_path.suffix.lower() == ".pdf":
                imgs = render_pdf_to_images(file_path, dpi=dpi, max_pages=max_pages, start_page=0)
            else:
                with Image.open(file_path) as im:
                    imgs = [im.convert("RGB")]
        except Exception:
            return images_payload

        for im in imgs[:max_pages]:
            try:
                bio = BytesIO()
                im.convert("RGB").save(bio, format="PNG")
                images_payload.append(("image/png", bio.getvalue()))
                if enhance and variants > 1:
                    enh = self._enhance_for_openai(im)
                    bio2 = BytesIO()
                    enh.save(bio2, format="PNG")
                    images_payload.append(("image/png", bio2.getvalue()))
            except Exception:
                continue
        return images_payload

    def _call_openai_responses(self, api_key: str, model: str, file_path: Path) -> tuple[bool, str, str]:
        """
        Sync volani OpenAI /v1/responses s JSON vystupem.
        """
        cfg = self.cfg.get("openai") if isinstance(self.cfg, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}
        timeout = int(cfg.get("timeout_sec", 60) or 60)
        use_json_schema = bool(cfg.get("use_json_schema", True))
        temperature = float(cfg.get("temperature", 0.0) or 0.0)
        max_output_tokens = int(cfg.get("max_output_tokens", 2000) or 2000)
        fallback_model = str(cfg.get("fallback_model") or "").strip() or None

        images_payload = self._prepare_openai_images(file_path)
        oai_cfg = OpenAIConfig(
            api_key=api_key,
            model=model or "auto",
            fallback_model=fallback_model,
            use_json_schema=use_json_schema,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        try:
            obj, raw, used_model = extract_with_openai_fallback(
                oai_cfg,
                ocr_text="",
                images=images_payload,
                timeout=timeout,
            )
            if isinstance(obj, dict):
                return True, raw, used_model
            return False, raw, used_model
        except Exception as exc:
            return False, str(exc), ""

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

    
    def refresh_suspicious(self):
        # Zachováno pro zpětnou kompatibilitu volání; přesměrováno na nový sjednocený seznam.
        self.refresh_unprocessed()

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

    def _apply_dashboard(self, c: Dict[str, Any] | None, rs: Dict[str, Any] | None) -> None:
        # RUN dashboard values (DB + filesystem + status)
        try:
            # keep original import status string if used elsewhere
            self.lbl_run_status.setText(self._import_status)
        except Exception:
            pass

        if c:
            try:
                q = int(c.get("quarantine", 0) or 0)
                d = int(c.get("duplicates", 0) or 0)
                total = q + d
                try:
                    self._dash_tiles["quarantine_total"].set_value(str(total))
                    self._dash_tiles["quarantine_dup"].set_value(str(d))
                    self._dash_tiles["quarantine_fail"].set_value(str(q))
                except Exception:
                    pass

                in_wait = int(getattr(self, "_dash_in_waiting", 0) or 0)
                try:
                    self._dash_tiles["in_waiting"].set_value(str(in_wait))
                except Exception:
                    pass
            except Exception:
                pass

        if not rs:
            return

        def fmt_int(n: int) -> str:
            return f"{int(n):,}".replace(",", " ")

        def fmt_money(x: float) -> str:
            return f"{float(x):,.2f}".replace(",", " ")

        try:
            if "suppliers" in self._stat_labels:
                self._stat_labels["suppliers"].setText(fmt_int(rs.get("suppliers", 0) or 0))
            if "receipts" in self._stat_labels:
                self._stat_labels["receipts"].setText(fmt_int(rs.get("receipts", 0) or 0))
            if "items" in self._stat_labels:
                self._stat_labels["items"].setText(fmt_int(rs.get("items", 0) or 0))

            if "sum_items_wo_vat" in self._stat_labels:
                self._stat_labels["sum_items_wo_vat"].setText(fmt_money(rs.get("sum_items_wo_vat", 0.0) or 0.0))
            if "sum_items_w_vat" in self._stat_labels:
                self._stat_labels["sum_items_w_vat"].setText(fmt_money(rs.get("sum_items_w_vat", 0.0) or 0.0))
        except Exception:
            pass


    def _refresh_dashboard_async(self) -> None:
        # prevent overlapping refreshes
        if self._dash_refresh_inflight:
            return
        self._dash_refresh_inflight = True

        def work():
            c = None
            rs = None
            in_waiting = 0
            avg_job_seconds = 0.0
            try:
                # filesystem: INPUT waiting
                try:
                    input_dir = Path(self.cfg.get("paths", {}).get("input_dir", "") or "")
                    if input_dir.exists():
                        exts = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
                        in_waiting = len([p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in exts])
                except Exception:
                    in_waiting = 0

                with self.sf() as session:
                    c = db_api.counts(session)
                    rs = db_api.run_stats(session)

                    # recent average processing time (PROCESSED only; best-effort)
                    try:
                        jobs = session.execute(
                            select(ImportJob.started_at, ImportJob.finished_at)
                            .where(ImportJob.status == "PROCESSED")
                            .where(ImportJob.started_at.is_not(None))
                            .where(ImportJob.finished_at.is_not(None))
                            .order_by(ImportJob.finished_at.desc())
                            .limit(50)
                        ).all()
                        durs: list[float] = []
                        for s, f in jobs:
                            try:
                                sec = (f - s).total_seconds()
                                if 1 <= sec <= 60 * 60:
                                    durs.append(float(sec))
                            except Exception:
                                pass
                        if durs:
                            avg_job_seconds = float(sum(durs) / len(durs))
                    except Exception:
                        avg_job_seconds = 0.0
            except Exception as e:
                c = None
                rs = None
                try:
                    self.log.debug("run tab stats failed: %s", e)
                except Exception:
                    pass
            return (c, rs, in_waiting, avg_job_seconds)

        def done(res):
            try:
                c, rs, in_waiting, avg_job_seconds = res
                self._dash_last_counts = c
                self._dash_in_waiting = int(in_waiting or 0)
                self._avg_job_seconds = float(avg_job_seconds or 0.0)
                self._apply_dashboard(c, rs)
            finally:
                self._dash_refresh_inflight = False

        def err(_msg: str):
            self._dash_refresh_inflight = False

        _SilentRunner.run(self, work, done, err, timeout_ms=12000)

