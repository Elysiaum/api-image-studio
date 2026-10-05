@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONPATH=%~dp0src

if not exist ".venv\Scripts\pythonw.exe" goto :setup

:launch
start "" ".venv\Scripts\pythonw.exe" -m api_image_studio
exit /b 0

:setup
echo [Setup] First run detected. Preparing environment, please wait...
echo.

set "PYEXE="
py -3 -c "import tkinter" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
    python -c "import tkinter" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] No usable Python found.
    echo.
    echo This tool needs Python 3.10 or newer WITH the tkinter module.
    echo   1^) Install from https://www.python.org/downloads/
    echo   2^) Keep "tcl/tk and IDLE" checked during installation
    echo   3^) Then run "安装依赖.bat"
    echo.
    pause
    exit /b 1
)

%PYEXE% -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Installation failed. Please run "安装依赖.bat" manually.
    pause
    exit /b 1
)

echo Setup complete. Launching...
echo.
goto :launch
