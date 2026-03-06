from pathlib import Path

from kajovospend.db.production_session import create_production_session_factory
from kajovospend.db.working_session import create_working_session_factory
from kajovospend.db.dual_db_guard import ensure_separate_databases


def test_working_and_production_factories_use_distinct_engines(tmp_path: Path):
    wdb = tmp_path / "w.sqlite"
    pdb = tmp_path / "p.sqlite"
    ensure_separate_databases(str(wdb), str(pdb))

    w_sf = create_working_session_factory(wdb)
    p_sf = create_production_session_factory(pdb)

    assert w_sf.bind is not None and p_sf.bind is not None
    assert str(w_sf.bind.url) != str(p_sf.bind.url)


def test_factories_create_physical_files(tmp_path: Path):
    wdb = tmp_path / "work.sqlite"
    pdb = tmp_path / "prod.sqlite"
    create_working_session_factory(wdb)
    create_production_session_factory(pdb)
    assert wdb.exists()
    assert pdb.exists()
