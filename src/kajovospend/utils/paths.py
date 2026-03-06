from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    working_db_path: Path
    production_db_path: Path
    log_dir: Path
    models_dir: Path
    # backward-compatible alias (legacy code may still read db_path)
    db_path: Path


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _derive_production_from_legacy(db_path: Path) -> Path:
    if db_path.suffix:
        return db_path.with_name(db_path.stem + "-production" + db_path.suffix)
    return db_path.with_name(db_path.name + "-production")


def resolve_app_paths(
    data_dir: Optional[str],
    db_path: Optional[str],
    log_dir: Optional[str],
    models_dir: Optional[str],
    working_db: Optional[str] = None,
    production_db: Optional[str] = None,
) -> AppPaths:
    dd = Path(data_dir) if data_dir else default_data_dir()
    # backward compatibility: if explicit working/production provided, honor them;
    # else fall back to legacy db_path as working and derive production alongside.
    if working_db or production_db:
        wdb = Path(working_db) if working_db else Path(db_path) if db_path else dd / "kajovospend-working.sqlite"
        pdb = Path(production_db) if production_db else _derive_production_from_legacy(wdb)
    else:
        legacy = Path(db_path) if db_path else dd / "kajovospend.sqlite"
        wdb = legacy
        pdb = _derive_production_from_legacy(legacy)
    ld = Path(log_dir) if log_dir else default_log_dir()
    md = Path(models_dir) if models_dir else dd / "models" / "rapidocr"
    ensure_dirs(dd, ld, md, wdb.parent, pdb.parent)
    return AppPaths(
        data_dir=dd,
        working_db_path=wdb,
        production_db_path=pdb,
        log_dir=ld,
        models_dir=md,
        db_path=wdb,
    )
