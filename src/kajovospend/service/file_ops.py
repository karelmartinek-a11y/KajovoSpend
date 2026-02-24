from __future__ import annotations

import shutil
import time
from pathlib import Path


def safe_move(src: Path, dst_dir: Path, target_name: str) -> Path:
    normalized_name = Path(str(target_name).replace("\\", "/")).name.strip()
    if not normalized_name:
        normalized_name = src.name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / normalized_name
    dst_dir_resolved = dst_dir.resolve()
    dst_resolved = dst.resolve()
    if dst_resolved != dst_dir_resolved and dst_dir_resolved not in dst_resolved.parents:
        raise ValueError("Cilovy soubor musi byt uvnitr cilove slozky")
    if dst.exists():
        stem = dst.stem
        suffix = dst.suffix
        i = 1
        while True:
            cand = dst_dir / f"{stem}_{i}{suffix}"
            if not cand.exists():
                dst = cand
                break
            i += 1
    for attempt in range(3):
        try:
            shutil.move(str(src), str(dst))
            return dst
        except PermissionError:
            if attempt < 2:
                time.sleep(0.05)
                continue
            shutil.copy2(str(src), str(dst))
            for _ in range(20):
                try:
                    Path(src).unlink()
                    break
                except PermissionError:
                    time.sleep(0.1)
                except Exception:
                    break
            return dst
    return dst
