"""
Helper for initializing a new SQLite DB at a user-selected location.
"""
from __future__ import annotations

from pathlib import Path
from PySide6.QtWidgets import QFileDialog, QMessageBox
from kajovospend.db.migrate import init_db
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.utils.paths import resolve_app_paths


def init_new_db(window) -> None:
    """Ask for path, create empty DB, rewire engine/session in the main window."""
    try:
        default_dir = window.ed_db_dir.text().strip() or str(window.paths.data_dir)
    except Exception:
        default_dir = ""
    fname, _ = QFileDialog.getSaveFileName(
        window,
        "Vyberte soubor nové databáze",
        str(Path(default_dir) / "kajovospend.sqlite"),
        "SQLite (*.sqlite *.db);;All files (*)",
    )
    if not fname:
        return
    path = Path(fname)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        QMessageBox.critical(window, "Databáze", f"Nelze vytvořit adresář: {exc}")
        return

    eng = make_engine(str(path))
    init_db(eng)
    sf = make_session_factory(eng)

    # update config + state
    window.ed_db_dir.setText(str(path.parent))
    window.cfg.setdefault("app", {})
    window.cfg["app"]["data_dir"] = str(path.parent)
    window.cfg["app"]["db_path"] = str(path)
    try:
        window.paths = resolve_app_paths(
            window.cfg["app"].get("data_dir"),
            window.cfg["app"].get("db_path"),
            window.cfg["app"].get("log_dir"),
            window.cfg.get("ocr", {}).get("models_dir"),
        )
    except Exception:
        pass
    window.engine = eng
    window.sf = sf
    QMessageBox.information(window, "Databáze", f"Nová databáze vytvořena: {path}")
