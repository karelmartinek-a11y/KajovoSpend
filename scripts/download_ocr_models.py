"""Download RapidOCR ONNX models into a local directory.

This is an optional helper for "pinned" offline deployments.

Notes:
- URLs can change over time. If a download fails, update MODEL_URLS.
- The application can still run without pinned models (RapidOCR may fetch defaults).
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from urllib.request import urlopen, Request


# Best-effort default URLs. If these break, update to the latest official model hosting.
MODEL_URLS = {
    # Detection model
    "ch_ppocr_server_v2.0_det_infer.onnx": "https://github.com/RapidAI/RapidOCR/releases/download/v1.0.0/ch_ppocr_server_v2.0_det_infer.onnx",
    # Recognition model
    "ch_ppocr_server_v2.0_rec_infer.onnx": "https://github.com/RapidAI/RapidOCR/releases/download/v1.0.0/ch_ppocr_server_v2.0_rec_infer.onnx",
    # Character dictionary
    "ppocr_keys_v1.txt": "https://github.com/RapidAI/RapidOCR/releases/download/v1.0.0/ppocr_keys_v1.txt",
}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "KajovoSpend/1.0"})
    with urlopen(req, timeout=60) as r:
        data = r.read()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", required=True, help="Target directory for RapidOCR models")
    ap.add_argument("--force", action="store_true", help="Redownload even if file exists")
    args = ap.parse_args()

    models_dir = Path(os.path.expandvars(os.path.expanduser(args.models_dir))).resolve()
    models_dir.mkdir(parents=True, exist_ok=True)

    print(f"Models dir: {models_dir}")
    ok = 0
    for name, url in MODEL_URLS.items():
        dest = models_dir / name
        if dest.exists() and not args.force:
            print(f"SKIP {name} (exists, sha256={_sha256(dest)[:12]}...)")
            ok += 1
            continue
        print(f"GET  {name} <- {url}")
        download(url, dest)
        print(f"OK   {name} (sha256={_sha256(dest)[:12]}...)")
        ok += 1

    print(f"Done. Downloaded/verified: {ok}/{len(MODEL_URLS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
