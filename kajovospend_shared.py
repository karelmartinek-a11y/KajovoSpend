from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = 1


def utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def expand_path(p: str) -> str:
    # Expand ${APPDATA} and ~
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return os.path.expanduser(p.replace("${APPDATA}", appdata))


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ServiceState:
    running: bool
    last_success: Optional[str]
    last_error: Optional[str]
    queue_size: int
    last_seen: Optional[str]


def db_connect(db_path: str) -> sqlite3.Connection:
    ensure_dir(str(Path(db_path).parent))
    con = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def db_init(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suppliers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ico TEXT NOT NULL UNIQUE,
          name TEXT,
          dic TEXT,
          address TEXT,
          bank_account TEXT,
          is_vat_payer INTEGER,
          ares_last_sync TEXT
        );

        CREATE TABLE IF NOT EXISTS files (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sha256 TEXT NOT NULL UNIQUE,
          original_name TEXT NOT NULL,
          original_path TEXT NOT NULL,
          current_path TEXT NOT NULL,
          mime_type TEXT,
          pages INTEGER DEFAULT 1,
          added_at TEXT NOT NULL,
          processed_at TEXT,
          status TEXT NOT NULL,
          last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS documents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          file_id INTEGER NOT NULL UNIQUE,
          supplier_id INTEGER,
          supplier_ico TEXT,
          doc_number TEXT,
          account_number TEXT,
          issue_date TEXT,
          total_with_vat REAL,
          currency TEXT DEFAULT 'CZK',
          confidence REAL DEFAULT 0.0,
          requires_review INTEGER DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
          FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          document_id INTEGER NOT NULL,
          line_no INTEGER NOT NULL,
          name TEXT NOT NULL,
          quantity REAL,
          vat_rate REAL,
          line_total REAL,
          FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
          UNIQUE(document_id, line_no)
        );

        CREATE TABLE IF NOT EXISTS processing_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          file_id INTEGER NOT NULL,
          ts TEXT NOT NULL,
          level TEXT NOT NULL,
          message TEXT NOT NULL,
          FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS service_status (
          singleton INTEGER PRIMARY KEY CHECK (singleton=1),
          running INTEGER NOT NULL,
          last_success TEXT,
          last_error TEXT,
          queue_size INTEGER NOT NULL,
          last_seen TEXT
        );

        INSERT OR IGNORE INTO service_status(singleton,running,queue_size) VALUES (1,0,0);
        """
    )
    con.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    con.commit()


def get_service_state(con: sqlite3.Connection) -> ServiceState:
    row = con.execute("SELECT * FROM service_status WHERE singleton=1").fetchone()
    if not row:
        return ServiceState(False, None, None, 0, None)
    return ServiceState(
        running=bool(row["running"]),
        last_success=row["last_success"],
        last_error=row["last_error"],
        queue_size=int(row["queue_size"]),
        last_seen=row["last_seen"],
    )


def set_service_state(
    con: sqlite3.Connection,
    *,
    running: Optional[bool] = None,
    last_success: Optional[str] = None,
    last_error: Optional[str] = None,
    queue_size: Optional[int] = None,
) -> None:
    row = con.execute("SELECT * FROM service_status WHERE singleton=1").fetchone()
    if row is None:
        con.execute("INSERT INTO service_status(singleton,running,queue_size) VALUES (1,0,0)")
        con.commit()
        row = con.execute("SELECT * FROM service_status WHERE singleton=1").fetchone()

    new_running = int(running) if running is not None else int(row["running"])
    new_last_success = last_success if last_success is not None else row["last_success"]
    new_last_error = last_error if last_error is not None else row["last_error"]
    new_queue_size = int(queue_size) if queue_size is not None else int(row["queue_size"])

    con.execute(
        """UPDATE service_status
           SET running=?, last_success=?, last_error=?, queue_size=?, last_seen=?
         WHERE singleton=1""",
        (new_running, new_last_success, new_last_error, new_queue_size, utcnow_iso()),
    )
    con.commit()


def upsert_supplier(con: sqlite3.Connection, ico: str, fields: Dict[str, Any]) -> int:
    # returns supplier_id
    ico = ico.strip()
    existing = con.execute("SELECT id FROM suppliers WHERE ico=?", (ico,)).fetchone()
    if existing:
        sets = ",".join([f"{k}=?" for k in fields.keys()])
        if sets:
            con.execute(
                f"UPDATE suppliers SET {sets} WHERE ico=?",
                tuple(fields.values()) + (ico,),
            )
        con.commit()
        return int(existing["id"])

    cols = ",".join(["ico"] + list(fields.keys()))
    qs = ",".join(["?"] * (1 + len(fields)))
    con.execute(
        f"INSERT INTO suppliers({cols}) VALUES ({qs})",
        (ico,) + tuple(fields.values()),
    )
    con.commit()
    return int(con.execute("SELECT id FROM suppliers WHERE ico=?", (ico,)).fetchone()["id"])


def log_event(con: sqlite3.Connection, file_id: int, level: str, message: str) -> None:
    con.execute(
        "INSERT INTO processing_events(file_id,ts,level,message) VALUES (?,?,?,?)",
        (file_id, utcnow_iso(), level.upper(), message),
    )
    con.commit()
