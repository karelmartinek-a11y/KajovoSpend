from __future__ import annotations

from pathlib import Path
from typing import Dict

from sqlalchemy.orm import Session
from sqlalchemy import text

from kajovospend.db.dual_db_guard import ensure_separate_databases, DualDbConfigError, canonicalize


def canonical_db_path(session: Session) -> Path:
    """Return canonicalized sqlite path for a SQLAlchemy session/engine."""
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    if url is None or url.database is None:
        raise ValueError("Session is not bound to a SQLite database")
    return canonicalize(str(url.database))


def assert_separate_sessions(working_session: Session, production_session: Session) -> None:
    """Raise DualDbConfigError if the two sessions point to the same physical DB."""
    ensure_separate_databases(
        str(canonical_db_path(working_session)),
        str(canonical_db_path(production_session)),
    )


def snapshot_counts(working_session: Session, production_session: Session) -> Dict[str, int]:
    """
    Lightweight forensic snapshot for tests: returns document counts in working vs production.
    Does not mutate data; raises if DBs are not physically separated.
    """
    assert_separate_sessions(working_session, production_session)
    w_cnt = int(working_session.execute(text("SELECT COUNT(*) FROM documents")).scalar_one())
    p_cnt = int(production_session.execute(text("SELECT COUNT(*) FROM documents")).scalar_one())
    return {"working_documents": w_cnt, "production_documents": p_cnt}
