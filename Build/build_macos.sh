#!/usr/bin/env bash
set -euo pipefail

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Chybí .venv. Vytvoř ji: python3 -m venv .venv" >&2
  exit 1
fi

.venv/bin/python -m pip install --upgrade pip pyinstaller

.venv/bin/pyinstaller \
  --noconfirm \
  --clean \
  --name KajovoSpend \
  --windowed \
  --icon assets/app.ico \
  --add-data "assets:assets" \
  --add-data "src:src" \
  run_gui.py

echo "Build hotov: dist/KajovoSpend.app"
