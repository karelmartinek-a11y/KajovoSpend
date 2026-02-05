from __future__ import annotations

import sys
from pathlib import Path

def _ensure_src_on_path() -> None:
    # Allow running from repo root without installation (src/ layout)
    here = Path(__file__).resolve()
    repo_root = here.parents[1]
    src = repo_root / 'src'
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

def main() -> None:
    _ensure_src_on_path()
    # Import after sys.path fix
    from run_gui import main as _main
    _main()
