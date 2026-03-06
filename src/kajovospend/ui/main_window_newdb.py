"""
Helper for initializing a new SQLite DB at a user-selected location.
"""
from __future__ import annotations

from pathlib import Path
from PySide6.QtWidgets import QFileDialog, QMessageBox
from kajovospend.db.migrate import init_working_db, init_production_db
from kajovospend.db.session import make_session_factory
from kajovospend.db.working_session import create_working_engine
from kajovospend.db.production_session import create_production_engine
from kajovospend.db.dual_db_guard import ensure_separate_databases
from kajovospend.utils.paths import resolve_app_paths
from kajovospend.service.processor import Processor


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

    # derive production DB path next to working db (compat derivation identical to resolve_app_paths)
    production_path = path.with_name(path.stem + "-production" + path.suffix)
    ensure_separate_databases(str(path), str(production_path))

    w_eng = create_working_engine(str(path))
    p_eng = create_production_engine(str(production_path))
    init_working_db(w_eng)
    init_production_db(p_eng)
    sf = make_session_factory(w_eng)
    sf_production = make_session_factory(p_eng)

    # update config + state
    window.ed_db_dir.setText(str(path.parent))
    window.cfg.setdefault("app", {})
    window.cfg["app"]["data_dir"] = str(path.parent)
    window.cfg["app"]["db_path"] = str(path)  # legacy key
    window.cfg["app"]["working_db_path"] = str(path)
    window.cfg["app"]["production_db_path"] = str(production_path)
    try:
        window.paths = resolve_app_paths(
            window.cfg["app"].get("data_dir"),
            window.cfg["app"].get("db_path"),
            window.cfg["app"].get("log_dir"),
            window.cfg.get("ocr", {}).get("models_dir"),
            working_db=window.cfg["app"].get("working_db_path"),
            production_db=window.cfg["app"].get("production_db_path"),
        )
    except Exception:
        pass
    window.engine = w_eng
    window.engine_production = p_eng
    window.sf = sf
    window.sf_production = sf_production
    try:
        window.processor = Processor(window.cfg, window.paths, window.log, window.sf, window.sf_production)
    except Exception:
        pass
    QMessageBox.information(
        window,
        "Databáze",
        f"Vytvořena working DB: {path}\nVytvořena production DB: {production_path}",
    )
