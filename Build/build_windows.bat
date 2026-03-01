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
set ICON=assets\app.ico
if not exist %ICON% (
  echo [WARN] Ikona %ICON% nenalezena, PyInstaller použije default.
)

REM Spuštění PyInstalleru (bez splash screenu – okno se otevře hned)
pyinstaller ^
  --noconfirm ^
  --windowed ^
  --icon=%ICON% ^
  --name=KajovoSpend ^
  run_gui.py

if errorlevel 1 (
  echo Build selhal.
  exit /b 1
)

REM Přesuň finální exe a ukliď
if exist KajovoSpend.exe del /f /q KajovoSpend.exe
if exist dist\KajovoSpend\KajovoSpend.exe copy /y dist\KajovoSpend\KajovoSpend.exe KajovoSpend.exe >nul
if exist build rd /s /q build
if exist dist rd /s /q dist

echo Hotovo. Výstup: %ROOT%\KajovoSpend.exe
popd
endlocal
