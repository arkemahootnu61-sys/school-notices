@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat once before starting the console.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" notices.py serve %*
set RESULT=%ERRORLEVEL%
echo.
pause
exit /b %RESULT%
