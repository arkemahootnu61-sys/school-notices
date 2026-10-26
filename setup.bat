@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" notices.py init
if errorlevel 1 goto failed
echo.
echo Setup complete. Chrome is reused; no additional browser download is required.
pause
exit /b 0
:failed
echo Setup failed. Check Python 3.10+ and your network connection.
pause
exit /b 1
