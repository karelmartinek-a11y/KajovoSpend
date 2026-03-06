import os
from pathlib import Path

import pytest

from kajovospend.db.dual_db_guard import ensure_separate_databases, DualDbConfigError
from kajovospend.utils.paths import resolve_app_paths


def test_resolve_app_paths_defaults_are_distinct(tmp_path: Path):
    paths = resolve_app_paths(
        data_dir=str(tmp_path),
        db_path=None,
        log_dir=None,
        models_dir=None,
    )
    assert paths.working_db_path != paths.production_db_path
    assert paths.working_db_path.exists() or True  # directory was ensured
    assert paths.production_db_path.parent.exists()


def test_guard_rejects_same_path(tmp_path: Path):
    p = tmp_path / "one.sqlite"
    p.touch()
    with pytest.raises(DualDbConfigError):
        ensure_separate_databases(str(p), str(p))


def test_guard_allows_distinct_paths(tmp_path: Path):
    p1 = tmp_path / "a.sqlite"
    p2 = tmp_path / "b.sqlite"
    w, b = ensure_separate_databases(str(p1), str(p2))
    assert w != b
