@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

rem One-time setup for a fresh download: private virtualenv, dependencies, default sources.
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
echo 安装完成。来源取自 sources.example.json，不会覆盖已有的 sources.json。
echo Chrome 只复用电脑上已安装的版本，不额外下载浏览器。
pause
exit /b 0

:nopython
echo.
echo 未找到 Python 3.10 或更高版本。
echo 请从 https://www.python.org/downloads/ 安装，并在安装时勾选 "Add python.exe to PATH"，
echo 然后重新双击 setup.bat。
pause
exit /b 1

:failed
echo.
echo 安装失败。请检查 Python 3.10+、网络连接，以及项目目录是否可写。
pause
exit /b 1
