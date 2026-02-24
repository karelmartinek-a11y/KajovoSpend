from __future__ import annotations

from pathlib import Path

from kajovospend.service.file_ops import safe_move


def test_safe_move_sanitizuje_path_traversal_na_nazev(tmp_path: Path) -> None:
    src = tmp_path / "zdroj.pdf"
    src.write_bytes(b"x")
    cil = tmp_path / "cil"

    moved = safe_move(src, cil, "../../unik.txt")

    assert moved.parent == cil
    assert moved.name == "unik.txt"
    assert moved.exists()


def test_safe_move_normalizuje_windows_oddelovac(tmp_path: Path) -> None:
    src = tmp_path / "faktura.pdf"
    src.write_bytes(b"x")
    cil = tmp_path / "out"

    moved = safe_move(src, cil, "podslozka\\doklad.pdf")

    assert moved.parent == cil
    assert moved.name == "doklad.pdf"
    assert moved.exists()
