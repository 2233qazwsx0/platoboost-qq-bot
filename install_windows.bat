@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Platoboost QQ 机器人 - 一键部署（环境检测版）
cd /d "%~dp0"

echo ============================================================
echo    Platoboost QQ 机器人 —— Windows 一键部署（环境检测版）
echo    脚本会逐步检测环境，任何一步出问题都会告诉你怎么修
echo ============================================================
echo.

REM ========== 步骤 1/8：检测目录写权限 ==========
echo [1/8] 检测当前目录写入权限...
> "_wtest.tmp" echo test 2>nul
if errorlevel 1 (
    echo  [错误] 当前目录没有写入权限！
    echo        解决方法（任选其一）：
    echo        A. 右键本脚本 - 以管理员身份运行
    echo        B. 右键文件夹 - 属性 - 安全 - 编辑 - 给你的用户勾选"修改+写入"
    echo        C. 把整个文件夹移到 D:\ 或你的用户目录下再运行
    pause
    exit /b 1
)
del /q "_wtest.tmp" >nul 2>&1
echo  [OK] 目录可写。
echo.

REM ========== 步骤 2/8：检测 Python ==========
echo [2/8] 检测 Python 环境...
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
    echo  [错误] 未检测到 Python！
    echo.
    echo  解决方法（按顺序做）：
    echo   1. 打开 https://www.python.org/downloads/
    echo   2. 下载 Python 3.12（Windows installer 64-bit）
    echo   3. 安装第一屏【务必】勾选 "Add python.exe to PATH"
    echo   4. 装完后关闭本窗口，重新双击本脚本
    pause
    exit /b 1
)
set "PYVER="
for /f "tokens=2" %%v in ('!PYCMD! --version 2^>^&1') do set "PYVER=%%v"
if not defined PYVER (
    echo  [错误] 找到了 !PYCMD! 命令，但无法获取版本号。
    echo        Python 安装可能损坏，建议卸载后重装 3.12。
    pause
    exit /b 1
)
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set "PYMJ=%%a"
    set "PYMN=%%b"
)
if not "!PYMJ!"=="3" (
    echo  [错误] 需要 Python 3.x，当前是 !PYVER!。请安装 3.10~3.12。
    pause
    exit /b 1
)
if !PYMN! LSS 10 (
    echo  [警告] Python !PYVER! 偏旧（建议 3.10~3.12），部分依赖可能装不上，继续尝试...
) else if !PYMN! GTR 12 (
    echo  [错误] Python !PYVER! 版本过新，numba/scipy 装不上。
    echo        请改装 3.10~3.12（推荐 3.12）：https://www.python.org/downloads/
    pause
    exit /b 1
)
echo  [OK] Python !PYVER!（使用命令：!PYCMD!）
echo.

REM ========== 步骤 3/8：检测网络 ==========
echo [3/8] 检测网络连通性...
powershell -NoProfile -Command "exit [int](-not (Test-NetConnection -ComputerName github.com -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue))" >nul 2>&1
if errorlevel 1 (
    echo  [警告] 连不上 github.com:443。
    echo        若稍后下载源码失败：到仓库页面点 Code - Download ZIP，
    echo        解压到本目录后重新运行本脚本（脚本会跳过下载）。
) else (
    echo  [OK] 能访问 GitHub（下载源码用）
)
powershell -NoProfile -Command "exit [int](-not (Test-NetConnection -ComputerName api.sgroup.qq.com -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue))" >nul 2>&1
if errorlevel 1 (
    echo  [警告] 连不上 api.sgroup.qq.com:443（QQ 机器人 API）。
    echo        现在能继续装，但启动后机器人可能收发不了消息，请检查网络/防火墙。
) else (
    echo  [OK] 能访问 QQ 开放平台 API
)
echo.

