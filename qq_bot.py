#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# QQ 官方机器人入口：用户私聊/群里@发 auth 链接 -> 自动解卡 -> 回 key
# 用法: python qq_bot.py   (配置见 qq_config.json)

import sys, os, time, json, re, threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import requests
import websocket  # pip install websocket-client

from auth_client import extract_ticket
from server import cache_get, cache_put, run_solves  # 复用缓存与求解(含一次重试)

CONFIG_FILE = os.path.join(HERE, "qq_config.json")
DEFAULT_CONFIG = {
    "app_id": "",
    "app_secret": "",
    "sandbox": False,          # 未上线机器人先在沙箱调试: true
    "solve_workers": 4,        # 并发求解线程数
    "user_cooldown": 10,       # 同一用户两条消息最小间隔(秒), 防刷
}

API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
GATEWAY_URL = "https://api.sgroup.qq.com/gateway"
INTENTS = 1 << 25  # GROUP_AND_C2C_EVENT: C2C_MESSAGE_CREATE + GROUP_AT_MESSAGE_CREATE

URL_RE = re.compile(r'https?://\S+')
AT_RE = re.compile(r'^<@!\d+>\s*')

HELP_TEXT = ("用法: /key <链接>\n"
             "示例: /key https://auth.platorelay.com/a?d=xxxx\n"
             "解卡约 7~10 秒, 结果自动回复。")


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


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

    def reply(self, scene, target, msg_id, text):
        """scene: 'c2c' | 'group'; target: openid / group_openid"""
        seq = self.reply_seq.get(msg_id, 0) + 1
        body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": seq}
        path = (f"/v2/users/{target}/messages" if scene == "c2c"
                else f"/v2/groups/{target}/messages")
        r = self.api_post(path, body)
        if r.status_code == 200:
            self.reply_seq[msg_id] = seq
        else:
            log(f"[reply] HTTP {r.status_code}: {r.text[:200]}")

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
        if not content.startswith("/key"):
            self.reply(scene, target, msg_id, HELP_TEXT)
            return
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
                           f"解卡成功\n{key}\n\nby CUA")
                return
            self.reply(scene, target, msg_id, "正在解卡😘")
            key, err, st = run_solves(ticket)
            if key:
                cache_put(ticket, key, st)
                self.reply(scene, target, msg_id,
                           f"解卡成功\n{key}\n\nby CUA")
            else:
                self.reply(scene, target, msg_id, f"解卡失败: {err}")
        except Exception as e:
            log(f"[solve] 异常: {type(e).__name__}: {e}")
            try:
                self.reply(scene, target, msg_id, "内部错误, 稍后再试")
            except Exception:
                pass
        finally:
            with self.busy_lock:
                self.user_busy.discard(openid)

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
