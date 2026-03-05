
from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPen, QPixmap, QRegularExpressionValidator, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from kajovospend.extract.standard_receipts import (
    FIELD_LEGEND,
    TemplateField,
    TemplateSchema,
    TemplateSchemaError,
    normalized_box_to_pixel_box,
    parse_template_schema_text,
    serialize_template_schema,
)
from kajovospend.ocr.pdf_render import render_pdf_to_images
from kajovospend.utils.hashing import sha256_file

FIELD_ORDER: List[Tuple[str, str, bool]] = [
    ("supplier_ico", "IČO", True),
    ("doc_number", "Číslo dokladu", True),
    ("issue_date", "Datum vystavení", True),
    ("total_with_vat", "Celkem včetně DPH", True),
    ("items_region", "Položky - oblast", False),
    ("bank_account", "Bankovní účet", False),
]
FIELD_LABELS = {key: label for key, label, _ in FIELD_ORDER}
REQUIRED_FIELDS = {key for key, _, required in FIELD_ORDER if required}
FIELD_COLORS = {k: v for k, v in FIELD_LEGEND}


@dataclass
class RoiRecord:
    field: str
    page: int
    box: Tuple[float, float, float, float]


def _normalize_ico_digits(value: str | None) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if not digits:
        return ""
    return digits.zfill(8) if len(digits) < 8 else digits


