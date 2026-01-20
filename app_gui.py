from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from kajovospend.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    root = Path(__file__).parent
    icon_path = root / "assets" / "app.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    w = MainWindow(config_path=root / "config.yaml", assets_dir=root / "assets")
    w.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
