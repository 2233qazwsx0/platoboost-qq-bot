#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# QQ 官方机器人入口：用户私聊/群里@发 auth 链接 -> 自动解卡 -> 回 key
# 用法: python qq_bot.py   (配置见 qq_config.json)

import sys, os, io, time, json, re, zipfile, subprocess, threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import requests
import websocket  # pip install websocket-client

from auth_client import extract_ticket
import server
from server import cache_get, cache_put, run_solves  # 复用缓存与求解(含一次重试)

CONFIG_FILE = os.path.join(HERE, "qq_config.json")
DEFAULT_CONFIG = {
    "app_id": "",
    "app_secret": "",
    "sandbox": False,          # 未上线机器人先在沙箱调试: true
    "solve_workers": 4,        # 并发求解线程数
    "user_cooldown": 10,       # 同一用户两条消息最小间隔(秒), 防刷
    "admins": [],              # 管理员 openid 列表, 用 /whoami 查自己的 openid
}

API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
GATEWAY_URL = "https://api.sgroup.qq.com/gateway"
INTENTS = 1 << 25  # GROUP_AND_C2C_EVENT: C2C_MESSAGE_CREATE + GROUP_AT_MESSAGE_CREATE

# ---------- 自动更新 ----------
UPDATE_ZIP_URL = "https://github.com/2233qazwsx0/platoboost-qq-bot/archive/refs/heads/main.zip"
UPDATE_KEEP = {"qq_config.json", ".key_cache.json"}  # 本地数据, 永不覆盖
UPDATE_EXT = {".py", ".txt", ".md"}                  # bat 运行中被锁, 不自动更新
UPDATE_SKIP_DIRS = {"venv", "__pycache__", ".git", ".idea", "node_modules"}
PIP_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"
EXIT_UPDATED = 2  # 退出码: 已更新代码, 守护脚本应立即重启

URL_RE = re.compile(r'https?://\S+')
AT_RE = re.compile(r'^<@!\d+>\s*')

HELP_TEXT = ("用法: /key <链接>\n"
             "示例: /key https://auth.platorelay.com/a?d=xxxx\n"
             "解卡约 7~10 秒, 结果自动回复。\n"
             "另有: /whoami 查自己的 openid")


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def self_update(here=None):
    """从 GitHub 拉最新 zip, 原地替换可更新文件. 返回 (是否有变更, 摘要)."""
    here = here or HERE
    log("[update] 下载最新代码...")
    data = requests.get(UPDATE_ZIP_URL, timeout=90).content
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return False, "远端 zip 为空"
    root = names[0].split("/")[0] + "/"

    changed = []
    for name in names:
        rel = name[len(root):]
        if not rel:
            continue
        parts = rel.replace("\\", "/").split("/")
        base = parts[-1]
        if base in UPDATE_KEEP:
            continue
        if any(p in UPDATE_SKIP_DIRS for p in parts[:-1]):
            continue
        if "." not in base or "." + base.rsplit(".", 1)[-1] not in UPDATE_EXT:
            continue
        new = zf.read(name)
        dst = os.path.join(here, *parts)
        try:
            with open(dst, "rb") as f:
                old = f.read()
        except Exception:
            old = None
        if old == new:
            continue
        os.makedirs(os.path.dirname(dst) or here, exist_ok=True)
        tmp = dst + ".new"
        with open(tmp, "wb") as f:
            f.write(new)
        os.replace(tmp, dst)
        changed.append(rel)

    if not changed:
        return False, "已是最新版本"

    # requirements 变了 -> 补装依赖
    if any(c.replace("\\", "/").split("/")[-1] == "requirements.txt" for c in changed):
        log("[update] requirements.txt 变更, 安装依赖...")
        r = subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r",
                             os.path.join(here, "requirements.txt"), "-i", PIP_MIRROR])
        if r != 0:
            subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r",
                             os.path.join(here, "requirements.txt")])

    summary = "\n".join(changed[:15]) + (f"\n...等 {len(changed)} 个文件" if len(changed) > 15 else "")
    return True, summary


