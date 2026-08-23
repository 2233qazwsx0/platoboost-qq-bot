@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title Platoboost QQ 机器人一键安装
cd /d "%~dp0"

echo ================================================
echo    Platoboost QQ 机器人 - Windows 一键安装
echo ================================================
echo.

REM ---------- 1. 检查 Python ----------
echo [1/5] 检查 Python 环境...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [错误] 未检测到 Python！
    echo  请先安装 Python 3.10 ~ 3.12：
    echo  https://www.python.org/downloads/
    echo  安装时务必勾选 "Add python.exe to PATH"
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
if not "!PYMAJOR!"=="3" (
    echo  [错误] 需要 Python 3.x，当前是 !PYVER!
    pause
    exit /b 1
)
if !PYMINOR! LSS 9 echo  [警告] Python !PYVER! 偏旧，建议 3.10~3.12
if !PYMINOR! GTR 12 echo  [警告] Python !PYVER! 过新，numba 可能装不上，建议 3.10~3.12
echo  [OK] Python !PYVER!
echo.

REM ---------- 2. 获取源代码 ----------
echo [2/5] 获取源代码...
if exist qq_bot.py (
    echo  [OK] 源代码已存在，跳过下载
) else (
    where git >nul 2>nul
    if not errorlevel 1 (
        echo  使用 git 克隆...
        git clone --depth 1 https://github.com/2233qazwsx0/platoboost-qq-bot.git
        if errorlevel 1 (
            echo  [错误] git 克隆失败，请检查网络
            pause
            exit /b 1
        )
        cd platoboost-qq-bot
    ) else (
        echo  未检测到 git，直接下载 zip...
        powershell -NoProfile -Command "try{[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;Invoke-WebRequest -Uri 'https://github.com/2233qazwsx0/platoboost-qq-bot/archive/refs/heads/main.zip' -OutFile 'pb-qq-bot.zip'}catch{exit 1}"
        if errorlevel 1 (
            echo  [错误] 下载失败，请检查网络能否访问 GitHub
            pause
            exit /b 1
        )
        powershell -NoProfile -Command "Expand-Archive -Path 'pb-qq-bot.zip' -DestinationPath '.' -Force"
        move /y platoboost-qq-bot-main\*.* . >nul
        rd /s /q platoboost-qq-bot-main
        del pb-qq-bot.zip
    )
)
if not exist qq_bot.py (
    echo  [错误] 源代码获取失败
    pause
    exit /b 1
)
echo.

REM ---------- 3. 创建虚拟环境 ----------
echo [3/5] 创建虚拟环境...
if exist venv\Scripts\python.exe (
    echo  [OK] 虚拟环境已存在，跳过
) else (
    python -m venv venv
    if errorlevel 1 (
        echo  [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
)
set PY=venv\Scripts\python.exe
echo  [OK] 虚拟环境就绪
echo.

REM ---------- 4. 安装依赖 ----------
echo [4/5] 安装依赖（国内镜像，约 2~5 分钟）...
"%PY%" -m pip install -q --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ >nul 2>&1
"%PY%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if errorlevel 1 (
    echo  阿里云镜像失败，改用官方源重试...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo  [错误] 依赖安装失败，请检查网络后重新运行本脚本
        pause
        exit /b 1
    )
)
echo  [OK] 依赖安装完成
echo.

REM ---------- 5. 生成配置并提醒填写 ----------
echo [5/5] 生成配置文件...
if not exist qq_config.json (
    "%PY%" -c "import json;json.dump({'app_id':'','app_secret':'','sandbox':True,'solve_workers':4,'user_cooldown':10},open('qq_config.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)"
    echo  [OK] 已生成 qq_config.json
) else (
    echo  [OK] qq_config.json 已存在
)
echo.
echo ================================================
echo   安装完成！还差最后一步：
echo.
echo   1. 打开 https://q.qq.com 注册并创建机器人
echo   2. 在"开发设置"里复制 AppID 和 AppSecret
echo   3. 填入马上打开的记事本窗口（两行引号里）
echo.
echo   提示：机器人未上线前 sandbox 保持 true，
echo   并在开放平台"沙箱配置"里添加你的QQ号
echo ================================================
echo.
notepad qq_config.json

set /p START=现在启动机器人吗？(y/n): 
if /i "!START!"=="y" (
    "%PY%" qq_bot.py
    pause
)
