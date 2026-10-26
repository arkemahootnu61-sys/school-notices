@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

rem One-time setup: private virtualenv, dependencies, default sources.
rem Keep this file ASCII-only: cmd.exe miscounts byte offsets in UTF-8 batch files
rem and would run the tail of a multibyte line as a command.
if exist ".venv\Scripts\python.exe" goto install

set "PY="
py -3 check_python.py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto createvenv
python check_python.py >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY goto nopython

:createvenv
%PY% -m venv .venv
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" notices.py init
if errorlevel 1 goto failed
echo.
echo Setup complete. Sources come from sources.example.json; an existing sources.json is kept.
echo Chrome is reused when installed; no browser is downloaded.
pause
exit /b 0

:nopython
echo.
echo Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/ with "Add python.exe to PATH" enabled,
echo then run setup.bat again.
pause
exit /b 1

:failed
echo.
echo Setup failed. Check Python 3.10+, the network connection, and that this folder is writable.
pause
exit /b 1
