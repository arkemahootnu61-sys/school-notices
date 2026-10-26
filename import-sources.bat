@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat once before using this shortcut.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" import_sources.py
if errorlevel 1 pause
