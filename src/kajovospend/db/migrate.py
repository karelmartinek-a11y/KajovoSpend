from __future__ import annotations

import re
from sqlalchemy import text
from sqlalchemy.engine import Engine

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

_ICO_DIGITS_RE = re.compile(r"\D+")


def _normalize_ico_soft(ico: str | None) -> str | None:
    if ico is None:
        return None
    raw = str(ico).strip()
    if not raw:
        return None
    digits = _ICO_DIGITS_RE.sub("", raw)
    if not digits:
        return None
    if len(digits) > 8:
        return digits
    return digits.zfill(8)


def _ensure_columns_and_indexes(engine: Engine) -> None:
    # Keep migrations deterministic and idempotent (no external tool).
    with engine.begin() as con:
        # Ensure FTS tables exist before indexing them.
        con.execute(text(FTS_DOCS))
        con.execute(text(FTS_ITEMS))

        # --- columns ---
        cols = con.execute(text("PRAGMA table_info('suppliers')")).fetchall()
        col_names = {row[1] for row in cols}  # (cid, name, type, notnull, dflt, pk)
        # historical columns that might be missing
        for name, coltype in [
            ("legal_form", "TEXT"),
            ("street", "TEXT"),
            ("street_number", "TEXT"),
            ("orientation_number", "TEXT"),
            ("city", "TEXT"),
            ("zip_code", "TEXT"),
        ]:
            if name not in col_names:
                con.execute(text(f"ALTER TABLE suppliers ADD COLUMN {name} {coltype}"))
        if "ico_norm" not in col_names:
            con.execute(text("ALTER TABLE suppliers ADD COLUMN ico_norm TEXT"))

        # Backfill ico_norm in Python (SQLite has no built-in regex replace)
        rows = con.execute(text("SELECT id, ico, ico_norm FROM suppliers")).fetchall()
        for rid, ico, ico_norm in rows:
            if ico_norm:
                continue
            norm = _normalize_ico_soft(ico)
            if norm:
                con.execute(
                    text("UPDATE suppliers SET ico_norm=:n WHERE id=:id"),
                    {"n": norm, "id": rid},
                )

        # --- indexes (IF NOT EXISTS is safe) ---
        # Supplier fast lookups / joins
        con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_ico_norm ON suppliers(ico_norm)"))

        # Documents filters / sort
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_issue_date ON documents(issue_date)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_supplier_ico ON documents(supplier_ico)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_doc_number ON documents(doc_number)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_bank_account ON documents(bank_account)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_requires_review ON documents(requires_review)"))

        # Line items foreign key / filtering
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_line_items_document_id ON items(document_id)"))

def init_db(engine: Engine) -> None:
    # ensure tables exist
    Base.metadata.create_all(engine)

    _ensure_columns_and_indexes(engine)

    # ensure singleton rows
    with engine.begin() as con:
        con.execute(
            text(
                """
                INSERT OR IGNORE INTO service_state (singleton, running, queue_size)
                VALUES (1, 0, 0)
                """
            )
        )
