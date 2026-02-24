from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon

from kajovospend.utils.env import load_user_env_var, sanitize_openai_api_key

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    # Prefer lokální zdrojáky před případnou instalací balíčku jinde v systému.
    sys.path.insert(0, str(SRC_DIR))

from kajovospend.ui.main_window import MainWindow


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
    icon_path = root / "assets" / "app.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    w = MainWindow(config_path=root / "config.yaml", assets_dir=root / "assets")
    w.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
