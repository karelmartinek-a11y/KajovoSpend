from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kajovospend.db.processing_models import BaseProcessing


def _processing_db_path(cfg) -> Path:
    try:
        paths = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
        raw = paths.get("processing_db")
        if raw:
            return Path(raw)
    except Exception:
        pass
    return Path("processing.db")


def create_processing_session_factory(cfg) -> Callable[[], sessionmaker]:
    db_path = _processing_db_path(cfg)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True)
    BaseProcessing.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