class TokenManager:
    """access_token 缓存, 到期前 120s 自动刷新; 401 时可强制失效"""

    def __init__(self, app_id, app_secret):
        self.app_id, self.app_secret = app_id, app_secret
        self.token, self.expire_at = None, 0.0
        self.lock = threading.Lock()

    def invalidate(self):
        with self.lock:
            self.expire_at = 0.0

    def get(self):
        with self.lock:
            if self.token and time.time() < self.expire_at - 120:
                return self.token
            r = requests.post(TOKEN_URL, timeout=10, json={
                "appId": self.app_id, "clientSecret": self.app_secret,
            }).json()
            if "access_token" not in r:
                raise RuntimeError(f"获取 access_token 失败: {r}")
            self.token = r["access_token"]
            self.expire_at = time.time() + int(r.get("expires_in", 7200))
            log("[token] 已刷新")
            return self.token


class QQBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tm = TokenManager(cfg["app_id"], cfg["app_secret"])
        self.base = SANDBOX_API_BASE if cfg["sandbox"] else API_BASE
        self.seq = 0
        self.ws = None
        self.hb_thread = None
        self.hb_stop = threading.Event()
        self.started_at = time.time()
        self.updating = False          # /update 防重入
        self.pool = ThreadPoolExecutor(max_workers=cfg["solve_workers"],
                                       thread_name_prefix="solve")
        self.user_last = {}          # openid -> 上次受理时间(节流)
        self.user_busy = set()       # 正在求解的 openid
        self.busy_lock = threading.Lock()
        self.reply_seq = {}          # msg_id -> 已用 msg_seq(同消息多次回复需递增)

    # ---------- OpenAPI ----------
    def api_post(self, path, body):
        for _ in range(2):
            r = requests.post(self.base + path, timeout=15, json=body, headers={
                "Authorization": f"QQBot {self.tm.get()}",
                "Content-Type": "application/json",
            })
            if r.status_code == 401:      # token 失效, 强刷重试一次
                self.tm.invalidate()
                continue
            return r
        return r

    def reply(self, scene, target, msg_id, text, at=None):
        """scene: 'c2c' | 'group'; target: openid / group_openid
        at: 要@的用户 openid(仅群聊生效, 私聊@无意义)"""
        seq = self.reply_seq.get(msg_id, 0) + 1
        # 群聊带@: 先试 markdown(<@!openid> 可高亮@), 无权限则降级纯文本
        if scene == "group" and at:
            body = {"msg_type": 2, "content": f"<@!{at}> {text}",
                    "msg_id": msg_id, "msg_seq": seq}
            r = self.api_post(f"/v2/groups/{target}/messages", body)
            if r.status_code == 200:
                self.reply_seq[msg_id] = seq
                return
            log(f"[reply] md HTTP {r.status_code}, 降级纯文本")
            seq += 1
        body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": seq}
        path = (f"/v2/users/{target}/messages" if scene == "c2c"
                else f"/v2/groups/{target}/messages")
        r = self.api_post(path, body)
        if r.status_code == 200:
            self.reply_seq[msg_id] = seq
        else:
            log(f"[reply] HTTP {r.status_code}: {r.text[:200]}")

    # ---------- 配置/权限 ----------
    def is_admin(self, openid):
        return openid in (self.cfg.get("admins") or [])

    def save_config(self):
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)

    # ---------- 消息处理 ----------
    def on_event(self, t, d):
        if t == "C2C_MESSAGE_CREATE":
            scene, target = "c2c", (d.get("author") or {}).get("id") or d.get("user_openid")
        elif t == "GROUP_AT_MESSAGE_CREATE":
            scene, target = "group", d.get("group_openid")
        else:
            return
        msg_id = d.get("id")
        openid = (d.get("author") or {}).get("id") or d.get("user_openid") or target
        content = AT_RE.sub("", (d.get("content") or "").strip()).strip()
        # 必须以 /key 命令开头
        if not content.startswith("/"):
            return

        if content.startswith("/whoami"):
            self.reply(scene, target, msg_id, f"你的 openid:\n{openid}")
            return
        if content.startswith("/key"):
            self.cmd_key(scene, target, msg_id, openid, content)
            return
        if content.startswith("/status"):
            self.require_admin(scene, target, msg_id, openid, self.cmd_status)
            return
        if content.startswith("/update"):
            self.require_admin(scene, target, msg_id, openid, self.cmd_update)
            return
        if content.startswith("/restart"):
            self.require_admin(scene, target, msg_id, openid, self.cmd_restart)
            return
        if content.startswith("/admin"):
            self.require_admin(scene, target, msg_id, openid, lambda s, t, m: self.cmd_admin(s, t, m, content))
            return
        self.reply(scene, target, msg_id, HELP_TEXT)

    def require_admin(self, scene, target, msg_id, openid, fn):
        if not self.is_admin(openid):
            self.reply(scene, target, msg_id,
                       "仅管理员可用 (用 /whoami 查 openid, 让站长加白)")
            return
        fn(scene, target, msg_id)

    # ---------- /key 求解 ----------
    def cmd_key(self, scene, target, msg_id, openid, content):
        # 节流: 同一用户冷却窗口内忽略
        now = time.time()
        last = self.user_last.get(openid, 0)
        if now - last < self.cfg["user_cooldown"]:
            return
        self.user_last[openid] = now
        m = URL_RE.search(content)
        if not m:
            self.reply(scene, target, msg_id, HELP_TEXT)
            return
        url = m.group(0)

        # 同一用户同时只跑一个求解
        with self.busy_lock:
            if openid in self.user_busy:
                self.reply(scene, target, msg_id, "上一条还在解, 稍等~")
                return
            self.user_busy.add(openid)

        self.pool.submit(self.solve_job, scene, target, msg_id, openid, url)

    def solve_job(self, scene, target, msg_id, openid, url):
        try:
            try:
                ticket = extract_ticket(url)
            except Exception:
                self.reply(scene, target, msg_id, "链接格式不对, 发完整的 auth 链接")
                return
            key, cached, _ = cache_get(ticket)
            if cached:
                self.reply(scene, target, msg_id,
                           f"解卡成功\n{key}\n\nby CUA", at=openid)
                return
            self.reply(scene, target, msg_id,
                       "欢迎使用由CUA部署的借卡机器人\n正在为您解卡", at=openid)
            t0 = time.time()
            key, err, st = run_solves(ticket)
            if key:
                cache_put(ticket, key, st)
                dur = time.time() - t0
                self.reply(scene, target, msg_id,
                           f"解卡成功\n{key}\n用时{dur:.1f}秒\n\nby CUA", at=openid)
            else:
                self.reply(scene, target, msg_id, f"解卡失败: {err}", at=openid)
        except Exception as e:
            log(f"[solve] 异常: {type(e).__name__}: {e}")
            try:
                self.reply(scene, target, msg_id, "内部错误, 稍后再试")
            except Exception:
                pass
        finally:
            with self.busy_lock:
                self.user_busy.discard(openid)

    # ---------- 管理命令 ----------
    def cmd_status(self, scene, target, msg_id):
        up = int(time.time() - self.started_at)
        h, m = up // 3600, up % 3600 // 60
        ws = "在线" if self.ws else "离线"
        with self.busy_lock:
            busy = len(self.user_busy)
        with server.lock:
            cached = len(server.key_cache)
        lines = [
            f"运行: {h}h{m}m  WS: {ws}",
            f"缓存 key: {cached}  求解中: {busy}",
            f"管理员: {len(self.cfg.get('admins') or [])} 人",
        ]
        self.reply(scene, target, msg_id, "\n".join(lines))

    def cmd_update(self, scene, target, msg_id):
        if self.updating:
            self.reply(scene, target, msg_id, "正在更新中, 别急")
            return
        self.updating = True
        try:
            self.reply(scene, target, msg_id, "开始更新, 群发确认后重启...")
            try:
                changed, summary = self_update()
            except Exception as e:
                self.reply(scene, target, msg_id, f"更新失败: {e}")
                return
            if changed:
                self.reply(scene, target, msg_id,
                           f"已更新:\n{summary}\n2 秒后重启生效")
                time.sleep(2)
                os._exit(EXIT_UPDATED)
            else:
                self.reply(scene, target, msg_id, summary)
        finally:
            self.updating = False

    def cmd_restart(self, scene, target, msg_id):
        self.reply(scene, target, msg_id, "重启中...")
        time.sleep(2)
        os._exit(EXIT_UPDATED)

    def cmd_admin(self, scene, target, msg_id, content=None):
        """用法: /admin add <openid> | /admin rm <openid> | /admin ls"""
        parts = (content or "").split()
        admins = self.cfg.setdefault("admins", [])
        if len(parts) < 2 or parts[1] not in ("add", "rm", "ls"):
            self.reply(scene, target, msg_id,
                       "用法: /admin add <openid> | /admin rm <openid> | /admin ls")
            return
        op = parts[1]
        if op == "ls":
            self.reply(scene, target, msg_id,
                       "\n".join(admins) if admins else "(空)")
            return
        if len(parts) < 3 or not parts[2]:
            self.reply(scene, target, msg_id, "缺少 openid")
            return
        oid = parts[2]
        if op == "add":
            if oid not in admins:
                admins.append(oid)
                self.save_config()
            self.reply(scene, target, msg_id, f"已加管理员: {oid}")
        else:  # rm
            if oid in admins:
                admins.remove(oid)
                self.save_config()
            self.reply(scene, target, msg_id, f"已移除: {oid}")

    # ---------- WebSocket ----------
    def heartbeat_loop(self, interval):
        while not self.hb_stop.wait(interval / 1000.0):
            try:
                self.ws.send(json.dumps({"op": 1, "d": self.seq or None}))
            except Exception:
                return  # 连接已断, on_close 会触发重连

    def on_ws_message(self, _, raw):
        try:
            pkt = json.loads(raw)
        except Exception:
            return
        op = pkt.get("op")

        if op == 10:  # Hello -> Identify
            interval = (pkt.get("d") or {}).get("heartbeat_interval", 45000)
            self.ws.send(json.dumps({"op": 2, "d": {
                "token": f"QQBot {self.tm.get()}",
                "intents": INTENTS,
                "shard": [0, 1],
                "properties": {"$os": "linux", "$browser": "delta-bot",
                               "$device": "delta-bot"},
            }}))
            self.hb_stop.clear()
            self.hb_thread = threading.Thread(
                target=self.heartbeat_loop, args=(interval,), daemon=True)
            self.hb_thread.start()

        elif op == 0:  # Dispatch
            if pkt.get("s"):
                self.seq = pkt["s"]
            t = pkt.get("t")
            if t == "READY":
                u = (pkt.get("d") or {}).get("user") or {}
                log(f"[ws] READY, bot={u.get('username')} ({u.get('id')})")
            elif t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                try:
                    self.on_event(t, pkt.get("d") or {})
                except Exception as e:
                    log(f"[event] 处理异常: {type(e).__name__}: {e}")

        elif op == 11:  # Heartbeat ACK
            pass
        elif op in (7, 9):  # 服务端要求重连 / 会话失效
            log(f"[ws] op{op}, 重连...")
            try:
                self.ws.close()
            except Exception:
                pass

    def run_forever(self):
        while True:
            self.hb_stop.set()
            try:
                gw = requests.get(
                    GATEWAY_URL, timeout=10,
                    headers={"Authorization": f"QQBot {self.tm.get()}"},
                ).json()["url"]
                log(f"[ws] 连接 {gw}")
                self.ws = websocket.WebSocketApp(
                    gw,
                    on_message=self.on_ws_message,
                    on_error=lambda ws, e: log(f"[ws] 错误: {e}"),
                    on_close=lambda ws, c, m: log("[ws] 已断开"),
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log(f"[ws] 异常: {type(e).__name__}: {e}")
            log("[ws] 5 秒后重连...")
            time.sleep(5)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"已生成配置模板 {CONFIG_FILE}, 填好 app_id/app_secret 后再运行")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    if not cfg["app_id"] or not cfg["app_secret"]:
        print(f"请先在 {CONFIG_FILE} 里填写 app_id / app_secret")
        sys.exit(1)
    return cfg


if __name__ == "__main__":
    QQBot(load_config()).run_forever()
