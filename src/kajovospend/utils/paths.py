from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "KajovoSpend"


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def _repo_root() -> Path:
    # src/kajovospend/utils/paths.py -> repo root is three parents up
    return Path(__file__).resolve().parents[3]


def default_log_dir() -> Path:
    # Keep logs inside repo root/LOG by default.
    return _repo_root() / "LOG"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    db_path: Path
    log_dir: Path
    models_dir: Path


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def resolve_app_paths(data_dir: str | None, db_path: str | None, log_dir: str | None, models_dir: str | None) -> AppPaths:
    dd = Path(data_dir) if data_dir else default_data_dir()
    db = Path(db_path) if db_path else dd / "kajovospend.sqlite"
    ld = Path(log_dir) if log_dir else default_log_dir()
    md = Path(models_dir) if models_dir else dd / "models" / "rapidocr"
    ensure_dirs(dd, ld, md)
    return AppPaths(data_dir=dd, db_path=db, log_dir=ld, models_dir=md)
