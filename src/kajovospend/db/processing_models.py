from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import String, Integer, DateTime, Text
from sqlalchemy.orm import declarative_base, mapped_column

BaseProcessing = declarative_base()


class IngestFile(BaseProcessing):
    __tablename__ = "ingest_files"

    id_in = mapped_column(Integer, primary_key=True, autoincrement=True)
    path_original = mapped_column(String(1024), nullable=False)
    path_current = mapped_column(String(1024), nullable=False)
    sha256 = mapped_column(String(64), nullable=True)
    status = mapped_column(String(32), nullable=False, default="NEW")
    size = mapped_column(Integer, nullable=True)
    mtime = mapped_column(DateTime, nullable=True)
    job_id = mapped_column(Integer, nullable=True)
    last_error = mapped_column(Text, nullable=True)
    created_at = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = mapped_column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow, nullable=False)

    @classmethod
    def from_path(cls, path: Path, status: str = "NEW", job_id: int | None = None) -> "IngestFile":
        try:
            st = path.stat()
            size = st.st_size
            mtime = dt.datetime.fromtimestamp(st.st_mtime)
        except Exception:
            size = None
            mtime = None
        return cls(
            path_original=str(path),
            path_current=str(path),
            status=status,
            job_id=job_id,
            size=size,
            mtime=mtime,
        )
