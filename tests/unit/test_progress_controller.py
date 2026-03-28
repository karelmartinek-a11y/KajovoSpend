from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

from tests.gui.smoke_support import create_app, ensure_repo_on_syspath

ensure_repo_on_syspath()

from kajovospend.ui.progress import MiniProgressWidget, ProgressController


class _HostWindow(QWidget):
    pass


def _make_controller() -> ProgressController:
    app = create_app()
    host = _HostWindow()
    mini = MiniProgressWidget(host)
    andon = QLabel(host)
    ctrl = ProgressController(host, mini, andon)
    # Keep references alive for the duration of the test.
    host._mini = mini  # type: ignore[attr-defined]
    host._andon = andon  # type: ignore[attr-defined]
    host._controller = ctrl  # type: ignore[attr-defined]
    app.processEvents()
    return ctrl


def test_progress_history_keeps_last_10_steps() -> None:
    app = create_app()
    ctrl = _make_controller()

    assert ctrl.start(title="IMPORT", step="Startuji import…", total=12, batch_total=12)
    for idx in range(1, 13):
        ctrl.update(step=f"Krok {idx}")
    app.processEvents()

    assert ctrl.dlg is not None
    lines = [line.strip() for line in ctrl.dlg.history_box.toPlainText().splitlines() if line.strip()]
    assert len(lines) == 10
    assert any("Krok 12" in line for line in lines)
    assert all("Startuji import" not in line for line in lines)
    assert all("Krok 2" not in line for line in lines)

    ctrl.finish("Hotovo.")
    app.processEvents()


def test_mark_batch_done_treats_processed_as_success() -> None:
    app = create_app()
    ctrl = _make_controller()

    assert ctrl.start(title="IMPORT", step="Startuji import…", total=1, batch_total=1)
    ctrl.mark_batch_done("PROCESSED")
    app.processEvents()

    assert ctrl.batch.done == 1
    assert ctrl.batch.production == 1
    assert ctrl.batch.error == 0

    ctrl.finish("Hotovo.")
    app.processEvents()
