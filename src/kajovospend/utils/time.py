from __future__ import annotations

import datetime as dt


def utc_now_naive() -> dt.datetime:
    """Vrátí aktuální UTC čas jako naive datetime (kompatibilní se stávající SQLite schémou)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)
