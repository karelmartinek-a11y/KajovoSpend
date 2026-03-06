from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import sessionmaker

from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.db.working_models import BaseWorking


def create_working_engine(db_path: Path | str):
    return make_engine(str(db_path))


def create_working_session_factory(db_path: Path | str) -> Callable[[], sessionmaker]:
    eng = create_working_engine(db_path)
    BaseWorking.metadata.create_all(eng)
    return make_session_factory(eng)


def init_working_db(db_path: Path | str) -> None:
    eng = create_working_engine(db_path)
    BaseWorking.metadata.create_all(eng)