REM ========== 步骤 4/8：获取源代码 ==========
echo [4/8] 获取源代码...
if exist qq_bot.py (
    echo  [OK] 源代码已存在，跳过下载。
    goto :venv_step
)
where git >nul 2>nul
if not errorlevel 1 (
    echo  [*] 检测到 git，使用 git 克隆...
    git clone --depth 1 https://github.com/2233qazwsx0/platoboost-qq-bot.git platoboost-qq-bot-tmp
    if errorlevel 1 (
        echo  [警告] git 克隆失败，改用 zip 下载...
        rd /s /q platoboost-qq-bot-tmp >nul 2>&1
        goto :zip_step
    )
    xcopy /e /y /i platoboost-qq-bot-tmp\. . >nul 2>&1
    rd /s /q platoboost-qq-bot-tmp >nul 2>&1
    goto :venv_step
)
:zip_step
echo  [*] 使用 PowerShell 下载 zip...
powershell -NoProfile -Command "try{[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;Invoke-WebRequest -Uri 'https://github.com/2233qazwsx0/platoboost-qq-bot/archive/refs/heads/main.zip' -OutFile 'pb-qq-bot.zip' -UseBasicParsing}catch{exit 1}"
if errorlevel 1 (
    echo  [错误] 源码下载失败！
    echo        请检查网络能否访问 GitHub，或手动下载 ZIP 解压到本目录后重跑。
    pause
    exit /b 1
)
powershell -NoProfile -Command "Expand-Archive -Path 'pb-qq-bot.zip' -DestinationPath '.' -Force" >nul 2>&1
xcopy /e /y /i platoboost-qq-bot-main\. . >nul 2>&1
rd /s /q platoboost-qq-bot-main >nul 2>&1
del /q pb-qq-bot.zip >nul 2>&1
:venv_step
if not exist qq_bot.py (
    echo  [错误] 源码获取失败（qq_bot.py 不存在）。
    pause
    exit /b 1
)
echo  [OK] 源代码就绪。
echo.

REM ========== 步骤 5/8：创建虚拟环境 ==========
echo [5/8] 创建虚拟环境 venv...
if exist venv\Scripts\python.exe (
    echo  [OK] venv 已存在，跳过创建。
) else (
    if exist venv rd /s /q venv >nul 2>&1
    !PYCMD! -m venv venv
    if errorlevel 1 (
        echo  [错误] 创建虚拟环境失败！
        echo        常见原因：目录权限不足（右键-以管理员身份运行），
        echo        或 Python 安装不完整（重装 3.12）。
        pause
        exit /b 1
    )
    echo  [OK] 虚拟环境已创建。
)
set "PY=venv\Scripts\python.exe"
echo.

REM ========== 步骤 6/8：安装依赖 ==========
echo [6/8] 安装依赖（阿里云镜像，约 2~5 分钟，请勿关闭窗口）...
"!PY!" -m pip install -q --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ >nul 2>&1
"!PY!" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if errorlevel 1 (
    echo  [警告] 阿里云镜像安装失败，改用官方源重试...
    "!PY!" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo  [错误] 依赖安装失败！请检查网络后重新运行本脚本。
        pause
        exit /b 1
    )
)
echo  [OK] 依赖安装完成。
echo.

REM ========== 步骤 7/8：生成并检查配置 ==========
echo [7/8] 生成/检查配置文件...
if not exist qq_config.json (
    "!PY!" -c "import json;json.dump({'app_id':'','app_secret':'','sandbox':True,'solve_workers':4,'user_cooldown':10},open('qq_config.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)"
    echo  [OK] 已生成 qq_config.json
) else (
    echo  [OK] qq_config.json 已存在，保留你的配置。
)
"!PY!" -c "import json,sys;c=json.load(open('qq_config.json',encoding='utf-8'));sys.exit(0 if c.get('app_id') and c.get('app_secret') else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [提示] app_id / app_secret 还没填，接下来会打开记事本：
    echo   1. 登录 https://q.qq.com ，控制台里创建/选择机器人
    echo   2. 复制 AppID（一串数字）和 AppSecret（密钥）
    echo   3. 填进记事本里对应的引号中，保存并关闭
    echo.
    echo  注意：机器人未上线前 sandbox 保持 true，
    echo        并在开放平台"沙箱配置"里加上你的 QQ 号。
    echo.
    notepad qq_config.json
)
echo.

REM ========== 步骤 8/8：启动 ==========
echo [8/8] 全部就绪！
echo.
set /p "GO=现在启动机器人吗？(y/n): "
if /i "!GO!"=="y" (
    echo.
    echo  [*] 启动中... 看到 [INFO] 已连接网关 即为成功。
    "!PY!" qq_bot.py
    pause
) else (
    echo  以后双击 start_windows.bat 即可启动。
)
endlocal
