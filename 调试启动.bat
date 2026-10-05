@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONPATH=%~dp0src

echo Running in debug mode (console visible). Close the window to exit.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Please run "安装依赖.bat" first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m api_image_studio
echo.
echo App exited. Press any key to close.
pause >nul
