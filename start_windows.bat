@echo off
chcp 65001 >nul
setlocal
title Platoboost QQ 机器人
cd /d "%~dp0"
rem 尝试激活 venv
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else (
    rem 没有 venv，先创建
    echo [*] 首次运行，正在创建虚拟环境...
    python -m venv venv || (echo [x] 创建 venv 失败，请以管理员运行或用系统 Python 安装依赖 & pause & exit /b 1)
    call venv\Scripts\activate.bat
    echo [*] 安装依赖...
    pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ || (echo [x] 依赖安装失败 & pause & exit /b 1)
)
if not exist qq_config.json (
    echo [*] 无配置文件，已生成模板，请编辑 qq_config.json 填好 app_id / app_secret
    python qq_bot.py
    pause
    exit /b 1
)
echo [*] 启动 QQ 机器人 (守护模式, 崩溃/更新自动重启)...

:loop
python qq_bot.py
set RC=%errorlevel%
if "%RC%"=="2" (
    echo [%time%] 代码已更新或手动重启, 3 秒后拉起新进程...
    timeout /t 3 /nobreak >nul
    goto loop
)
echo [%time%] 机器人退出 (code %RC%), 10 秒后自动重启 (关闭本窗口可停止)...
timeout /t 10 /nobreak >nul
goto loop