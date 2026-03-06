from __future__ import annotations

import os
import sys
import tempfile
import traceback
from multiprocessing import freeze_support
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    # Prefer local sources before any installed package elsewhere in system.
    sys.path.insert(0, str(SRC_DIR))


def _prepare_qtwebengine_dirs() -> None:
    """
    Force QtWebEngine to use writable per-user paths.
    Prevents Chromium cache errors on Windows when default cache location
    is locked or not writable.
    """
    local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    candidates = []
    if local_app_data:
        candidates.append(Path(local_app_data) / "KajovoSpend" / "qtwebengine")
    candidates.append(ROOT_DIR / ".qtwebengine")
    candidates.append(Path(tempfile.gettempdir()) / "KajovoSpend" / "qtwebengine")

    cache_dir = None
    user_data_dir = None
    for base in candidates:
        try:
            maybe_cache = base / "cache"
            maybe_user_data = base / "user_data"
            maybe_cache.mkdir(parents=True, exist_ok=True)
            maybe_user_data.mkdir(parents=True, exist_ok=True)
            cache_dir = maybe_cache
            user_data_dir = maybe_user_data
            break
        except OSError:
            continue

    if cache_dir is None or user_data_dir is None:
        return

    # Must be set before QApplication/QtWebEngine is initialized.
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if "--disk-cache-dir=" not in flags:
        flags = f"{flags} --disk-cache-dir={cache_dir}".strip()
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags
    os.environ["QTWEBENGINE_USER_DATA_DIR"] = str(user_data_dir)


_prepare_qtwebengine_dirs()

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from kajovospend.ui.main_window import MainWindow
from kajovospend.utils.env import load_user_env_var, sanitize_openai_api_key


def _install_excepthook() -> None:
    """
    Global crash-guard for unhandled exceptions.
    Shows a dialog (when possible) and prints traceback to stderr.
    """

    def _excepthook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass
        try:
            # Avoid insanely long dialogs; show tail.
            tail = msg[-6000:] if len(msg) > 6000 else msg
            QMessageBox.critical(None, "KájovoSpend – neočekávaná chyba", tail)
        except Exception:
            pass

    sys.excepthook = _excepthook


def main() -> int:
    root = ROOT_DIR
    # načti klíč přímo z registru (uživatelské proměnné). Procesy spuštěné ze stejného PowerShellu
    # nemusí mít aktualizované prostředí, proto nečteme jen os.getenv.
    val = sanitize_openai_api_key(load_user_env_var("KAJOVOSPEND_OPENAI_API_KEY"))
    if val:
        os.environ["KAJOVOSPEND_OPENAI_API_KEY"] = val

    app = QApplication(sys.argv)
    _install_excepthook()
    icon_candidates = [root / "assets" / "app.icns", root / "assets" / "app.ico"]
    for icon_path in icon_candidates:
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))
            break

    w = MainWindow(config_path=root / "config.yaml", assets_dir=root / "assets")
    w.showMaximized()
    return app.exec()


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
