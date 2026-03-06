from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple


class DualDbConfigError(ValueError):
    pass


def canonicalize(path: str) -> Path:
    # Resolve symlinks and normalize case on Windows; keep deterministic behavior.
    return Path(os.path.realpath(os.path.expanduser(path))).resolve()


def ensure_separate_databases(working_db: str, production_db: str) -> Tuple[Path, Path]:
    """
    Verify working_db and production_db do not point to the same physical SQLite file.
    Returns canonicalized paths on success or raises DualDbConfigError on violation.
    """
    w = canonicalize(working_db)
    p = canonicalize(production_db)
    if w == p:
        raise DualDbConfigError(
            f"working_db and production_db resolve to the same path: {w}"
        )
    return w, p

