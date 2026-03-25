from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def _fmt_mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    mm = s // 60
    ss = s % 60
    return f"{mm:02d}:{ss:02d}"


@dataclass
class BatchSummary:
    total: int = 0
    done: int = 0
    production: int = 0
    quarantine: int = 0
    duplicate: int = 0
    error: int = 0

    def as_text(self) -> str:
        if self.total <= 0:
            return ""
        return (
            f"Cyklus {self.done}/{self.total} | "
            f"PROD: {self.production}  KAR: {self.quarantine}  DUP: {self.duplicate}  CHYBA: {self.error}"
        )


class ProgressDialog(QDialog):
    minimized = Signal()
    restored = Signal()
    canceled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowModality(Qt.NonModal)
        self.setModal(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setMinimumSize(640, 280)

        self._t0 = time.monotonic()
        self._last_heartbeat = 0
        # By default, user cannot close the dialog with X; we treat it as a cancel request.
        # When the operation finishes, ProgressController enables closing.
        self._allow_close = False

        self.lbl_title = QLabel("Pracuji…")
        self.lbl_title.setObjectName("ProgressTitle")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_step = QLabel("")
        self.lbl_step.setWordWrap(True)
        self.lbl_step.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_time = QLabel("00:00")
        self.lbl_time.setObjectName("ProgressTime")
        self.lbl_time.setMinimumWidth(92)
        self.lbl_time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_heartbeat = QLabel("●")
        self.lbl_heartbeat.setObjectName("ProgressHeartbeat")
        self.lbl_heartbeat.setToolTip("Indikace živosti")
        self.lbl_heartbeat.setMinimumWidth(18)
        self.lbl_heartbeat.setAlignment(Qt.AlignCenter)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setValue(0)
        self.bar.setMinimumHeight(28)

        self.batch_box = QLabel("")
        self.batch_box.setObjectName("ProgressBatch")
        self.batch_box.setWordWrap(True)
        self.batch_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.batch_box.hide()

        self.btn_min = QPushButton("Minimalizovat")
        self.btn_min.clicked.connect(self._on_minimize)
        self.btn_min.setMinimumWidth(128)

        self.btn_cancel = QPushButton("Zastavit")
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_cancel.setMinimumWidth(128)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.lbl_title, 1)
        top.addWidget(self.lbl_heartbeat)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        mid.addWidget(self.lbl_time)
        mid.addStretch(1)
        mid.addWidget(self.btn_min)
        mid.addWidget(self.btn_cancel)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.lbl_step)
        lay.addWidget(self.bar)
        lay.addWidget(self.batch_box)
        lay.addLayout(mid)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_title(self, title: str) -> None:
        self.setWindowTitle(title)
        self.lbl_title.setText(title)

    def set_step(self, text: str) -> None:
        self.lbl_step.setText(text or "")

    def set_determinate(self, total: int) -> None:
        total_i = max(1, int(total))
        self.bar.setRange(0, total_i)

    def set_indeterminate(self) -> None:
        self.bar.setRange(0, 0)

    def set_value(self, value: int) -> None:
        try:
            self.bar.setValue(int(value))
        except Exception:
            pass

    def set_batch_text(self, text: str) -> None:
        if text:
            self.batch_box.setText(text)
            self.batch_box.show()
        else:
            self.batch_box.setText("")
            self.batch_box.hide()

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._t0
        eta = ""
        try:
            mn = self.bar.minimum()
            mx = self.bar.maximum()
            val = self.bar.value()
            if mx > mn and val > mn:
                frac = (val - mn) / float(mx - mn)
                if frac > 0.01:
                    total_est = elapsed / frac
                    remain = max(0.0, total_est - elapsed)
                    eta = f" | ETA {_fmt_mmss(remain)}"
        except Exception:
            eta = ""
        self.lbl_time.setText(f"{_fmt_mmss(elapsed)}{eta}")

        # heartbeat toggle
        self._last_heartbeat += 1
        self.lbl_heartbeat.setText("●" if (self._last_heartbeat % 2 == 0) else "○")

    def _on_cancel(self) -> None:
        # Request cancel; actual cancellation is handled by ProgressController.
        self.canceled.emit()

    def closeEvent(self, event):  # noqa: N802
        # Treat window close as cancel request while running.
        # When controller marks the operation as finished, allow normal close.
        if self._allow_close:
            event.accept()
            return
        try:
            self.canceled.emit()
        finally:
            event.ignore()

    def allow_close(self) -> None:
        self._allow_close = True

    def _on_minimize(self) -> None:
        self.hide()
        self.minimized.emit()

    def restore(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.restored.emit()


class MiniProgressWidget(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MiniProgress")
        self.setCursor(Qt.PointingHandCursor)
        self.lbl1 = QLabel("")
        self.lbl2 = QLabel("")
        self.lbl2.setObjectName("MiniProgress2")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(0)
        lay.addWidget(self.lbl1)
        lay.addWidget(self.lbl2)
        self.hide()

    def set_lines(self, a: str, b: str) -> None:
        self.lbl1.setText(a or "")
        self.lbl2.setText(b or "")

    def mousePressEvent(self, event):  # noqa: N802
        try:
            self.clicked.emit()
        finally:
            super().mousePressEvent(event)


class ProgressController:
    """Single-instance progress UI with optional batch summary."""

    def __init__(self, parent_window, mini_widget: MiniProgressWidget, openai_andon_label: QLabel):
        self.parent_window = parent_window
        self.mini = mini_widget
        self.andon = openai_andon_label
        self.dlg: Optional[ProgressDialog] = None
        self.active = False
        self.batch = BatchSummary()
        self._openai_on = False
        self._openai_off_timer: Optional[QTimer] = None
        self._cancel_cb = None

        self.mini.clicked.connect(self.restore)

    def can_start(self) -> bool:
        return not self.active

    def start(
        self,
        *,
        title: str,
        step: str,
        total: Optional[int] = None,
        batch_total: Optional[int] = None,
        cancel_cb=None,
    ) -> bool:
        if self.active:
            return False
        # Defensive cleanup for any stale dialog instance.
        if self.dlg is not None:
            try:
                self.dlg.allow_close()
                self.dlg.close()
            except Exception:
                pass
            self.dlg = None
        self.active = True
        self.batch = BatchSummary(total=int(batch_total or 0), done=0)
        self._cancel_cb = cancel_cb
        self.dlg = ProgressDialog(self.parent_window)
        self.dlg.set_title(title)
        self.dlg.set_step(step)
        if total is None:
            self.dlg.set_indeterminate()
        else:
            self.dlg.set_determinate(int(total))
            self.dlg.set_value(0)
        if self.batch.total > 0:
            self.dlg.set_batch_text(self.batch.as_text())

        self.dlg.minimized.connect(self._on_minimized)
        self.dlg.restored.connect(self._on_restored)
        self.dlg.canceled.connect(self.cancel)
        self.dlg.show()
        self._update_mini()
        return True

    def update(
        self,
        *,
        step: Optional[str] = None,
        value: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        if not self.active or not self.dlg:
            return
        if step is not None:
            self.dlg.set_step(step)
        if total is not None:
            self.dlg.set_determinate(int(total))
        if value is not None:
            self.dlg.set_value(int(value))
        if self.batch.total > 0:
            self.dlg.set_batch_text(self.batch.as_text())
        self._update_mini()

    def set_cancel_callback(self, cancel_cb) -> None:
        self._cancel_cb = cancel_cb

    def mark_batch_done(self, status: str) -> None:
        if self.batch.total <= 0:
            return
        self.batch.done += 1
        st = str(status or "").upper()
        if st == "PRODUCTION":
            self.batch.production += 1
        elif st == "QUARANTINE":
            self.batch.quarantine += 1
        elif st == "DUPLICATE":
            self.batch.duplicate += 1
        else:
            self.batch.error += 1
        self.update(step=None)

    def cancel(self) -> None:
        # Called from UI (cancel button or window close).
        cb = self._cancel_cb
        self.abort("Zastaveno.")
        try:
            if callable(cb):
                cb()
        except Exception:
            pass

    def abort(self, final_step: str = "Zastaveno.") -> None:
        if not self.active:
            return
        try:
            if self.dlg:
                self.dlg.set_step(final_step)
                self.dlg.allow_close()
                self.dlg.close()
        except Exception:
            pass
        self.active = False
        self._set_openai_andon(False)
        self.mini.hide()
        self.dlg = None
        self._cancel_cb = None

    def finish(self, final_step: str) -> None:
        if not self.active:
            return
        dlg = self.dlg
        try:
            if dlg:
                dlg.set_step(final_step)
                # Make determinate and full if possible
                try:
                    if dlg.bar.maximum() > dlg.bar.minimum():
                        dlg.set_value(dlg.bar.maximum())
                except Exception:
                    pass
                # Allow closing now; otherwise closeEvent would emit cancel and ignore.
                try:
                    dlg.allow_close()
                except Exception:
                    pass
                dlg.close()
        except Exception:
            pass
        self.active = False
        self._set_openai_andon(False)
        self.mini.hide()
        self.dlg = None
        self._cancel_cb = None

    def minimize(self) -> None:
        if self.dlg:
            self.dlg.hide()
            self._on_minimized()

    def restore(self) -> None:
        if self.dlg:
            self.dlg.restore()
        # Do not reset cancel callback here; cancellation should keep working
        # after minimize/restore.


    def _on_minimized(self) -> None:
        self._update_mini(force_show=True)

    def _on_restored(self) -> None:
        self.mini.hide()

    def _update_mini(self, force_show: bool = False) -> None:
        if not self.active or not self.dlg:
            self.mini.hide()
            return
        line1 = self.dlg.windowTitle() or "Pracuji…"
        line2 = self.dlg.lbl_step.text() or ""
        if self.batch.total > 0:
            line2 = self.batch.as_text()
        self.mini.set_lines(line1, line2)
        if force_show or (self.dlg and not self.dlg.isVisible()):
            self.mini.show()

    def set_openai_phase(self, phase_text: str, *, keep_on_ms: int = 1200) -> None:
        """Turn on andon briefly; extend if called repeatedly."""
        self._set_openai_andon(True)
        if self._openai_off_timer is None:
            self._openai_off_timer = QTimer(self.parent_window)
            self._openai_off_timer.setSingleShot(True)
            self._openai_off_timer.timeout.connect(lambda: self._set_openai_andon(False))
        self._openai_off_timer.start(max(200, int(keep_on_ms)))
        # If there is an active progress, mirror phase to mini tooltip
        try:
            self.andon.setToolTip(phase_text or "OpenAI API")
        except Exception:
            pass

    def _set_openai_andon(self, on: bool) -> None:
        self._openai_on = bool(on)
        try:
            self.andon.setProperty("on", self._openai_on)
            self.andon.style().unpolish(self.andon)
            self.andon.style().polish(self.andon)
            self.andon.update()
        except Exception:
            pass

