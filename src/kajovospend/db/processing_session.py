from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kajovospend.db.processing_models import BaseProcessing


def _processing_db_path(cfg) -> Path:
    try:
        app_cfg = cfg.get("app", {}) if isinstance(cfg, dict) else {}
        data_dir = app_cfg.get("data_dir")
        paths = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
        raw = paths.get("processing_db")
        if raw:
            return Path(raw)
        if data_dir:
            return Path(data_dir) / "kajovospend-processing.sqlite"
    except Exception:
        pass
    return Path("processing.db")


def create_processing_session_factory(cfg) -> Callable[[], sessionmaker]:
    db_path = _processing_db_path(cfg)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True)
    BaseProcessing.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    # Ulož engine, aby jej bylo možné explicitně uvolnit (Windows locky).
    sf._engine = engine  # type: ignore[attr-defined]
    return sf


def dispose_processing_session_factory(sf) -> None:
    """Best-effort uvolnění SQLite engine pro processing DB (Windows lock prevention)."""
    try:
        try:
            sf.close_all()
        except Exception:
            pass
        bind = getattr(sf, "_engine", None) or getattr(sf, "bind", None)
        if bind is not None:
            bind.dispose()
    except Exception:
        pass
