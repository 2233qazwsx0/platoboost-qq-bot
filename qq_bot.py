#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# QQ 官方机器人入口：用户私聊/群里@发 auth 链接 -> 自动解卡 -> 回 key
# 用法: python qq_bot.py   (配置见 qq_config.json)

import sys, os, io, time, json, re, zipfile, subprocess, threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

HELP_TEXT = ("=== 可用指令 ===\n"
             "【普通用户】\n"
             "/key <链接>  解卡(发 auth.platorelay.com 的链接)\n"
             "/whoami  查自己的 openid\n"
             "/help 或 /菜单  显示本清单\n"
             "【管理员】\n"
             "/status  查看状态\n"
             "/update  在线更新\n"
             "/restart  重启\n"
             "/admin add <openid>  加管理员\n"
             "/admin rm <openid>  移除管理员\n"
             "/admin ls  列出管理员")


def log(*a):
    global LOG_SEQ
    line = f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    with LOG_LOCK:
        LOG_SEQ += 1
        LOG_BUFFER.append((LOG_SEQ, line))


# ---------- 日志网页 ----------
LOG_BUFFER = deque(maxlen=500)
LOG_LOCK = threading.Lock()
LOG_SEQ = 0

LOG_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>CUA 解卡机器人日志</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{background:#0d1117;color:#c9d1d9;font:13px/1.6 monospace;margin:0;padding:16px}
h1{font-size:15px;color:#58a6ff;margin:0 0 8px}
#bar{position:sticky;top:0;background:#0d1117;padding:4px 0;border-bottom:1px solid #21262d;margin-bottom:8px}
#bar span{color:#8b949e;font-size:12px;margin-left:12px}
#logs{white-space:pre-wrap;word-break:break-all}
.ln{padding:1px 0}
.ln b{color:#58a6ff;font-weight:400}
.tag-solve{color:#3fb950}.tag-reply{color:#d29922}.tag-msg{color:#8b949e}
.tag-ws{color:#a371f7}.tag-key{color:#39c5cf}.tag-admin{color:#f85149}
</style></head><body>
<div id="bar"><h1>CUA 解卡机器人日志</h1><span id="st">连接中...</span></div>
<div id="logs"></div>
<script>
let lines=[];
async function tick(){
  try{
    const r=await fetch('/api/logs?after='+lastSeq);
    const j=await r.json();
    if(j.lines.length){
      lastSeq=j.last;
      lines.push(...j.lines);
      if(lines.length>500)lines=lines.slice(-500);
      render();
    }
    document.getElementById('st').textContent='在线 · '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('st').textContent='连接失败,重试中';}
  setTimeout(tick,2000);
}
let lastSeq=0;
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function render(){
  document.getElementById('logs').innerHTML=lines.map(l=>{
    const m=l.match(/^(\[[\d:]+\])(\[[\w ]+\])(.*)$/);
    if(m)return `<div class="ln"><b>${m[1]}</b><span class="tag-${m[2].slice(1,-1).split(' ')[0]}">${m[2]}</span>${esc(m[3])}</span></div>`;
    return `<div class="ln">${esc(l)}</div>`;
  }).join('');
  window.scrollTo(0,document.body.scrollHeight);
}
tick();
</script></body></html>"""


ADMIN_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>CUA 机器人管理</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{background:#0d1117;color:#c9d1d9;font:14px/1.6 system-ui,sans-serif;margin:0;padding:20px;max-width:900px;margin:0 auto}
h1{font-size:18px;color:#58a6ff}
h2{font-size:15px;color:#58a6ff;margin:24px 0 8px}
input,button{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 10px;font-size:13px}
input{width:180px}
button{cursor:pointer}
button:hover{border-color:#58a6ff}
button.danger{color:#f85149}
table{border-collapse:collapse;width:100%;margin-top:8px}
td,th{border-bottom:1px solid #21262d;padding:8px 6px;text-align:left;font-size:13px}
th{color:#8b949e;font-weight:400}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
.on{background:#1b4721;color:#3fb950}.off{background:#4a2020;color:#f85149}
.warn{background:#4a3a10;color:#d29922}
#msg{margin-top:10px;color:#3fb950;font-size:13px;min-height:20px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:center}
.row input{flex:1;min-width:120px}
</style></head><body>
<h1>CUA 机器人管理</h1>
<div class="row"><input id="pw" type="password" placeholder="管理密码" style="width:200px">
<button onclick="savePw()">保存密码</button><span id="st" style="color:#8b949e;font-size:12px"></span></div>
<h2>添加机器人</h2>
<div class="row">
  <input id="a_name" placeholder="名字(如 bot1)">
  <input id="a_id" placeholder="app_id">
  <input id="a_secret" placeholder="app_secret" style="width:220px">
  <input id="a_days" type="number" placeholder="有效天数(空=永久)" style="width:150px">
  <button onclick="addBot()">添加</button>
</div>
<h2>机器人列表</h2>
<table><thead><tr><th>名字</th><th>昵称</th><th>app_id</th><th>状态</th><th>有效期至</th><th>操作</th></tr></thead>
<tbody id="tb"></tbody></table>
<div id="msg"></div>
<script>
let pw=localStorage.getItem('pw')||'';
document.getElementById('pw').value=pw;
function savePw(){pw=document.getElementById('pw').value;localStorage.setItem('pw',pw);load()}
function say(t,isErr){const m=document.getElementById('msg');m.textContent=t;m.style.color=isErr?'#f85149':'#3fb950'}
async function api(body){
  const r=await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...body,pw})});
  const j=await r.json();
  if(r.status===401){say('密码错误',1);throw 0}
  if(j.error)say(j.error,1);else say(j.msg||'OK');
  load();return j;
}
async function admins(n){
  const j=await api({action:'admins_ls',name:n});
  if(!j||!j.admins)return;
  const cur=(j.admins||[]).join("\n");
  const op=prompt(n+' 当前管理员(一行一个 openid, 可直接增删后确认):', cur);
  if(op===null)return;
  const want=op.split(/\s*,\s*|\s+/).map(s=>s.trim()).filter(Boolean).map(s=>s.toUpperCase());
  const have=(j.admins||[]).map(s=>s.toUpperCase());
  for(const o of have){if(!want.includes(o)){await api({action:'admin_rm',name:n,openid:o})}}
  for(const o of want){if(!have.includes(o)){await api({action:'admin_add',name:n,openid:o})}}
  say('管理员已更新');
}
async function load(){
  if(!pw)return;
  try{
    const r=await fetch('/api/bots?pw='+encodeURIComponent(pw));
    if(r.status===401){say('密码错误',1);return}
    const j=await r.json();
    document.getElementById('st').textContent='已连接 · '+j.bots.length+' 个机器人';
    document.getElementById('tb').innerHTML=j.bots.map(b=>`<tr>
      <td>${b.name}</td><td>${b.bot_username?('<b style="color:#58a6ff">'+b.bot_username+'</b>'):'<span style="color:#8b949e">-</span>'}</td><td>${b.app_id}</td>
      <td><span class="badge ${b.running?'on':(b.expired?'warn':'off')}">${b.running?'运行中':(b.expired?'已过期':'已停止')}</span></td>
      <td>${b.expire_str}${b.expire_ts&&!b.expired?' <span style="color:#8b949e">('+Math.ceil((b.expire_ts*1000-Date.now())/86400000)+'天)</span>':''}</td>
      <td>
        ${b.enabled?'<button onclick="act(\'stop\')">停止</button>':'<button onclick="act(\'start\')">启动</button>'}
        <button onclick="renew('${b.name}')">续期</button>
        <button onclick="act('permanent','${b.name}')" title="清除有效期">永久</button>
        <button onclick="admins('${b.name}')">管理员</button>
        <button class="danger" onclick="del('${b.name}')">删除</button>
      </td></tr>`).join('')||'<tr><td colspan=5 style="color:#8b949e">暂无机器人</td></tr>';
  }catch(e){document.getElementById('st').textContent='连接失败'}
}
function act(a,n){api({action:a,name:n})}
function del(n){if(confirm('删除 '+n+' ?'))api({action:'delete',name:n})}
function renew(n){const d=prompt(n+' 续期天数:');if(d)api({action:'renew',name:n,days:d})}
function addBot(){
  const name=document.getElementById('a_name').value.trim();
  const app_id=document.getElementById('a_id').value.trim();
  const app_secret=document.getElementById('a_secret').value.trim();
  const days=document.getElementById('a_days').value;
  if(!name||!app_id||!app_secret){say('名字/app_id/app_secret 必填',1);return}
  api({action:'add',name,app_id,app_secret,days});
}
load();setInterval(load,10000);
</script></body></html>"""


class LogHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        """从 query 或 header 取 pw 校验"""
        pw = None
        if "pw=" in self.path:
            from urllib.parse import parse_qs, urlparse
            pw = parse_qs(urlparse(self.path).query).get("pw", [None])[0]
        if not pw:
            pw = self.headers.get("X-Admin-Pw")
        return pw == ADMIN_PW

    def do_GET(self):
        if self.path.startswith("/api/logs"):
            try:
                after = int(self.path.split("after=")[1].split("&")[0])
            except Exception:
                after = 0
            with LOG_LOCK:
                snap = list(LOG_BUFFER)
            items = [(i, l) for i, l in snap if i > after]
            body = json.dumps({"lines": [l for _, l in items],
                               "last": snap[-1][0] if snap else 0}).encode()
            self._send(200, body)
        elif self.path == "/":
            self._send(200, LOG_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/admin"):
            self._send(200, ADMIN_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/bots"):
            if not self._auth():
                self._send(401, b'{"error": "bad password"}')
                return
            with BOTS_LOCK:
                bots = []
                for n, v in BOTS.items():
                    alive = bool(v["thread"] and v["thread"].is_alive())
                    exp = v["cfg"].get("expire_ts")
                    bots.append({
                        "name": n, "app_id": v["cfg"].get("app_id", ""),
                        "bot_username": (v["bot"].bot_username
                                         if v["bot"] else v["cfg"].get("bot_username")),
                        "enabled": v["enabled"], "running": alive,
                        "expire_ts": exp,
                        "expire_str": (time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
                                       if exp else "永久"),
                        "expired": bool(exp and time.time() > exp),
                        "admins": v["cfg"].get("admins") or [],
                    })
            self._send(200, json.dumps({"bots": bots}, ensure_ascii=False).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        if not self.path.startswith("/api/admin"):
            self._send(404, b"{}")
            return
        try:
            ln = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            self._send(400, b'{"error": "bad json"}')
            return
        if req.get("pw") != ADMIN_PW:
            self._send(401, b'{"error": "bad password"}')
            return
        act = req.get("action")
        try:
            resp = handle_admin(req)
            self._send(200, json.dumps(resp, ensure_ascii=False).encode())
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                       ensure_ascii=False).encode())

    def log_message(self, *a):
        pass  # http.server 默认把请求日志也打进来, 关掉


def handle_admin(req):
    act = req.get("action")
    name = (req.get("name") or "").strip()
    if act == "add":
        if not name or not re.fullmatch(r"[\w\u4e00-\u9fff-]{1,32}", name):
            return {"error": "名字不合法(1-32位字母数字中文横线)"}
        app_id = (req.get("app_id") or "").strip()
        app_secret = (req.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            return {"error": "app_id / app_secret 必填"}
        with BOTS_LOCK:
            if name in BOTS:
                return {"error": f"名字 {name} 已存在"}
        days = req.get("days")
        expire_ts = None
        if days not in (None, "", 0, "0"):
            expire_ts = _expiry_ts(time.time(), float(days))
        cfg = {"app_id": app_id, "app_secret": app_secret,
               "sandbox": bool(req.get("sandbox")), "expire_ts": expire_ts}
        with BOTS_LOCK:
            BOTS[name] = {"cfg": cfg, "bot": None, "thread": None, "enabled": True}
        save_bots()
        start_bot(name)
        return {"ok": True, "msg": f"已添加并启动 {name}"}
    if not name or name not in BOTS:
        return {"error": f"机器人 {name} 不存在"}
    if act == "delete":
        stop_bot(name)
        with BOTS_LOCK:
            BOTS.pop(name)
        save_bots()
        return {"ok": True, "msg": f"已删除 {name}"}
    if act == "start":
        with BOTS_LOCK:
            BOTS[name]["enabled"] = True
        save_bots()
        start_bot(name)
        return {"ok": True, "msg": f"已启动 {name}"}
    if act == "stop":
        with BOTS_LOCK:
            BOTS[name]["enabled"] = False
        save_bots()
        stop_bot(name)
        return {"ok": True, "msg": f"已停止 {name}"}
    if act == "admins_ls":
        with BOTS_LOCK:
            admins = BOTS[name]["cfg"].get("admins") or []
        return {"ok": True, "admins": admins}
    if act == "admin_add":
        oid = (req.get("openid") or "").strip().upper()
        if not oid:
            return {"error": "openid 必填"}
        with BOTS_LOCK:
            admins = BOTS[name]["cfg"].setdefault("admins", [])
            if oid not in admins:
                admins.append(oid)
        save_bots()
        return {"ok": True, "msg": "已添加管理员 " + oid}
    if act == "admin_rm":
        oid = (req.get("openid") or "").strip().upper()
        with BOTS_LOCK:
            admins = BOTS[name]["cfg"].setdefault("admins", [])
            if oid in admins:
                admins.remove(oid)
        save_bots()
        return {"ok": True, "msg": "已移除管理员 " + oid}
    if act == "renew":
        days = float(req.get("days") or 0)
        if days <= 0:
            return {"error": "天数要 > 0"}
        with BOTS_LOCK:
            cur = BOTS[name]["cfg"].get("expire_ts")
            base = cur if (cur and time.time() < cur) else time.time()
            new_exp = _expiry_ts(base, days)
            BOTS[name]["cfg"]["expire_ts"] = new_exp
        save_bots()
        return {"ok": True, "msg": "{} 续期 {} 天, 至 {}".format(
            name, days, time.strftime("%Y-%m-%d %H:%M", time.localtime(new_exp)))}
    if act == "permanent":
        with BOTS_LOCK:
            BOTS[name]["cfg"]["expire_ts"] = None
        save_bots()
        return {"ok": True, "msg": f"{name} 已设为永久"}
    return {"error": f"未知操作 {act}"}


def start_log_server(port=8080):
    try:
        ThreadingHTTPServer(("0.0.0.0", port), LogHandler).serve_forever()
    except Exception as e:
        log(f"[http] 日志服务启动失败: {e}")


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
    def __init__(self, cfg, name="bot"):
        self.name = name
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        self.cfg = cfg
        self.bot_username = None     # READY 后从网关拿到真实昵称
        self.tm = TokenManager(cfg["app_id"], cfg["app_secret"])
        self.base = SANDBOX_API_BASE if cfg["sandbox"] else API_BASE
        self.seq = 0
        self.ws = None
        self.hb_thread = None
        self.hb_stop = threading.Event()
        self.stop_event = threading.Event()   # 外部停止(删除/禁用/过期)
        self.started_at = time.time()
        self.updating = False          # /update 防重入
        self.pool = ThreadPoolExecutor(max_workers=cfg["solve_workers"],
                                       thread_name_prefix="solve")
        self.user_last = {}          # openid -> 上次受理时间(节流)
        self.user_busy = set()       # 正在求解的 openid
        self.busy_lock = threading.Lock()
        self.reply_seq = {}          # msg_id -> 已用 msg_seq(同消息多次回复需递增)

    def stop(self):
        self.stop_event.set()
        self.hb_stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ---------- OpenAPI ----------
    def api_post(self, path, body):
        for _ in range(2):
            r = requests.post(self.base + path, timeout=15, json=body, headers={
                "Authorization": f"QQBot {self.tm.get()}",
                "Content-Type": "application/json",
            })
            if r.status_code == 401:      # token 失效, 强刷重试一次
                log("[api] 401 token失效, 强刷重试")
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
                log(f"[reply] ok {scene} md seq={seq} len={len(text)} text={text[:200]!r}")
                return
            log(f"[reply] md HTTP {r.status_code}, 降级纯文本: {r.text[:120]}")
            seq += 1
        body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": seq}
        path = (f"/v2/users/{target}/messages" if scene == "c2c"
                else f"/v2/groups/{target}/messages")
        r = self.api_post(path, body)
        if r.status_code == 200:
            self.reply_seq[msg_id] = seq
            log(f"[reply] ok {scene} seq={seq} len={len(text)} text={text[:200]!r}")
        else:
            log(f"[reply] FAIL {scene} HTTP {r.status_code}: {r.text[:200]}")

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
        openid = (d.get("author") or {}).get("id") or d.get("user_openid") or target
        content = AT_RE.sub("", (d.get("content") or "").strip()).strip()
        msg_id = d.get("id")
        log(f"[msg] {t} from {openid}: {content[:50]}")
        # 必须以 /key 命令开头
        if not content.startswith("/"):
            self.reply(scene, target, msg_id, HELP_TEXT)
            return

        if content.startswith("/help") or content.startswith("/菜单"):
            self.reply(scene, target, msg_id, HELP_TEXT)
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
            log(f"[admin] 拒绝非管理员 {openid}")
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
            log(f"[key] 节流忽略 {openid} (冷却中 {self.cfg['user_cooldown'] - (now - last):.0f}s)")
            return
        self.user_last[openid] = now
        m = URL_RE.search(content)
        if not m:
            log(f"[key] 无链接 {openid}")
            self.reply(scene, target, msg_id, HELP_TEXT)
            return
        url = m.group(0)

        # 同一用户同时只跑一个求解
        with self.busy_lock:
            if openid in self.user_busy:
                log(f"[key] 忙碌拒绝 {openid} (已有任务在跑)")
                self.reply(scene, target, msg_id, "上一条还在解, 稍等~")
                return
            self.user_busy.add(openid)

        log(f"[key] 受理 {openid} ticket={url[-24:]}")
        self.pool.submit(self.solve_job, scene, target, msg_id, openid, url)

    def solve_job(self, scene, target, msg_id, openid, url):
        try:
            try:
                ticket = extract_ticket(url)
            except Exception:
                log(f"[solve] ticket提取失败 {openid}: {url[:60]}")
                self.reply(scene, target, msg_id, "链接格式不对, 发完整的 auth 链接")
                return
            key, cached, _ = cache_get(ticket)
            if cached:
                log(f"[solve] 命中缓存 {openid} ticket={ticket[:16]}")
                self.reply(scene, target, msg_id,
                           f"解卡成功\n{key}\n\nby CUA", at=openid)
                return
            log(f"[solve] 开始求解 {openid} ticket={ticket[:16]}")
            self.reply(scene, target, msg_id,
                       "欢迎使用由CUA部署的解卡机器人\n正在为您解卡", at=openid)
            t0 = time.time()
            key, err, st = run_solves(ticket)
            dur = time.time() - t0
            if key:
                cache_put(ticket, key, st)
                self.reply(scene, target, msg_id,
                           f"解卡成功\n{key}\n用时{dur:.1f}秒\n\nby CUA", at=openid)
                log(f"[solve] 成功 {openid} 用时{dur:.1f}s ticket={ticket[:16]}")
            else:
                self.reply(scene, target, msg_id, f"解卡失败: {err}", at=openid)
                log(f"[solve] 失败 {openid} 用时{dur:.1f}s err={err}")
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
                log(f"[{self.name}][ws] READY, bot={u.get('username')} ({u.get('id')})")
                # 存真实昵称, 面板展示用
                if u.get("username"):
                    self.bot_username = u["username"]
                    with BOTS_LOCK:
                        ent = BOTS.get(self.name)
                        if ent:
                            ent["cfg"]["bot_username"] = u["username"]
                    save_bots()
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
        elif op == 1:  # 服务端心跳请求
            try:
                self.ws.send(json.dumps({"op": 1, "d": self.seq or None}))
            except Exception:
                pass

    def run_forever(self):
        while not self.stop_event.is_set():
            self.hb_stop.set()
            # 有效期检查: 过期直接退出线程
            exp = self.cfg.get("expire_ts")
            if exp and time.time() > exp:
                log(f"[{self.name}] 已过期, 停止运行")
                return
            try:
                gw = requests.get(
                    GATEWAY_URL, timeout=10,
                    headers={"Authorization": f"QQBot {self.tm.get()}"},
                ).json()["url"]
                log(f"[{self.name}][ws] 连接 {gw}")
                self.ws = websocket.WebSocketApp(
                    gw,
                    on_message=self.on_ws_message,
                    on_error=lambda ws, e: log(f"[{self.name}][ws] 错误: {e}"),
                    on_close=lambda ws, c, m: log(f"[{self.name}][ws] 已断开"),
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log(f"[{self.name}][ws] 异常: {type(e).__name__}: {e}")
            if self.stop_event.is_set():
                break
            log(f"[{self.name}][ws] 5 秒后重连...")
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


# ================= 多实例管理器 =================
BOTS_FILE = os.path.join(HERE, "bots.json")
BOTS = {}          # name -> {"cfg": {...}, "bot": QQBot, "thread": Thread, "enabled": bool}
BOTS_LOCK = threading.Lock()
ADMIN_PASSWORD_FILE = os.path.join(HERE, ".admin_pass")


def load_bots():
    if os.path.exists(BOTS_FILE):
        try:
            with open(BOTS_FILE) as f:
                data = json.load(f)
            for name, cfg in data.items():
                BOTS[name] = {"cfg": cfg, "bot": None, "thread": None,
                              "enabled": cfg.get("enabled", True)}
        except Exception as e:
            log(f"[mgr] bots.json 加载失败: {e}")


def save_bots():
    with BOTS_LOCK:
        data = {n: {**v["cfg"], "enabled": v["enabled"]} for n, v in BOTS.items()}
    tmp = BOTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOTS_FILE)


def start_bot(name):
    with BOTS_LOCK:
        entry = BOTS.get(name)
        if not entry or entry["thread"] and entry["thread"].is_alive():
            return
        bot = QQBot(entry["cfg"], name=name)
        entry["bot"] = bot
        t = threading.Thread(target=bot.run_forever, name=f"bot-{name}", daemon=True)
        entry["thread"] = t
        t.start()
        log(f"[mgr] 已启动 {name}")


def stop_bot(name):
    with BOTS_LOCK:
        entry = BOTS.get(name)
        if not entry:
            return
        bot, t = entry["bot"], entry["thread"]
        entry["bot"], entry["thread"] = None, None
    if bot:
        bot.stop()
    if t and t.is_alive():
        t.join(timeout=8)
    log(f"[mgr] 已停止 {name}")


def _expiry_ts(base_ts, days):
    """按自然日计: 从 base 时刻起 days 个自然日, 到期日 23:59:59"""
    import datetime as _dt
    end = (_dt.datetime.fromtimestamp(base_ts) + _dt.timedelta(days=days)) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    return end.timestamp() - 1


def expire_loop():
    """每 60s 检查一次有效期, 过期自动停"""
    while True:
        time.sleep(60)
        now = time.time()
        with BOTS_LOCK:
            expired = [n for n, v in BOTS.items()
                       if v["enabled"] and v["thread"] and v["thread"].is_alive()
                       and v["cfg"].get("expire_ts") and now > v["cfg"]["expire_ts"]]
        for n in expired:
            log(f"[mgr] {n} 有效期到, 自动停止")
            stop_bot(n)


def get_admin_password():
    if os.path.exists(ADMIN_PASSWORD_FILE):
        with open(ADMIN_PASSWORD_FILE) as f:
            return f.read().strip()
    import secrets
    pw = secrets.token_urlsafe(8)
    with open(ADMIN_PASSWORD_FILE, "w") as f:
        f.write(pw)
    os.chmod(ADMIN_PASSWORD_FILE, 0o600)
    log(f"[mgr] 管理密码已生成: {pw}")
    return pw


if __name__ == "__main__":
    ADMIN_PW = get_admin_password()
    # 旧单实例配置迁移到 bots.json
    if not os.path.exists(BOTS_FILE) and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                old = json.load(f)
            if old.get("app_id") and old.get("app_secret"):
                with open(BOTS_FILE, "w", encoding="utf-8") as f:
                    json.dump({"main": {k: v for k, v in old.items()
                                        if k in ("app_id", "app_secret", "sandbox", "admins")}
                               | {"enabled": True, "expire_ts": None}},
                              f, ensure_ascii=False, indent=2)
                log("[mgr] 已迁移 qq_config.json -> bots.json")
        except Exception as e:
            log(f"[mgr] 迁移失败: {e}")
    load_bots()
    threading.Thread(target=start_log_server, kwargs={"port": 8080}, daemon=True).start()
    threading.Thread(target=expire_loop, daemon=True).start()
    # 启动所有 enabled 的 bot
    for name in list(BOTS):
        if BOTS[name]["enabled"] and not (BOTS[name]["cfg"].get("expire_ts")
                                          and time.time() > BOTS[name]["cfg"]["expire_ts"]):
            start_bot(name)
    log(f"[mgr] 管理面板: http://0.0.0.0:8080/admin  (密码见 .admin_pass 或日志)")
    # 主线程挂起, daemon 线程干活
    threading.Event().wait()
