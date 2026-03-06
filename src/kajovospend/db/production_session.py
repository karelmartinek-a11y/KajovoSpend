from __future__ import annotations

from pathlib import Path
from typing import Callable

from sqlalchemy.orm import sessionmaker

from kajovospend.db.production_models import BaseProduction
from kajovospend.db.session import make_engine, make_session_factory


def create_production_engine(db_path: Path | str):
    return make_engine(str(db_path))


def create_production_session_factory(db_path: Path | str) -> Callable[[], sessionmaker]:
    eng = create_production_engine(db_path)
    BaseProduction.metadata.create_all(eng)
    return make_session_factory(eng)


def init_production_db(db_path: Path | str) -> None:
    eng = create_production_engine(db_path)
    BaseProduction.metadata.create_all(eng)
