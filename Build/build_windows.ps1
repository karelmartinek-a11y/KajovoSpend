$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Write-Host "Chybí .venv. Vytvoř ji: python -m venv .venv" -ForegroundColor Red
  exit 1
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip pyinstaller

.\.venv\Scripts\pyinstaller.exe `
  --noconfirm `
  --clean `
  --name KajovoSpend `
  --windowed `
  --icon assets/app.ico `
  --add-data "assets;assets" `
  --add-data "src;src" `
  run_gui.py

Write-Host "Build hotov: dist/KajovoSpend/KajovoSpend.exe" -ForegroundColor Green
