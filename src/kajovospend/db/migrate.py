from __future__ import annotations

from sqlalchemy import text

from .base import Base


FTS_DOCS = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  document_id UNINDEXED,
  supplier_ico,
  doc_number,
  bank_account,
  text
);
"""

FTS_ITEMS = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  document_id UNINDEXED,
  item_name
);
"""


def _ensure_supplier_columns(conn) -> None:
    # Lightweight SQLite migration: add missing columns (no destructive changes).
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(suppliers)")).fetchall()}
    to_add: list[tuple[str, str]] = [
        ("legal_form", "TEXT"),
        ("street", "TEXT"),
        ("street_number", "TEXT"),
        ("orientation_number", "TEXT"),
        ("city", "TEXT"),
        ("zip_code", "TEXT"),
    ]
    for name, coltype in to_add:
        if name not in cols:
            conn.execute(text(f"ALTER TABLE suppliers ADD COLUMN {name} {coltype}"))


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _ensure_supplier_columns(conn)
        conn.execute(text(FTS_DOCS))
        conn.execute(text(FTS_ITEMS))
        # singleton row
        conn.execute(text("INSERT OR IGNORE INTO service_state (singleton, running, queue_size) VALUES (1, 0, 0)"))
