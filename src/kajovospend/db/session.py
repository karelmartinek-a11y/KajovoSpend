from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(db_path: str):
    # SQLite tuned for large-ish local datasets (10k+ documents, 100k+ items).
    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        try:
            cur = dbapi_connection.cursor()
            # Safety + concurrency
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            # Performance
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA cache_size=-200000")  # ~200MB page cache (negative = KB)
            cur.execute("PRAGMA mmap_size=268435456")  # 256MB (best-effort)
            cur.execute("PRAGMA optimize")
            cur.close()
        except Exception:
            # Never crash the app due to PRAGMA failures (older SQLite builds, etc.)
            pass

    return eng


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