def _pil_to_qpixmap(image) -> QPixmap:
    rgba = image.convert("RGBA")
    w, h = rgba.size
    data = rgba.tobytes("raw", "RGBA")
    qimg = QImage(data, w, h, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class RoiHandleItem(QGraphicsRectItem):
    def __init__(self, parent_item: "RoiRectItem", handle: str):
        super().__init__(-4, -4, 8, 8, parent_item)
        self._parent_item = parent_item
        self.handle = handle
        self._start_scene_pos = QPointF()
        self._cursor_by_handle = {
            "nw": Qt.SizeFDiagCursor,
            "se": Qt.SizeFDiagCursor,
            "ne": Qt.SizeBDiagCursor,
            "sw": Qt.SizeBDiagCursor,
            "n": Qt.SizeVerCursor,
            "s": Qt.SizeVerCursor,
            "e": Qt.SizeHorCursor,
            "w": Qt.SizeHorCursor,
        }
        self.setBrush(QColor("#FFFFFF"))
        self.setPen(QPen(QColor("#1F2937"), 1.0))
        self.setZValue(100.0)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self.setCursor(self._cursor_by_handle.get(handle, Qt.ArrowCursor))

    def mousePressEvent(self, event) -> None:
        self._start_scene_pos = event.scenePos()
        self._parent_item.begin_resize()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        delta = event.scenePos() - self._start_scene_pos
        self._parent_item.resize_by_delta(self.handle, delta.x(), delta.y())
        self._start_scene_pos = event.scenePos()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._parent_item.finish_resize()
        event.accept()


class RoiRectItem(QGraphicsRectItem):
    def __init__(
        self,
        field: str,
        label: str,
        page: int,
        rect: QRectF,
        color: QColor,
        page_rect: QRectF,
        on_changed: Callable[[str, int, Tuple[float, float, float, float]], None],
        on_selected: Callable[[Optional[str]], None],
    ):
        super().__init__(0, 0, rect.width(), rect.height())
        self.field = field
        self.page = page
        self._page_rect = QRectF(page_rect)
        self._on_changed = on_changed
        self._on_selected = on_selected
        self._resizing = False

        self.setPos(rect.topLeft())
        self.setBrush(QColor(color.red(), color.green(), color.blue(), 60))
        self.setPen(QPen(color, 2.0))
        self.setZValue(10.0)

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

        self._label_item = QGraphicsSimpleTextItem(label, self)
        self._label_item.setBrush(QColor("#111827"))
        self._label_item.setZValue(20.0)

        self._handles: Dict[str, RoiHandleItem] = {
            key: RoiHandleItem(self, key)
            for key in ("nw", "n", "ne", "e", "se", "s", "sw", "w")
        }
        self._update_geometry_visuals()
        self._show_handles(False)

    def _show_handles(self, visible: bool) -> None:
        for handle in self._handles.values():
            handle.setVisible(visible)

    def _scene_rect(self) -> QRectF:
        return QRectF(self.pos().x(), self.pos().y(), self.rect().width(), self.rect().height())

    def _update_geometry_visuals(self) -> None:
        r = self.rect()
        positions = {
            "nw": QPointF(0, 0),
            "n": QPointF(r.width() / 2.0, 0),
            "ne": QPointF(r.width(), 0),
            "e": QPointF(r.width(), r.height() / 2.0),
            "se": QPointF(r.width(), r.height()),
            "s": QPointF(r.width() / 2.0, r.height()),
            "sw": QPointF(0, r.height()),
            "w": QPointF(0, r.height() / 2.0),
        }
        for key, pt in positions.items():
            self._handles[key].setPos(pt)
        self._label_item.setPos(6, 2)

    def _emit_changed(self) -> None:
        sr = self._scene_rect()
        width = self._page_rect.width() or 1.0
        height = self._page_rect.height() or 1.0
        x0 = (sr.left() - self._page_rect.left()) / width
        y0 = (sr.top() - self._page_rect.top()) / height
        x1 = (sr.right() - self._page_rect.left()) / width
        y1 = (sr.bottom() - self._page_rect.top()) / height
        box = (
            max(0.0, min(1.0, x0)),
            max(0.0, min(1.0, y0)),
            max(0.0, min(1.0, x1)),
            max(0.0, min(1.0, y1)),
        )
        self._on_changed(self.field, self.page, box)

    def begin_resize(self) -> None:
        self._resizing = True

    def finish_resize(self) -> None:
        self._resizing = False
        self._emit_changed()

    def resize_by_delta(self, handle: str, dx: float, dy: float) -> None:
        sr = self._scene_rect()
        left = sr.left()
        right = sr.right()
        top = sr.top()
        bottom = sr.bottom()
        min_size = 12.0

        if "w" in handle:
            left += dx
        if "e" in handle:
            right += dx
        if "n" in handle:
            top += dy
        if "s" in handle:
            bottom += dy

        left = max(self._page_rect.left(), min(left, self._page_rect.right() - min_size))
        right = min(self._page_rect.right(), max(right, self._page_rect.left() + min_size))
        top = max(self._page_rect.top(), min(top, self._page_rect.bottom() - min_size))
        bottom = min(self._page_rect.bottom(), max(bottom, self._page_rect.top() + min_size))

        if right - left < min_size:
            right = left + min_size
        if bottom - top < min_size:
            bottom = top + min_size

        self.setPos(QPointF(left, top))
        self.setRect(0, 0, right - left, bottom - top)
        self._update_geometry_visuals()

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):
        if change == QGraphicsItem.ItemPositionChange and self.scene():
            new_pos = QPointF(value)
            w = self.rect().width()
            h = self.rect().height()
            clamped_x = min(max(new_pos.x(), self._page_rect.left()), self._page_rect.right() - w)
            clamped_y = min(max(new_pos.y(), self._page_rect.top()), self._page_rect.bottom() - h)
            return QPointF(clamped_x, clamped_y)

        if change == QGraphicsItem.ItemPositionHasChanged and not self._resizing:
            self._emit_changed()

        if change == QGraphicsItem.ItemSelectedHasChanged:
            selected = bool(value)
            self._show_handles(selected)
            self._on_selected(self.field if selected else None)

        return super().itemChange(change, value)

