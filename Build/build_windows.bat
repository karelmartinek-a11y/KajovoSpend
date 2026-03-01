@echo off
setlocal

REM Build GUI aplikace KajovoSpend do .exe (PyInstaller)
REM Požadavky: aktivní venv s nainstalovanými závislostmi a pyinstaller.

REM Cesty
set ROOT=%~dp0..
pushd "%ROOT%"

REM Vytvoř dist/
if exist dist rd /s /q dist

REM Ikona
set ICON=assets\\app.ico
if not exist %ICON% (
  echo [WARN] Ikona %ICON% nenalezena, PyInstaller použije default.
)

REM Splash: použijeme stejné logo, PyInstaller zobrazí při startu
set SPLASH=assets\\app.ico

REM Spuštění PyInstalleru
pyinstaller ^
  --noconfirm ^
  --windowed ^
  --icon=%ICON% ^
  --splash=%SPLASH% ^
  --name=KajovoSpend ^
  run_gui.py

if errorlevel 1 (
  echo Build selhal.
  exit /b 1
)

echo Hotovo. Výstup v dist\\KajovoSpend\\KajovoSpend.exe
popd
endlocal
