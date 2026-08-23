@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo 请先运行 install_windows.bat 完成安装
    pause
    exit /b 1
)
venv\Scripts\python.exe qq_bot.py
pause
