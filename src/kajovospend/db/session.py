from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def make_engine(db_path: str):
    # check_same_thread False to allow background threads
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
