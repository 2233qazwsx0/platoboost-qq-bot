# QQ 机器人部署指南

从零部署一个 QQ 官方机器人：用户私聊或群里 @ 它发 Platoboost 链接，机器人自动求解并回复结果。

---

## 目录

1. [前置条件](#1-前置条件)
2. [在 QQ 开放平台创建机器人](#2-在-qq-开放平台创建机器人)
3. [Windows 一键安装（推荐）](#3-windows-一键安装推荐)
4. [手动安装（Windows / Linux / macOS）](#4-手动安装)
5. [填写配置文件](#5-填写配置文件)
6. [启动与验证](#6-启动与验证)
7. [使用方法](#7-使用方法)
8. [配置项详解](#8-配置项详解)
9. [故障排查](#9-故障排查)
10. [架构说明](#10-架构说明)

---

## 1. 前置条件

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（一键脚本）、Linux、macOS（手动） |
| Python | **3.9 ~ 3.12**（3.13 尚未支持 numba） |
| 网络 | 能访问 GitHub（下载源码）和 `api.sgroup.qq.com`（机器人 API） |
| QQ 账号 | 已实名认证的账号，用于注册 [QQ 开放平台](https://q.qq.com) |

检查 Python：打开 CMD 执行 `python --version`，显示 3.9 ~ 3.12 即可。
没装或版本不对去 https://www.python.org/downloads/ 装 3.12，**勾选 Add python.exe to PATH**。

---

## 2. 在 QQ 开放平台创建机器人

1. 打开 https://q.qq.com ，QQ 扫码登录，完成开发者注册（个人主体即可）。
2. 控制台 → **创建机器人**：名称、头像、简介随意（这是用户看到的机器人形象）。
3. 进入机器人详情，记下两个值（**第 5 步要填**）：
   - **AppID**：机器人详情页顶部的一串数字
   - **AppSecret**：开发设置 → 密钥 → 点「获取」
4. 开发设置 → **沙箱配置**：添加你自己的 QQ 号为沙箱成员。
   机器人未上线前，**只有沙箱成员能和它对话**。
5. 机器人能力：开启 **C2C（单聊）** 和 **群聊** 能力（如平台要求申请，按提示提交）。

> 正式上线需审核，个人开发者的工具类机器人一般能过。上线前用沙箱模式调试完全够用。

---

## 3. Windows 一键安装（推荐）

1. 下载 `install_windows.bat`（仓库主页右侧 Code → Download ZIP，解压后即有；或单独下载该文件放到空文件夹）。
2. **双击运行**。脚本会自动完成：
   - ✅ 检查 Python 版本（不对会提示并给出下载地址）
   - ✅ 下载源代码（有 git 用 git clone，没有就下 zip 自动解压）
   - ✅ 创建独立虚拟环境 `venv\`（不污染系统 Python）
   - ✅ 从阿里云镜像安装全部依赖（失败自动切官方源重试）
   - ✅ 生成 `qq_config.json` 并**自动弹出记事本**提醒你填 AppID/AppSecret
3. 填好配置保存关闭记事本，回到黑窗口按 `y` 回车即启动机器人。

之后日常启动只需双击 `start_windows.bat`。

---

## 4. 手动安装

```bash
# 1. 获取源码
git clone https://github.com/2233qazwsx0/platoboost-qq-bot.git
cd platoboost-qq-bot

# 2. 创建虚拟环境
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

# 3. 安装依赖（国内用阿里云镜像更快）
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 4. 生成配置（首次运行会自动生成模板）
python qq_bot.py
# 提示"请填写 app_id/app_secret"即正常，进入下一步
```

---

## 5. 填写配置文件

用任意编辑器打开 `qq_config.json`：

```json
{
  "app_id": "123456789",
  "app_secret": "abcdef1234567890abcdef",
  "sandbox": true,
  "solve_workers": 4,
  "user_cooldown": 10
}
```

| 字段 | 填什么 |
|---|---|
| `app_id` | 开放平台的 AppID（纯数字） |
| `app_secret` | 开放平台的密钥（点「获取」后复制） |
| `sandbox` | **未上线机器人必须 `true`**；正式上线后改 `false` |
| `solve_workers` | 并发求解线程数，默认 4，一般不用改 |
| `user_cooldown` | 同一用户两条消息最小间隔秒数，防刷屏 |

⚠️ `app_secret` 是机器人的密码，**不要发给别人、不要提交到 git**（`.gitignore` 已排除）。

---

## 6. 启动与验证

```bash
python qq_bot.py
```

正常启动输出：

```
[INFO] 已连接网关, 机器人已就绪
```

验证：用沙箱成员的 QQ 号私聊机器人（QQ 搜索机器人名称），发一条 `你好`，
机器人应回复使用说明；发一条 Platoboost 链接，约 7~10 秒后回复求解结果。

---

## 7. 使用方法

| 场景 | 操作 | 机器人响应 |
|---|---|---|
| 私聊 | 直接发 Platoboost 链接 | "求解中, 约 7~10 秒..." → 结果 |
| 群聊 | @机器人 + 链接 | 同上 |
| 私聊/群 | 发其他文字 | 使用说明 |

支持的消息格式：`https://auth.platorelay.com/...`、`https://plato.pizzabox.com/...` 等任意以 http(s) 开头的链接，前后有无文字都行。

结果格式：`✅ 已解卡: <ticket>`，失败则 `❌ 求解失败: <原因>`。

---

## 8. 配置项详解

- **`sandbox`**：沙箱模式走 `sandbox.api.sgroup.qq.com`，只有沙箱成员可见。上线后改 `false`。
- **`solve_workers`**：同时求解的线程上限。每个求解约占 1 核几秒钟，CPU 差就调小到 2。
- **`user_cooldown`**：同一用户发第二条消息的间隔限制，期间的消息直接忽略（不回复）。
- 频控说明：QQ 官方限制被动回复群消息 5 分钟 5 条、私聊 60 分钟 4 条。本机器人每次求解只回复 2 条（受理 + 结果），正常使用不会触发。

---

## 9. 故障排查

| 现象 | 原因与解决 |
|---|---|
| `请先填写 app_id / app_secret` | 配置没填或填错，回到第 5 步 |
| 连接网关后立刻断开 | AppSecret 错误，重新复制；或 `sandbox` 设了 `true` 但机器人已上线（改 `false`） |
| 机器人不回消息 | ① 你的 QQ 号不在沙箱名单 ② 群里没 @ 机器人 ③ 机器人未开启 C2C/群聊能力 |
| `求解失败: 网络错误` | 服务器访问不了 `auth.platorelay.com`，检查出网 |
| pip 装 numba 失败 | Python 版本 > 3.12，换 3.10~3.12 |
| 长时间无输出 | 看终端日志，`[WARN]` 是可恢复重连，`[ERROR]` 需按提示处理 |

---

## 10. 架构说明

```
QQ 用户 ──(WS 网关)──> qq_bot.py ──> server.run_solves()
                                    ├── auth_client.py   拉取页面/提取票据
                                    ├── captcha_solver.py 验证码识别(numba 加速)
                                    └── 结果缓存(同链接 30 分钟内直接返回)
```

- `qq_bot.py`：QQ 官方机器人客户端（WebSocket 长连接、鉴权、心跳、消息分发）
- `server.py`：求解调度 + 缓存（HTTP API 模式也在这里，`python server.py` 可单独跑 API 服务）
- `auth_client.py`：Platoboost 页面拉取、AES 解密、票据提取
- `captcha_solver.py`：滑块验证码图像识别

本项目基于 [AbabaHnb/Delta-bypass](https://github.com/AbabaHnb/Delta-bypass) 二次开发，新增 QQ 机器人接入。