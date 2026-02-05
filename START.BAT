@echo off
setlocal

set REPO=%~dp0
if "%REPO:~-1%"=="\\" set REPO=%REPO:~0,-1%

if not exist "%REPO%\venv" (
  echo [ERROR] Nenalezena slozka venv.
  echo Spust nejprve:
  echo   python -m venv venv
  echo   venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

call "%REPO%\venv\Scripts\activate.bat" || (
  echo [ERROR] Nelze aktivovat venv.
  exit /b 1
)

pushd "%REPO%"
python -m app_gui
popd

endlocal