class PdfTemplateCanvas(QGraphicsView):
    roi_drawn = Signal(str, int, tuple)
    roi_selected = Signal(str)
    roi_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = self._scene.addPixmap(QPixmap())
        self._page_rect = QRectF(0, 0, 1, 1)
        self._current_page = 1
        self._active_field: Optional[str] = None
        self._rois: Dict[str, RoiRecord] = {}
        self._roi_items: Dict[str, RoiRectItem] = {}
        self._is_drawing = False
        self._draw_origin = QPointF()
        self._draw_temp: Optional[QGraphicsRectItem] = None

        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)

    def clear(self) -> None:
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(QPixmap())
        self._scene.setSceneRect(0, 0, 1, 1)
        self._page_rect = QRectF(0, 0, 1, 1)
        self._roi_items = {}
        self._draw_temp = None
        self.resetTransform()

    def set_active_field(self, field: Optional[str]) -> None:
        self._active_field = field

    def set_page(self, page: int, pixmap: QPixmap, rois: Dict[str, RoiRecord]) -> None:
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(self._pixmap_item.boundingRect()))
        self._page_rect = QRectF(self._pixmap_item.boundingRect())
        self._current_page = int(page)
        self._rois = dict(rois)
        self._roi_items = {}

        for field, roi in self._rois.items():
            if int(roi.page) != self._current_page:
                continue
            px0, py0, px1, py1 = normalized_box_to_pixel_box(
                roi.box,
                int(self._page_rect.width()),
                int(self._page_rect.height()),
            )
            rect = QRectF(float(px0), float(py0), float(px1 - px0), float(py1 - py0))
            color = QColor(FIELD_COLORS.get(field, "#22C55E"))
            label = FIELD_LABELS.get(field, field)
            item = RoiRectItem(
                field=field,
                label=label,
                page=self._current_page,
                rect=rect,
                color=color,
                page_rect=self._page_rect,
                on_changed=self._on_item_changed,
                on_selected=self._on_item_selected,
            )
            self._scene.addItem(item)
            self._roi_items[field] = item

        self.fit_to_width()

    def _on_item_changed(self, field: str, page: int, box: Tuple[float, float, float, float]) -> None:
        self.roi_drawn.emit(field, int(page), box)

    def _on_item_selected(self, field: Optional[str]) -> None:
        if field:
            self.roi_selected.emit(field)
        else:
            self.roi_cleared.emit()

    def select_roi(self, field: str) -> None:
        item = self._roi_items.get(field)
        if not item:
            return
        for roi_item in self._roi_items.values():
            roi_item.setSelected(False)
        item.setSelected(True)
        self.centerOn(item)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.scale(1.2, 1.2)
            else:
                self.scale(0.85, 0.85)
            event.accept()
            return
        super().wheelEvent(event)

    def fit_to_width(self) -> None:
        if self._page_rect.isEmpty():
            return
        self.resetTransform()
        self.fitInView(self._page_rect, Qt.KeepAspectRatio)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._active_field and not self._page_rect.isEmpty():
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._page_rect.contains(scene_pos):
                hit_item = self.itemAt(event.position().toPoint())
                if not isinstance(hit_item, (RoiRectItem, RoiHandleItem)):
                    self._is_drawing = True
                    self._draw_origin = scene_pos
                    self._draw_temp = QGraphicsRectItem(QRectF(scene_pos, scene_pos))
                    self._draw_temp.setPen(QPen(QColor("#111827"), 1.5, Qt.DashLine))
                    self._draw_temp.setBrush(QColor(17, 24, 39, 30))
                    self._draw_temp.setZValue(200.0)
                    self._scene.addItem(self._draw_temp)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._is_drawing and self._draw_temp is not None:
            pos = self.mapToScene(event.position().toPoint())
            clamped = QPointF(
                min(max(pos.x(), self._page_rect.left()), self._page_rect.right()),
                min(max(pos.y(), self._page_rect.top()), self._page_rect.bottom()),
            )
            self._draw_temp.setRect(QRectF(self._draw_origin, clamped).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._is_drawing and event.button() == Qt.LeftButton:
            self._is_drawing = False
            if self._draw_temp is None:
                event.accept()
                return
            rect = self._draw_temp.rect().normalized().intersected(self._page_rect)
            self._scene.removeItem(self._draw_temp)
            self._draw_temp = None
            if rect.width() >= 8 and rect.height() >= 8 and self._active_field:
                width = self._page_rect.width() or 1.0
                height = self._page_rect.height() or 1.0
                box = (
                    (rect.left() - self._page_rect.left()) / width,
                    (rect.top() - self._page_rect.top()) / height,
                    (rect.right() - self._page_rect.left()) / width,
                    (rect.bottom() - self._page_rect.top()) / height,
                )
                self.roi_drawn.emit(self._active_field, int(self._current_page), box)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ReceiptTemplateEditorDialog(QDialog):
    def __init__(self, paths, cfg: Dict[str, Any], template: Dict[str, Any] | None = None, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.cfg = cfg or {}
        self._template = template or {}
        self._roi_by_field: Dict[str, RoiRecord] = {}
        self._selected_field: Optional[str] = None
        self._selected_roi_field: Optional[str] = None
        self._sample_pdf_path: Optional[Path] = None
        self._sample_folder_relpath: Optional[Path] = None
        self._existing_sample_relpath: Optional[str] = self._template.get("sample_file_relpath")
        self._existing_sample_name: Optional[str] = self._template.get("sample_file_name")
        self._existing_sample_sha: Optional[str] = self._template.get("sample_file_sha256")
        self._page_cache: Dict[int, QPixmap] = {}
        self._page_count = 0
        self._current_page = 1

        self.setWindowTitle("Editor šablony účtenky")
        self.resize(1400, 900)

        self._build_ui()
        self._load_template()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        body = QHBoxLayout()
        root.addLayout(body, 1)

        left = QWidget(self)
        left.setMinimumWidth(380)
        left_layout = QVBoxLayout(left)
        body.addWidget(left, 0)

        self.ed_name = QLineEdit(self._template.get("name") or "")
        self.chk_enabled = QCheckBox("Aktivní")
        self.chk_enabled.setChecked(bool(self._template.get("enabled", True)))

        sample_row = QHBoxLayout()
        self.btn_choose_pdf = QPushButton("Vybrat PDF")
        self.btn_choose_pdf.clicked.connect(self._choose_pdf)
        self.btn_open_pdf = QPushButton("Otevřít")
        self.btn_open_pdf.clicked.connect(self._open_sample_file)
        self.lbl_sample = QLabel(self._existing_sample_name or "Žádný soubor")
        self.lbl_sample.setWordWrap(True)
        sample_row.addWidget(self.btn_choose_pdf)
        sample_row.addWidget(self.btn_open_pdf)

        top_form = QFormLayout()
        top_form.addRow("Název", self.ed_name)
        top_form.addRow(self.chk_enabled)
        top_form.addRow("Vzorový PDF", self.lbl_sample)
        left_layout.addLayout(top_form)
        left_layout.addLayout(sample_row)

        left_layout.addWidget(QLabel("Pole"))
        self.field_list = QListWidget(self)
        self.field_list.setSelectionMode(QAbstractItemView.SingleSelection)
        for key, label, required in FIELD_ORDER:
            txt = f"{label} ({key})"
            if required:
                txt += " *"
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, key)
            self.field_list.addItem(it)
        self.field_list.currentItemChanged.connect(self._on_field_changed)
        left_layout.addWidget(self.field_list, 1)

        self.lbl_draw_hint = QLabel("Vyberte pole a potom tahem myši vyznačte ROI v PDF.")
        self.lbl_draw_hint.setWordWrap(True)
        left_layout.addWidget(self.lbl_draw_hint)

        row_actions = QHBoxLayout()
        self.btn_delete_selected_roi = QPushButton("Smazat vybranou oblast")
        self.btn_delete_selected_roi.clicked.connect(self._delete_selected_roi)
        self.btn_reassign_selected_roi = QPushButton("Přiřadit vybranou oblast k poli")
        self.btn_reassign_selected_roi.clicked.connect(self._reassign_selected_roi)
        row_actions.addWidget(self.btn_delete_selected_roi)
        row_actions.addWidget(self.btn_reassign_selected_roi)
        left_layout.addLayout(row_actions)

        left_layout.addWidget(QLabel("Označené oblasti"))
        self.roi_table = QTableWidget(0, 5, self)
        self.roi_table.setHorizontalHeaderLabels(["Pole", "Strana", "Box", "Vybrat", "Smazat"])
        self.roi_table.verticalHeader().setVisible(False)
        self.roi_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        left_layout.addWidget(self.roi_table, 2)
        matching_box = QFrame(self)
        matching_box.setFrameShape(QFrame.StyledPanel)
        matching_layout = QVBoxLayout(matching_box)
        matching_layout.addWidget(QLabel("Rozpoznání šablony"))

        self.chk_use_match_ico = QCheckBox("Použít IČO pro rozpoznání")
        self.chk_use_match_ico.setChecked(True)
        matching_layout.addWidget(self.chk_use_match_ico)

        ico_validator = QRegularExpressionValidator(QRegularExpression(r"^\d*$"), self)
        self.ed_match_ico = QLineEdit(self._template.get("match_supplier_ico_norm") or "")
        self.ed_match_ico.setValidator(ico_validator)
        self.ed_match_ico.editingFinished.connect(lambda: self.ed_match_ico.setText(_normalize_ico_digits(self.ed_match_ico.text())))
        matching_layout.addWidget(QLabel("IČO pro matching"))
        matching_layout.addWidget(self.ed_match_ico)

        self.btn_auto_fill_ico = QPushButton("Zkusit načíst IČO z ROI")
        self.btn_auto_fill_ico.clicked.connect(self._auto_fill_match_ico)
        matching_layout.addWidget(self.btn_auto_fill_ico)

        self.chk_use_anchor = QCheckBox("Použít anchor text pro rozpoznání")
        self.chk_use_anchor.setChecked(False)
        matching_layout.addWidget(self.chk_use_anchor)

        self.ed_anchor_tokens = QTextEdit(self)
        self.ed_anchor_tokens.setPlaceholderText("1 token na řádek")
        matching_layout.addWidget(self.ed_anchor_tokens)

        left_layout.addWidget(matching_box)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        body.addWidget(right, 1)

        toolbar = QHBoxLayout()
        self.btn_prev_page = QPushButton("<")
        self.btn_next_page = QPushButton(">")
        self.lbl_page = QLabel("Strana 0 / 0")
        self.btn_zoom_out = QPushButton("-")
        self.btn_zoom_in = QPushButton("+")
        self.btn_fit_width = QPushButton("Na šířku")
        self.btn_prev_page.clicked.connect(self._go_prev_page)
        self.btn_next_page.clicked.connect(self._go_next_page)
        self.btn_zoom_out.clicked.connect(lambda: self.canvas.scale(0.85, 0.85))
        self.btn_zoom_in.clicked.connect(lambda: self.canvas.scale(1.2, 1.2))
        for w in (self.btn_prev_page, self.btn_next_page, self.lbl_page, self.btn_zoom_out, self.btn_zoom_in, self.btn_fit_width):
            toolbar.addWidget(w)
        toolbar.addStretch(1)
        right_layout.addLayout(toolbar)

        self.canvas = PdfTemplateCanvas(self)
        self.canvas.roi_drawn.connect(self._on_roi_drawn)
        self.canvas.roi_selected.connect(self._on_roi_selected)
        self.canvas.roi_cleared.connect(self._on_roi_cleared)
        self.btn_fit_width.clicked.connect(self.canvas.fit_to_width)
        right_layout.addWidget(self.canvas, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _load_template(self) -> None:
        match_texts = self._template.get("match_texts_json")
        if match_texts:
            try:
                parsed = json.loads(match_texts)
                if isinstance(parsed, list):
                    tokens = [str(x).strip() for x in parsed if str(x).strip()]
                    self.ed_anchor_tokens.setPlainText("\n".join(tokens))
                    self.chk_use_anchor.setChecked(bool(tokens))
            except Exception:
                pass

        sample_rel = self._template.get("sample_file_relpath")
        if sample_rel:
            try:
                rel_path = Path(sample_rel)
                if rel_path.parent:
                    self._sample_folder_relpath = rel_path.parent
                full = self.paths.data_dir / rel_path
                if full.exists() and full.suffix.lower() == ".pdf":
                    self._sample_pdf_path = full
            except Exception:
                self._sample_folder_relpath = None

        schema_text = str(self._template.get("schema_json") or "").strip()
        if schema_text:
            try:
                parsed = parse_template_schema_text(schema_text)
                for key, field in parsed.fields.items():
                    self._roi_by_field[key] = RoiRecord(field=key, page=int(field.page), box=tuple(field.box))
            except Exception:
                pass

        if self._sample_pdf_path and self._sample_pdf_path.exists():
            self._load_pdf(self._sample_pdf_path)

        if self.field_list.count() > 0:
            self.field_list.setCurrentRow(0)

        self._refresh_roi_table()

    def _choose_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Vybrat vzorové PDF", str(Path.home()), "PDF Files (*.pdf)")
        if not path:
            return
        selected = Path(path)
        if selected.suffix.lower() != ".pdf":
            QMessageBox.warning(self, "Šablona", "Vzorový soubor musí být PDF.")
            return
        self._sample_pdf_path = selected
        self.lbl_sample.setText(selected.name)
        self._load_pdf(selected)

    def _load_pdf(self, path: Path) -> None:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            self._page_count = len(reader.pages)
        except Exception as exc:
            QMessageBox.warning(self, "PDF", f"Nepodařilo se načíst PDF: {exc}")
            self._page_count = 0
            return

        self._page_cache.clear()
        self._current_page = 1
        self._render_current_page()

    def _render_current_page(self) -> None:
        if not self._sample_pdf_path or self._page_count <= 0:
            self.canvas.clear()
            self.lbl_page.setText("Strana 0 / 0")
            return
        self._current_page = max(1, min(self._current_page, self._page_count))
        px = self._page_cache.get(self._current_page)
        if px is None:
            try:
                images = render_pdf_to_images(
                    self._sample_pdf_path,
                    dpi=220,
                    start_page=self._current_page - 1,
                    max_pages=1,
                )
                if not images:
                    raise RuntimeError("Render nevrátil stránku.")
                px = _pil_to_qpixmap(images[0])
                self._page_cache[self._current_page] = px
            except Exception as exc:
                QMessageBox.warning(self, "PDF", f"Nepodařilo se vyrenderovat stránku: {exc}")
                return
        self.canvas.set_page(self._current_page, px, self._roi_by_field)
        self.lbl_page.setText(f"Strana {self._current_page} / {self._page_count}")

    def _go_prev_page(self) -> None:
        if self._page_count <= 0:
            return
        self._current_page = max(1, self._current_page - 1)
        self._render_current_page()

    def _go_next_page(self) -> None:
        if self._page_count <= 0:
            return
        self._current_page = min(self._page_count, self._current_page + 1)
        self._render_current_page()

    def _open_sample_file(self) -> None:
        target = self._sample_pdf_path
        if target and target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
            return
        if self._existing_sample_relpath:
            t = self.paths.data_dir / self._existing_sample_relpath
            if t.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(t)))

    def _on_field_changed(self, current: Optional[QListWidgetItem], _: Optional[QListWidgetItem]) -> None:
        if current is None:
            self._selected_field = None
            self.canvas.set_active_field(None)
            self.lbl_draw_hint.setText("Vyberte pole a potom tahem myši vyznačte ROI v PDF.")
            return
        field = str(current.data(Qt.UserRole) or "")
        self._selected_field = field
        label = FIELD_LABELS.get(field, field)
        self.canvas.set_active_field(field)
        self.lbl_draw_hint.setText(f"Označ oblast pro: {label}")

    def _on_roi_drawn(self, field: str, page: int, box: tuple) -> None:
        if field in self._roi_by_field:
            reply = QMessageBox.question(self, "ROI", f"Pole {FIELD_LABELS.get(field, field)} už má oblast. Nahradit?")
            if reply != QMessageBox.Yes:
                return
        try:
            _ = normalized_box_to_pixel_box(box, 1000, 1000)
        except TemplateSchemaError as exc:
            QMessageBox.warning(self, "ROI", str(exc))
            return
        self._roi_by_field[field] = RoiRecord(field=field, page=int(page), box=tuple(float(x) for x in box))
        self._render_current_page()
        self.canvas.select_roi(field)
        self._selected_roi_field = field
        self._refresh_roi_table()

    def _on_roi_selected(self, field: str) -> None:
        self._selected_roi_field = field
        self._select_field_in_palette(field)

    def _on_roi_cleared(self) -> None:
        self._selected_roi_field = None

    def _select_field_in_palette(self, field: str) -> None:
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            if str(item.data(Qt.UserRole)) == field:
                self.field_list.setCurrentRow(i)
                return

    def _refresh_roi_table(self) -> None:
        keys = [key for key, _, _ in FIELD_ORDER if key in self._roi_by_field]
        for key in sorted(self._roi_by_field.keys()):
            if key not in keys:
                keys.append(key)

        self.roi_table.setRowCount(len(keys))
        for row, field in enumerate(keys):
            roi = self._roi_by_field[field]
            self.roi_table.setItem(row, 0, QTableWidgetItem(FIELD_LABELS.get(field, field)))
            self.roi_table.setItem(row, 1, QTableWidgetItem(str(roi.page)))
            box_txt = f"[{roi.box[0]:.3f}, {roi.box[1]:.3f}, {roi.box[2]:.3f}, {roi.box[3]:.3f}]"
            self.roi_table.setItem(row, 2, QTableWidgetItem(box_txt))

            btn_select = QPushButton("Vybrat")
            btn_select.clicked.connect(lambda _=False, f=field: self._focus_roi(f))
            self.roi_table.setCellWidget(row, 3, btn_select)

            btn_delete = QPushButton("Smazat")
            btn_delete.clicked.connect(lambda _=False, f=field: self._delete_roi_by_field(f))
            self.roi_table.setCellWidget(row, 4, btn_delete)

        self.roi_table.resizeColumnsToContents()

    def _focus_roi(self, field: str) -> None:
        roi = self._roi_by_field.get(field)
        if not roi:
            return
        self._current_page = int(roi.page)
        self._render_current_page()
        self.canvas.select_roi(field)
        self._selected_roi_field = field
        self._select_field_in_palette(field)

    def _delete_roi_by_field(self, field: str) -> None:
        if field in self._roi_by_field:
            del self._roi_by_field[field]
            if self._selected_roi_field == field:
                self._selected_roi_field = None
            self._render_current_page()
            self._refresh_roi_table()

    def _delete_selected_roi(self) -> None:
        if not self._selected_roi_field:
            QMessageBox.information(self, "ROI", "Nejprve vyberte oblast v náhledu nebo v seznamu.")
            return
        self._delete_roi_by_field(self._selected_roi_field)

    def _reassign_selected_roi(self) -> None:
        if not self._selected_roi_field:
            QMessageBox.information(self, "ROI", "Nejprve vyberte oblast k přeřazení.")
            return
        if not self._selected_field:
            QMessageBox.information(self, "ROI", "Vyberte cílové pole v levém seznamu.")
            return
        src = self._selected_roi_field
        dst = self._selected_field
        if src == dst:
            return
        src_roi = self._roi_by_field.get(src)
        if not src_roi:
            return

        if dst in self._roi_by_field:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Question)
            msg.setWindowTitle("Konflikt polí")
            msg.setText(f"Pole {FIELD_LABELS.get(dst, dst)} už má oblast.")
            btn_replace = msg.addButton("Nahradit", QMessageBox.AcceptRole)
            btn_swap = msg.addButton("Prohodit", QMessageBox.ActionRole)
            msg.addButton("Zrušit", QMessageBox.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == btn_replace:
                self._roi_by_field[dst] = RoiRecord(field=dst, page=src_roi.page, box=src_roi.box)
                del self._roi_by_field[src]
            elif clicked == btn_swap:
                dst_roi = self._roi_by_field[dst]
                self._roi_by_field[dst] = RoiRecord(field=dst, page=src_roi.page, box=src_roi.box)
                self._roi_by_field[src] = RoiRecord(field=src, page=dst_roi.page, box=dst_roi.box)
            else:
                return
        else:
            self._roi_by_field[dst] = RoiRecord(field=dst, page=src_roi.page, box=src_roi.box)
            del self._roi_by_field[src]

        self._selected_roi_field = dst
        self._render_current_page()
        self._refresh_roi_table()
        self._focus_roi(dst)

    def _auto_fill_match_ico(self) -> None:
        val = self._extract_ico_from_roi_supplier()
        if not val:
            QMessageBox.information(self, "IČO", "Nepodařilo se automaticky načíst IČO. Zadejte ho ručně.")
            return
        self.ed_match_ico.setText(val)

    def _extract_ico_from_roi_supplier(self) -> str:
        roi = self._roi_by_field.get("supplier_ico")
        if not roi or not self._sample_pdf_path:
            return ""
        try:
            images = render_pdf_to_images(
                self._sample_pdf_path,
                dpi=300,
                start_page=max(0, int(roi.page) - 1),
                max_pages=1,
            )
            if not images:
                return ""
            img = images[0]
            x0, y0, x1, y1 = normalized_box_to_pixel_box(roi.box, img.size[0], img.size[1])
            crop = img.crop((x0, y0, x1, y1))
            text = self._ocr_text_for_matching(crop)
            return _normalize_ico_digits(text)
        except Exception:
            return ""

    def _ocr_text_for_matching(self, image) -> str:
        try:
            from kajovospend.ocr.rapidocr_engine import RapidOcrEngine

            ocr = RapidOcrEngine(self.paths.models_dir)
            if ocr.is_available():
                txt, _ = ocr.image_to_text(image)
                if txt and txt.strip():
                    return txt
        except Exception:
            pass

        try:
            from kajovospend.ocr.handwriting_tesseract import TesseractHandwritingEngine

            ocr2 = TesseractHandwritingEngine(lang="ces", psm=7, oem=1)
            if ocr2.is_available():
                txt2, _ = ocr2.image_to_text(image)
                if txt2 and txt2.strip():
                    return txt2
        except Exception:
            pass
        return ""

    def _prepare_sample_info(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if self._sample_pdf_path and self._sample_pdf_path.exists():
            folder = self._sample_folder_relpath or Path("templates") / uuid.uuid4().hex
            dest_dir = self.paths.data_dir / folder
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / self._sample_pdf_path.name
            shutil.copy2(self._sample_pdf_path, dest_path)
            self._sample_folder_relpath = folder
            relpath = str(dest_path.relative_to(self.paths.data_dir))
            self._existing_sample_relpath = relpath
            self._existing_sample_name = self._sample_pdf_path.name
            self._existing_sample_sha = sha256_file(dest_path)
        return (self._existing_sample_name, self._existing_sample_sha, self._existing_sample_relpath)

    def _build_schema_text(self) -> str:
        fields = {
            field: TemplateField(name=field, page=int(roi.page), box=tuple(roi.box))
            for field, roi in self._roi_by_field.items()
        }
        schema = TemplateSchema(version=1, fields=fields)
        return serialize_template_schema(schema)

    def _validate_before_save(self) -> Optional[str]:
        if not self.ed_name.text().strip():
            return "Název je povinný."
        if not self._sample_pdf_path or not self._sample_pdf_path.exists() or self._sample_pdf_path.suffix.lower() != ".pdf":
            return "Je nutné zvolit vzorový PDF soubor."
        missing = sorted(REQUIRED_FIELDS - set(self._roi_by_field.keys()))
        if missing:
            names = ", ".join(FIELD_LABELS.get(x, x) for x in missing)
            return f"Chybí povinné oblasti: {names}."
        for field, roi in self._roi_by_field.items():
            try:
                _ = normalized_box_to_pixel_box(roi.box, 1000, 1000)
            except TemplateSchemaError as exc:
                return f"Pole {FIELD_LABELS.get(field, field)}: {exc}"
        return None

    def accept(self) -> None:
        err = self._validate_before_save()
        if err:
            QMessageBox.warning(self, "Šablona", err)
            return

        if self.chk_use_match_ico.isChecked():
            ico = _normalize_ico_digits(self.ed_match_ico.text())
            if not ico:
                ico = self._extract_ico_from_roi_supplier()
                if ico:
                    self.ed_match_ico.setText(ico)
            if not ico:
                QMessageBox.warning(self, "Šablona", "IČO pro matching je zapnuté, ale IČO není vyplněné.")
                return

        super().accept()

    @property
    def payload(self) -> Dict[str, Any]:
        sample_name, sample_sha, sample_rel = self._prepare_sample_info()
        schema_json = self._build_schema_text()

        match_ico: Optional[str]
        if self.chk_use_match_ico.isChecked():
            match_ico = _normalize_ico_digits(self.ed_match_ico.text()) or None
        else:
            match_ico = None

        if self.chk_use_anchor.isChecked():
            tokens = [line.strip() for line in self.ed_anchor_tokens.toPlainText().splitlines() if line.strip()]
            match_texts_json = json.dumps(tokens, ensure_ascii=False) if tokens else None
        else:
            match_texts_json = None

        return {
            "name": self.ed_name.text().strip(),
            "enabled": bool(self.chk_enabled.isChecked()),
            "match_supplier_ico_norm": match_ico,
            "match_texts_json": match_texts_json,
            "schema_json": schema_json,
            "sample_file_name": sample_name,
            "sample_file_sha256": sample_sha,
            "sample_file_relpath": sample_rel,
        }
