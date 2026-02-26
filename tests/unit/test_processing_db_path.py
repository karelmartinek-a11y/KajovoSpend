from __future__ import annotations

from pathlib import Path

from kajovospend.db.processing_session import _processing_db_path


def test_processing_db_path_prefers_explicit_config() -> None:
    cfg = {
        "app": {"data_dir": "/tmp/app"},
        "paths": {"processing_db": "/tmp/custom/processing.sqlite"},
    }
    assert _processing_db_path(cfg) == Path("/tmp/custom/processing.sqlite")


def test_processing_db_path_uses_app_data_dir_when_missing_explicit() -> None:
    cfg = {
        "app": {"data_dir": "/tmp/appdata"},
        "paths": {},
    }
    assert _processing_db_path(cfg) == Path("/tmp/appdata") / "kajovospend-processing.sqlite"
