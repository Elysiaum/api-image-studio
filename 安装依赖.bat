@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================
echo   API Image Platform - Setup
echo ==========================================
echo.

rem --- 找一个带 tkinter 的 Python（tkinter 是界面库，必须要有）---
set "PYEXE="
py -3 -c "import tkinter" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
    python -c "import tkinter" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] No usable Python found.
    echo.
    echo This tool needs Python 3.10 or newer WITH the tkinter module.
    echo.
    echo   1^) Install Python from https://www.python.org/downloads/
    echo   2^) In the installer, keep "tcl/tk and IDLE" checked
    echo   3^) Check "Add python.exe to PATH"
    echo   4^) Run this file again
    echo.
    pause
    exit /b 1
)

echo Using Python: & %PYEXE% --version
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Creating virtual environment .venv ...
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/2] Virtual environment already exists.
)

echo [2/2] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed. Try a domestic mirror:
    echo    .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import tkinter, customtkinter, requests, PIL" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Dependencies installed but not importable.
    echo         Your Python may be missing tkinter. Reinstall Python
    echo         with the "tcl/tk and IDLE" option enabled.
    echo.
    pause
    exit /b 1
)

echo.
echo Done. Now double-click "启动.bat" to launch the app.
echo.
pause
