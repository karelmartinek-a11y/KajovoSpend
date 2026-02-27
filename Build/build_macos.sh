#!/usr/bin/env bash
set -euo pipefail

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Chybí .venv. Vytvoř ji: python3 -m venv .venv" >&2
  exit 1
fi

.venv/bin/python -m pip install --upgrade pip pyinstaller

ICON_ARG=()
if [[ -f "assets/app.icns" ]]; then
  ICON_ARG=(--icon "assets/app.icns")
elif [[ -f "assets/app.ico" ]]; then
  echo "Upozornění: assets/app.icns nenalezeno, používám assets/app.ico jako fallback." >&2
  ICON_ARG=(--icon "assets/app.ico")
else
  echo "Upozornění: ikona pro build nenalezena (assets/app.icns ani assets/app.ico)." >&2
fi

.venv/bin/pyinstaller \
  --noconfirm \
  --clean \
  --name KajovoSpend \
  --windowed \
  "${ICON_ARG[@]}" \
  --add-data "assets:assets" \
  --add-data "src:src" \
  run_gui.py

echo "Build hotov: dist/KajovoSpend.app"
