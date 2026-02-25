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
        if "pending_ares" not in col_names:
            con.execute(text("ALTER TABLE suppliers ADD COLUMN pending_ares INTEGER DEFAULT 0"))

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

        # documents: VAT/net-gross fields (PULS-001)
        if "total_without_vat" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN total_without_vat REAL"))
        if "total_vat_amount" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN total_vat_amount REAL"))
        if "vat_breakdown_json" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN vat_breakdown_json TEXT"))
        if "doc_type" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN doc_type TEXT"))
        if "processing_profile" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN processing_profile TEXT"))

        # items: UI expects unit_price/ean/item_code
        cols_items = con.execute(text("PRAGMA table_info('items')")).fetchall()
        item_col_names = {row[1] for row in cols_items}
        if "unit_price" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN unit_price REAL"))
        if "ean" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN ean TEXT"))
        if "item_code" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN item_code TEXT"))

        # items: VAT/net-gross fields (PULS-001)
        if "unit_price_net" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN unit_price_net REAL"))
        if "unit_price_gross" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN unit_price_gross REAL"))
        if "line_total_net" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN line_total_net REAL"))
        if "line_total_gross" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN line_total_gross REAL"))
        if "vat_amount" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN vat_amount REAL"))
        if "vat_code" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN vat_code TEXT"))

        # Deterministický backfill kompatibility:
        # - unit_price -> unit_price_net
        # - line_total -> line_total_gross
        con.execute(text("UPDATE items SET unit_price_net = unit_price WHERE unit_price_net IS NULL AND unit_price IS NOT NULL"))
        con.execute(text("UPDATE items SET line_total_gross = line_total WHERE line_total_gross IS NULL AND line_total IS NOT NULL"))

        # Backfill odvozených hodnot z dostupných dat (deterministicky).
        con.execute(text("""
            UPDATE items
            SET
              line_total_net = CASE
                WHEN line_total_net IS NOT NULL THEN line_total_net
                WHEN line_total_gross IS NULL THEN NULL
                WHEN vat_rate IS NULL OR vat_rate = 0 THEN line_total_gross
                ELSE ROUND(line_total_gross / (1.0 + (vat_rate / 100.0)), 2)
              END,
              vat_amount = CASE
                WHEN vat_amount IS NOT NULL THEN vat_amount
                WHEN line_total_gross IS NULL THEN NULL
                WHEN vat_rate IS NULL OR vat_rate = 0 THEN 0.0
                ELSE ROUND(line_total_gross - (line_total_gross / (1.0 + (vat_rate / 100.0))), 2)
              END,
              unit_price_gross = CASE
                WHEN unit_price_gross IS NOT NULL THEN unit_price_gross
                WHEN quantity IS NULL OR quantity = 0 THEN NULL
                WHEN line_total_gross IS NULL THEN NULL
                ELSE ROUND(line_total_gross / quantity, 4)
              END
        """))
        con.execute(text("""
            UPDATE items
            SET unit_price_net = CASE
              WHEN unit_price_net IS NOT NULL THEN unit_price_net
              WHEN quantity IS NULL OR quantity = 0 THEN NULL
              WHEN line_total_net IS NULL THEN NULL
              ELSE ROUND(line_total_net / quantity, 4)
            END
        """))

        # Documents backfill z položek: total_without_vat + total_vat_amount.
        con.execute(text("""
            UPDATE documents
            SET
              total_without_vat = COALESCE(total_without_vat, (
                SELECT ROUND(SUM(COALESCE(i.line_total_net,
                    CASE
                      WHEN i.line_total_gross IS NULL THEN NULL
                      WHEN i.vat_rate IS NULL OR i.vat_rate = 0 THEN i.line_total_gross
                      ELSE i.line_total_gross / (1.0 + (i.vat_rate / 100.0))
                    END
                )), 2)
                FROM items i WHERE i.document_id = documents.id
              )),
              total_vat_amount = COALESCE(total_vat_amount, (
                CASE
                  WHEN total_with_vat IS NULL THEN NULL
                  ELSE ROUND(total_with_vat - COALESCE((
                    SELECT SUM(COALESCE(i.line_total_net,
                      CASE
                        WHEN i.line_total_gross IS NULL THEN NULL
                        WHEN i.vat_rate IS NULL OR i.vat_rate = 0 THEN i.line_total_gross
                        ELSE i.line_total_gross / (1.0 + (i.vat_rate / 100.0))
                      END
                    ))
                    FROM items i WHERE i.document_id = documents.id
                  ), 0.0), 2)
                END
              )),
              doc_type = COALESCE(doc_type, CASE WHEN doc_number IS NULL OR TRIM(doc_number) = '' THEN 'receipt' ELSE 'invoice' END)
        """))

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

        # Import jobs: processing_id_in pro vazbu na zpracovatelskou DB
        cols_jobs = con.execute(text("PRAGMA table_info('import_jobs')")).fetchall()
        job_col_names = {row[1] for row in cols_jobs}
        if "processing_id_in" not in job_col_names:
            con.execute(text("ALTER TABLE import_jobs ADD COLUMN processing_id_in INTEGER"))
            con.execute(text("CREATE INDEX IF NOT EXISTS idx_import_jobs_idin ON import_jobs(processing_id_in)"))

        # Items: technické ID + skupiny + ID účtenky/dodavatele
        cols_items = con.execute(text("PRAGMA table_info('items')")).fetchall()
        item_col_names = {row[1] for row in cols_items}
        if "id_item" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN id_item INTEGER"))
            con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_id_item ON items(id_item)"))
            con.execute(text("UPDATE items SET id_item = rowid WHERE id_item IS NULL"))
        if "id_receipt" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN id_receipt INTEGER"))
        if "id_supplier" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN id_supplier INTEGER"))
        if "group_id" not in item_col_names:
            con.execute(text("ALTER TABLE items ADD COLUMN group_id INTEGER"))

        # Item groups table
        con.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS item_groups (
                    id_group INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    color TEXT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

        # Receipts/documents: ID_Uctenky
        cols_docs = con.execute(text("PRAGMA table_info('documents')")).fetchall()
        doc_col_names = {row[1] for row in cols_docs}
        if "id_receipt" not in doc_col_names:
            con.execute(text("ALTER TABLE documents ADD COLUMN id_receipt INTEGER"))
            con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_id_receipt ON documents(id_receipt)"))
            con.execute(text("UPDATE documents SET id_receipt = id WHERE id_receipt IS NULL"))

        # Suppliers: ID_Dodavatele
        cols_sup = con.execute(text("PRAGMA table_info('suppliers')")).fetchall()
        sup_col_names = {row[1] for row in cols_sup}
        if "id_supplier_ext" not in sup_col_names:
            con.execute(text("ALTER TABLE suppliers ADD COLUMN id_supplier_ext INTEGER"))
            con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_id_supplier_ext ON suppliers(id_supplier_ext)"))
            con.execute(text("UPDATE suppliers SET id_supplier_ext = id WHERE id_supplier_ext IS NULL"))

        # Zpětné doplnění id_supplier/id_receipt do items z vazeb (použij prefixy, aby nedošlo ke kolizi)
        con.execute(
            text(
                """
                UPDATE items
                SET
                  id_receipt = COALESCE(items.id_receipt, d.id_receipt, d.id),
                  id_supplier = COALESCE(items.id_supplier, d.supplier_id)
                FROM documents d
                WHERE d.id = items.document_id
                """
            )
        )

        # Tvrdá stěna: soubory/doklady bez dodavatele do karantény
        con.execute(
            text(
                """
                UPDATE files
                SET status='QUARANTINE'
                WHERE id IN (
                    SELECT DISTINCT file_id FROM documents
                    WHERE supplier_id IS NULL OR supplier_ico IS NULL OR TRIM(COALESCE(supplier_ico,''))=''
                )
                """
            )
        )
        con.execute(
            text(
                """
                UPDATE documents
                SET requires_review=1,
                    review_reasons=COALESCE(review_reasons||'; ','') || 'dodavatel_chybi'
                WHERE supplier_id IS NULL OR supplier_ico IS NULL OR TRIM(COALESCE(supplier_ico,''))=''
                """
            )
        )

        # Karanténa: staré doklady bez dodavatele vrátit zpět
        con.execute(
            text(
                """
                UPDATE files
                SET status='QUARANTINE'
                WHERE id IN (
                    SELECT DISTINCT file_id FROM documents
                    WHERE supplier_id IS NULL OR supplier_ico IS NULL OR TRIM(COALESCE(supplier_ico,''))=''
                )
                """
            )
        )
        con.execute(
            text(
                """
                UPDATE documents
                SET requires_review=1,
                    review_reasons=COALESCE(review_reasons||'; ','') || 'dodavatel_chybi'
                WHERE supplier_id IS NULL OR supplier_ico IS NULL OR TRIM(COALESCE(supplier_ico,''))=''
                """
            )
        )

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
