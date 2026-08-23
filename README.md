# Platoboost QQ 机器人

QQ 官方机器人：私聊或群里 @ 它发送 Platoboost 链接，自动求解并回复结果（约 7~10 秒）。

## 快速开始（Windows）

1. 下载仓库（Code → Download ZIP）并解压
2. 双击 `install_windows.bat` —— 自动检查 Python、装依赖、生成配置并提醒填写
3. 到 [QQ 开放平台](https://q.qq.com) 创建机器人，把 AppID/AppSecret 填进 `qq_config.json`
4. 双击 `start_windows.bat` 启动

详细教程见 **[DEPLOY.md](DEPLOY.md)**（含开放平台注册、沙箱配置、故障排查）。

## 手动安装（Linux / macOS）

```bash
git clone https://github.com/2233qazwsx0/platoboost-qq-bot.git
cd platoboost-qq-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
python qq_bot.py   # 首次运行生成 qq_config.json，填好后再跑
```

## 使用

| 场景 | 操作 |
|---|---|
| 私聊 | 直接发 Platoboost 链接 |
| 群聊 | @机器人 + 链接 |

回复示例：`✅ 已解卡: eyJhbGciOi...`

## 文件结构

| 文件 | 说明 |
|---|---|
| `qq_bot.py` | QQ 官方机器人客户端（WS 网关/鉴权/消息分发） |
| `server.py` | 求解调度 + 缓存，也可单独跑 HTTP API |
| `auth_client.py` | Platoboost 页面拉取与票据提取 |
| `captcha_solver.py` | 滑块验证码识别（numba 加速） |
| `install_windows.bat` | Windows 一键安装 |
| `start_windows.bat` | Windows 一键启动 |

基于 [AbabaHnb/Delta-bypass](https://github.com/AbabaHnb/Delta-bypass) 二次开发。