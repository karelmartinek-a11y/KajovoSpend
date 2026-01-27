from __future__ import annotations

import re
from typing import Set
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

FTS_ITEMS2 = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts2 USING fts5(
  item_id UNINDEXED,
  document_id UNINDEXED,
  item_name,
  supplier_ico,
  doc_number
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
        con.execute(text(FTS_ITEMS2))

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

        # documents: newly added paging metadata
        cols_docs = con.execute(text("PRAGMA table_info('documents')")).fetchall()
        doc_col_names = {row[1] for row in cols_docs}
        if "page_from" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN page_from INTEGER DEFAULT 1"))
        if "page_to" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN page_to INTEGER"))
        # documents: audit columns for text quality + OpenAI fallback (even if OpenAI not wired yet)
        if "document_text_quality" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN document_text_quality REAL DEFAULT 0.0"))
        if "openai_model" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN openai_model TEXT"))
        if "openai_raw_response" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN openai_raw_response TEXT"))

        # items: UI expects unit_price/ean/item_code
        cols_items = con.execute(text("PRAGMA table_info('items')")).fetchall()
        item_col_names = {row[1] for row in cols_items}
        if "unit_price" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN unit_price REAL"))
        if "ean" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN ean TEXT"))
        if "item_code" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN item_code TEXT"))

        # service_state: observability columns (idempotent)
        cols_ss = con.execute(text("PRAGMA table_info('service_state')")).fetchall()
        ss_col_names = {row[1] for row in cols_ss}
        for name, coltype, dflt in [
            ("inflight", "INTEGER", "0"),
            ("max_workers", "INTEGER", "0"),
            ("current_job_id", "INTEGER", "NULL"),
            ("current_path", "TEXT", "NULL"),
            ("current_phase", "TEXT", "NULL"),
            ("current_progress", "REAL", "NULL"),
            ("heartbeat_at", "TEXT", "NULL"),
            ("stuck", "INTEGER", "0"),
            ("stuck_reason", "TEXT", "NULL"),
        ]:
            if name not in ss_col_names:
                if dflt == "NULL":
                    con.execute(text(f"ALTER TABLE service_state ADD COLUMN {name} {coltype}"))
                else:
                    con.execute(text(f"ALTER TABLE service_state ADD COLUMN {name} {coltype} DEFAULT {dflt}"))

        # --- indexes (IF NOT EXISTS is safe) ---
        # Supplier fast lookups / joins
        con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_ico_norm ON suppliers(ico_norm)"))

        # Documents filters / sort
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_issue_date ON documents(issue_date)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_supplier_ico ON documents(supplier_ico)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_doc_number ON documents(doc_number)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_bank_account ON documents(bank_account)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_requires_review ON documents(requires_review)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_file_page ON documents(file_id, page_from, page_to)"))
        # Kompozitní index pro business duplicity (IČO + číslo dokladu + datum).
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_dup_key ON documents(supplier_ico, doc_number, issue_date)"))
        # Audit / debug
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_text_quality ON documents(document_text_quality)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_documents_extraction_method ON documents(extraction_method)"))

        # Line items foreign key / filtering
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_line_items_document_id ON items(document_id)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_line_items_name ON items(name)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_line_items_ean ON items(ean)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_line_items_item_code ON items(item_code)"))

def init_db(engine: Engine) -> None:
    # ensure tables exist
    Base.metadata.create_all(engine)

    _ensure_columns_and_indexes(engine)

    # ensure singleton rows
    with engine.begin() as con:
        con.execute(
            text(
                """
                INSERT OR IGNORE INTO service_state (singleton, running, queue_size, inflight, max_workers, stuck)
                VALUES (1, 0, 0, 0, 0, 0)
                """
            )
        )
