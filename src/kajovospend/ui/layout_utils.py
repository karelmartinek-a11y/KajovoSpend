from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QLayout,
    QSizePolicy,
    QStyle,
    QWidget,
    QWidgetItem,
)


class FlowLayout(QLayout):
    """Jednoduchý zalamovací layout pro husté toolbary a akční bloky."""

    def __init__(self, parent: QWidget | None = None, *, margin: int = 0, h_spacing: int = 8, v_spacing: int = 8):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._h_spacing = int(h_spacing)
        self._v_spacing = int(v_spacing)
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, max(0, width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisible():
                continue

            hint = item.sizeHint()
            next_x = x + hint.width()
            if line_height > 0 and next_x > effective.right() + 1:
                x = effective.x()
                y += line_height + self._v_spacing
                next_x = x + hint.width()
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x = next_x + self._h_spacing
            line_height = max(line_height, hint.height())

        used = y + line_height - rect.y() + margins.bottom()
        return max(0, used)


def set_button_min_widths(*widgets: QWidget, extra: int = 28) -> None:
    for widget in widgets:
        if widget is None:
            continue
        try:
            text = widget.text()
        except Exception:
            continue
        metric = widget.fontMetrics()
        width = metric.horizontalAdvance(text or "") + extra
        widget.setMinimumWidth(max(widget.minimumWidth(), width))
        widget.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)


def set_editor_char_width(widget: QWidget, chars: int, *, floor: int = 0) -> None:
    metric = widget.fontMetrics()
    width = metric.horizontalAdvance("0" * max(1, int(chars))) + 28
    widget.setMinimumWidth(max(widget.minimumWidth(), floor, width))
    widget.setProperty("audit_expected_chars", int(chars))


def tune_form_layout(form: QFormLayout, *, label_width: int = 190, spacing: int = 10) -> None:
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setFormAlignment(Qt.AlignTop | Qt.AlignLeft)
    form.setHorizontalSpacing(spacing)
    form.setVerticalSpacing(spacing)
    for row in range(form.rowCount()):
        label_item = form.itemAt(row, QFormLayout.LabelRole)
        if label_item is None:
            continue
        label_widget = label_item.widget()
        if label_widget is None:
            continue
        label_widget.setMinimumWidth(label_width)
        label_widget.setWordWrap(True)


def style_as_panel(widget: QWidget) -> None:
    widget.setProperty("panel", True)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def default_spacing(widget: QWidget) -> int:
    style = widget.style() if widget is not None else None
    if style is None:
        return 8
    return style.pixelMetric(QStyle.PM_LayoutHorizontalSpacing, None, widget) or 8
