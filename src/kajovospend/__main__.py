"""Spouštěcí bod pro `python -m kajovospend`."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path


def _find_run_gui():
    """
    Preferuje existující `run_gui.py` v kořenovém adresáři repo,
    protože obsahuje nastavení cest a ikon.
    """
    root = Path(__file__).resolve().parents[2]
    candidate = root / "run_gui.py"
    if candidate.exists():
        # Přidej kořen repo na sys.path, aby import run_gui fungoval i při
        # spuštění z jiné složky (např. po instalaci -e).
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        return import_module("run_gui")
    return None


def main() -> int:
    run_gui_mod = _find_run_gui()
    if run_gui_mod and hasattr(run_gui_mod, "main"):
        return run_gui_mod.main()

    # Fallback: nabídni uživateli informaci, proč start selhal.
    raise RuntimeError(
        "Nenalezl jsem soubor run_gui.py. Spusť prosím příkaz z kořene repozitáře "
        "nebo nainstaluj aplikaci v editable režimu (pip install -e .)."
    )


if __name__ == "__main__":
    raise SystemExit(main())
