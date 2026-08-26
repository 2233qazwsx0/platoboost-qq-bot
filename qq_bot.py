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


STOP_TEXT = ("此机器人已被停机\n"
             "请找 3290274245 续费")
# ---------- 默认话术模板 (可被后台深度定制覆盖; 占位符 {key}{dur}{err}{openid}) ----------
DEFAULT_MESSAGES = {
    "welcome":   {"text": "欢迎使用由CUA部署的解卡机器人\n正在为您解卡", "img": ""},
    "success":   {"text": "解卡成功\n{key}\n用时{dur}秒\n\nby CUA", "img": ""},
    "success_cached": {"text": "解卡成功\n{key}\n\nby CUA", "img": ""},
    "fail":      {"text": "解卡失败: {err}", "img": ""},
    "bad_link":  {"text": "链接格式不对, 发完整的 auth 链接", "img": ""},
    "busy":      {"text": "上一条还在解, 稍等~", "img": ""},
    "whoami":    {"text": "你的 openid:\n{openid}", "img": ""},
    "internal":  {"text": "内部错误, 稍后再试", "img": ""},
    "no_admin":  {"text": "仅管理员可用 (用 /whoami 查 openid, 让站长加白)", "img": ""},
    "admin_usage": {"text": "用法: /admin add <openid> | /admin rm <openid> | /admin ls", "img": ""},
    "admin_added": {"text": "已加管理员: {openid}", "img": ""},
    "admin_removed": {"text": "已移除: {openid}", "img": ""},
    "admin_empty": {"text": "(空)", "img": ""},
    "updating":   {"text": "正在更新中, 别急", "img": ""},
    "update_start": {"text": "开始更新, 群发确认后重启...", "img": ""},
    "update_fail": {"text": "更新失败: {err}", "img": ""},
    "update_ok":  {"text": "已更新:\n{summary}\n2 秒后重启生效", "img": ""},
    "restarting": {"text": "重启中...", "img": ""},
    "help":       {"text": "=== 可用指令 ===\n"
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
                          "/admin ls  列出管理员", "img": ""},
    "stop":       {"text": "此机器人已被停机\n请找 3290274245 续费", "img": ""},
}
MSG_KEYS = list(DEFAULT_MESSAGES.keys())
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
.adm{position:relative;display:inline-block}
.adm summary{list-style:none;cursor:pointer;background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 10px;font-size:13px}
.adm summary:hover{border-color:#58a6ff}
.adm-box{position:absolute;top:100%;left:0;margin-top:4px;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px;min-width:260px;z-index:10}
.adm-row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:3px 0}
.adm-row code{color:#58a6ff;font-size:12px;word-break:break-all}
.adm-row button{padding:2px 8px;font-size:12px}
.adm-add{display:flex;gap:6px;margin-top:6px}
.adm-add input{width:auto;flex:1;min-width:0}
#msg{margin-top:10px;color:#3fb950;font-size:13px;min-height:20px}
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;align-items:flex-start;justify-content:center;overflow-y:auto;padding:20px}
#dlg{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;width:640px;max-width:100%;margin:20px 0}
#dlg h2{margin:0 0 12px;font-size:16px}
.mrow{border-bottom:1px solid #21262d;padding:10px 0}
.mrow .k{color:#58a6ff;font-weight:600;font-size:13px;margin-bottom:4px}
.mrow textarea{width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px;font-size:13px;font-family:inherit;resize:vertical}
.mimg{display:flex;align-items:center;gap:8px;margin-top:4px}
.mimg img{max-width:120px;max-height:80px;border-radius:4px;border:1px solid #30363d}
.mimg .ph{color:#8b949e;font-size:12px}
.mimg input[type=file]{display:none}
.mimg button{padding:2px 8px;font-size:12px}
#dlg .foot{margin-top:12px;display:flex;gap:8px}
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
<div id="overlay">
  <div id="dlg">
    <h2>话术定制 - <span id="dlg-name" style="color:#3fb950"></span></h2>
    <div id="dlg-body"></div>
    <div class="foot">
      <button onclick="saveMsgs()">保存</button>
      <button onclick="closeDlg()">关闭</button>
    </div>
  </div>
</div>
<script>
let pw=localStorage.getItem('pw')||'';
document.getElementById('pw').value=pw;
function savePw(){pw=document.getElementById('pw').value;localStorage.setItem('pw',pw);load()}
function say(t,isErr){const m=document.getElementById('msg');m.textContent=t;m.style.color=isErr?'#f85149':'#3fb950'}
async function apiNR(body){const r=await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,pw})});const j=await r.json();if(r.status===401){say('密码错误',1);throw 0}if(j.error)say(j.error,1);else say(j.msg||'OK');return j}
async function api(body){
  const r=await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...body,pw})});
  const j=await r.json();
  if(r.status===401){say('密码错误',1);throw 0}
  if(j.error)say(j.error,1);else say(j.msg||'OK');
  load();return j;
}
async function admins(n){
  const j=await apiNR({action:'admins_ls',name:n});
  if(!j||!j.admins)return;
  const list=(j.admins||[]).map(o=>`<div class="adm-row"><code>${o}</code><button class="danger" onclick="adminRm('${n}','${o}')">删除</button></div>`).join('')||'<div style="color:#8b949e">(暂无管理员)</div>';
  document.getElementById('adm-m'+n).innerHTML=list;
}
async function adminAdd(n){
  const inp=document.getElementById('adm-i'+n);
  const oid=inp.value.trim().toUpperCase();
  if(!oid){say('openid 必填',1);return}
  await apiNR({action:'admin_add',name:n,openid:oid});
  inp.value='';
  admins(n);
}
async function adminRm(n,oid){await apiNR({action:'admin_rm',name:n,openid:oid});admins(n)}
async function load(){
  if(!pw)return;
  try{
    const r=await fetch('/api/bots?pw='+encodeURIComponent(pw));
    if(r.status===401){say('密码错误',1);return}
    const j=await r.json();
    document.getElementById('st').textContent='已连接 · '+j.bots.length+' 个机器人';
    window.botMsgs={};
    j.bots.forEach(b=>{window.botMsgs[b.name]=b.messages||{}});
    document.getElementById('tb').innerHTML=j.bots.map(b=>`<tr>
      <td>${b.name}</td><td>${b.bot_username?('<b style="color:#58a6ff">'+b.bot_username+'</b>'):'<span style="color:#8b949e">-</span>'}</td><td>${b.app_id}</td>
      <td><span class="badge ${b.running?'on':(b.expired?'warn':'off')}">${b.running?'运行中':(b.expired?'已过期':'已停止')}</span></td>
      <td>${b.expire_str}${b.expire_ts&&!b.expired?' <span style="color:#8b949e">('+Math.ceil((b.expire_ts*1000-Date.now())/86400000)+'天)</span>':''}</td>
      <td>
        ${b.enabled?'<button onclick="act(&#39;stop&#39;,&#39;'+b.name+'&#39;)">停止</button>':'<button onclick="act(&#39;start&#39;,&#39;'+b.name+'&#39;)">启动</button>'}
        <button onclick="renew('${b.name}')">续期</button>
        <button onclick="act('permanent','${b.name}')" title="清除有效期">永久</button>
        <button onclick="openDlg('${b.name}')">话术</button>
        <details class="adm" ontoggle="if(this.open)admins('${b.name}')"><summary>管理员</summary>
        <div class="adm-box">
          <div id="adm-m${b.name}"></div>
          <div class="adm-add"><input id="adm-i${b.name}" placeholder="输入 openid 添加"><button onclick="adminAdd('${b.name}')">添加</button></div>
        </div></details>
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
const MSG_LABEL={welcome:'欢迎语',success:'解卡成功',success_cached:'解卡成功(缓存命中)',fail:'解卡失败',bad_link:'链接格式错误',busy:'忙碌提示',whoami:'查询openid',internal:'内部错误',no_admin:'无权限提示',admin_usage:'管理员用法',admin_added:'已加管理员',admin_removed:'已移除管理员',admin_empty:'管理员空列表',updating:'更新中',update_start:'更新开始',update_fail:'更新失败',update_ok:'更新成功',restarting:'重启中',help:'帮助菜单',stop:'停机提示'};
let curName='',curMsgs={};
function openDlg(n){
  curName=n;
  document.getElementById('dlg-name').textContent=n;
  const j=botMsgs[n]||{};
  curMsgs=JSON.parse(JSON.stringify(j));
  const body=document.getElementById('dlg-body');
  body.innerHTML=Object.keys(MSG_LABEL).map(k=>{
    const m=curMsgs[k]||{text:'',img:''};
    const img=(m.img?`<div class="mimg"><img src="/img/${m.img}" onerror="this.style.display='none'"><span class="ph">${m.img}</span><button class="danger" onclick="rmImg('${k}')">删图</button></div>`:`<div class="mimg"><span class="ph">(无图片)</span></div>`);
    return `<div class="mrow">
      <div class="k">${MSG_LABEL[k]} <span style="color:#8b949e;font-weight:400">(${k})</span></div>
      <textarea rows="3" id="mt-${k}">${(m.text||'').replace(/</g,'&lt;')}</textarea>
      ${img}
      <div class="mimg"><input type="file" id="mf-${k}" accept="image/*" onchange="upImg('${k}',this)"><button onclick="document.getElementById('mf-${k}').click()">上传图片</button></div>
    </div>`;
  }).join('');
  document.getElementById('overlay').style.display='flex';
}
function closeDlg(){document.getElementById('overlay').style.display='none'}
async function upImg(k,inp){
  const f=inp.files&&inp.files[0];
  if(!f)return;
  const fd=new FormData();fd.append('img',f);
  const r=await fetch('/api/upload_img',{method:'POST',body:fd});
  const j=await r.json();
  if(j.error){say('上传失败: '+j.error,1);return}
  curMsgs[k]=curMsgs[k]||{text:'',img:''};
  curMsgs[k].img=j.name;
  openDlg(curName);
  say('图片已上传');
}
function rmImg(k){curMsgs[k]=curMsgs[k]||{};curMsgs[k].img='';openDlg(curName)}
async function saveMsgs(){
  const out={};
  Object.keys(MSG_LABEL).forEach(k=>{
    out[k]={text:document.getElementById('mt-'+k).value,img:(curMsgs[k]||{}).img||''};
  });
  await api({action:'save_messages',name:curName,messages:out});
  closeDlg();
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
        if self.path.startswith("/img/"):
            # 静态图片读取 (供 QQ 富媒体拉取)
            name = self.path[len("/img/"):].split("?")[0]
            name = os.path.basename(name)  # 防目录穿越
            fp = os.path.join(IMG_DIR, name)
            if os.path.isfile(fp):
                ct = "image/png" if name.lower().endswith(".png") else \
                     "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) else \
                     "image/gif" if name.lower().endswith(".gif") else \
                     "image/webp" if name.lower().endswith(".webp") else "application/octet-stream"
                with open(fp, "rb") as f:
                    body = f.read()
                self._send(200, body, ct)
            else:
                self._send(404, b"{}")
        elif self.path.startswith("/api/groups"):
            if not self._auth():
                self._send(401, b'{"error": "bad password"}')
                return
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("bot") or [""])[0]
            with BOTS_LOCK:
                bot = BOTS.get(name, {}).get("bot")
            if not bot:
                self._send(200, json.dumps({"groups": [], "bot": name}, ensure_ascii=False).encode())
                return
            groups = sorted(bot.know_groups.items(), key=lambda kv: kv[1].get("last", 0), reverse=True)
            self._send(200, json.dumps({"groups": [
                {"openid": g, "first": r.get("first"), "last": r.get("last"), "msgs": r.get("msgs", 0)}
                for g, r in groups], "bot": name}, ensure_ascii=False).encode())
        elif self.path.startswith("/api/logs"):
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
                        "messages": {k: (v["cfg"].get("messages", {}).get(k) or
                                         DEFAULT_MESSAGES[k])
                                     for k in MSG_KEYS},
                    })
            self._send(200, json.dumps({"bots": bots}, ensure_ascii=False).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        if self.path.startswith("/api/upload_img"):
            # 图片上传: multipart/form-data, 字段 img
            try:
                import cgi
                ln = int(self.headers.get("Content-Length", 0))
                form = cgi.FieldStorage(
                    fp=self.rfile, headers=self.headers,
                    environ={"REQUEST_METHOD": "POST",
                             "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                             "CONTENT_LENGTH": str(ln)})
                f = None
                for _it in (getattr(form, "list", None) or []):
                    if getattr(_it, "name", None) == "img":
                        f = _it
                        break
                if f is None or not getattr(f, "filename", None):
                    self._send(400, b'{"error": "no img"}')
                    return
                fn = f.filename
                ext = os.path.splitext(fn)[1].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                    self._send(400, b'{"error": "unsupported type"}')
                    return
                if not os.path.isdir(IMG_DIR):
                    os.makedirs(IMG_DIR)
                import uuid
                name = uuid.uuid4().hex[:12] + ext
                raw = f.file.read()
                if len(raw) > 20 * 1024 * 1024:
                    self._send(400, b'{"error": "too large"}')
                    return
                with open(os.path.join(IMG_DIR, name), "wb") as fo:
                    fo.write(raw)
                self._send(200, json.dumps({"name": name}, ensure_ascii=False).encode())
            except Exception as e:
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                           ensure_ascii=False).encode())
            return
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
    if act == "push":
        # 主动向指定群发送文本
        with BOTS_LOCK:
            bot = BOTS[name].get("bot")
        if not bot:
            return {"error": f"机器人 {name} 未在线, 无法推送"}
        gid = (req.get("gid") or "").strip()
        text = (req.get("text") or "").strip()
        ok, err = bot.push_group(gid, text)
        return {"ok": ok, "msg": err}
    if act == "get_messages":
        with BOTS_LOCK:
            msgs = {k: (BOTS[name]["cfg"].get("messages", {}).get(k) or
                        DEFAULT_MESSAGES[k]) for k in MSG_KEYS}
        return {"ok": True, "messages": msgs}
    if act == "save_messages":
        msgs_in = req.get("messages") or {}
        # 逐字段合并, 空文本回退默认值
        with BOTS_LOCK:
            saved = BOTS[name]["cfg"].setdefault("messages", {})
            for k in MSG_KEYS:
                mv = msgs_in.get(k) or {}
                item = {}
                t = (mv.get("text") or "").strip()
                item["text"] = t if t else DEFAULT_MESSAGES[k]["text"]
                item["img"] = (mv.get("img") or "").strip()
                saved[k] = item
        save_bots()
        # 热更新: 若 bot 实例已在跑, 直接刷新内存
        with BOTS_LOCK:
            bot = BOTS[name].get("bot")
        if bot:
            bot.msgs = {k: {**DEFAULT_MESSAGES[k], **saved.get(k, {})}
                        for k in MSG_KEYS}
        return {"ok": True, "msg": f"{name} 话术已保存"}
    if act == "rm_img":
        img = (req.get("img") or "").strip()
        if not img or os.path.basename(img) != img:
            return {"error": "非法文件名"}
        fp = os.path.join(IMG_DIR, img)
        if os.path.isfile(fp):
            os.remove(fp)
        # 从所有 bot 的 messages 里清掉该 img 引用
        with BOTS_LOCK:
            for n, v in BOTS.items():
                for k in MSG_KEYS:
                    m = v["cfg"].get("messages", {}).get(k)
                    if m and m.get("img") == img:
                        m["img"] = ""
        save_bots()
        return {"ok": True, "msg": "已删除图片"}
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
        self.enabled = True            # 软停止: False 时在线但只回停机票
        self.pool = ThreadPoolExecutor(max_workers=cfg["solve_workers"],
                                       thread_name_prefix="solve")
        self.user_last = {}          # openid -> 上次受理时间(节流)
        self.user_busy = set()       # 正在求解的 openid
        self.busy_lock = threading.Lock()
        self.reply_seq = {}          # msg_id -> 已用 msg_seq(同消息多次回复需递增)
        self.push_seq = 0            # 主动推送用 msg_seq 递增(无 msg_id)
        self.push_lock = threading.Lock()
        self.know_groups = {}        # group_openid -> {first,last,msgs}; 群记录(方案A)
        gdir = os.path.join(HERE, "groups")
        os.makedirs(gdir, exist_ok=True)
        gfile = os.path.join(gdir, self.name + ".json")
        try:
            if os.path.exists(gfile):
                with open(gfile, encoding="utf-8") as f:
                    self.know_groups = json.load(f)
        except Exception as e:
            log("[groups] 加载群记录失败(%s): %s" % (self.name, e))
        # 深度定制话术: 合并默认值, 后台改过的存 cfg["messages"]
        self.msgs = {k: {**DEFAULT_MESSAGES[k], **cfg.get("messages", {}).get(k, {})}
                     for k in MSG_KEYS}

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

    def tmpl(self, key, **kw):
        """取话术文本并渲染占位符"""
        return self.msgs[key]["text"].format(**kw)
    def send_image(self, scene, target, msg_id, img):
        """上传本地托管图片到 QQ -> file_info -> msg_type=7 发图。
        img: 文件名(imgs/ 下)。返回 True/False。"""
        if not img:
            return False
        mail_url = f"http://45.192.98.5:8080/img/{img}"
        try:
            # 场景上传: c2c=/v2/users/{id}/files, group=/v2/groups/{id}/files
            up_path = (f"/v2/users/{target}/files" if scene == "c2c"
                       else f"/v2/groups/{target}/files")
            r = self.api_post(up_path, {"file_type": 1, "url": mail_url})
            if r.status_code != 200:
                log(f"[img] 上传失败 {scene} HTTP {r.status_code}: {r.text[:150]}")
                return False
            fi = (r.json() or {}).get("file_info")
            if not fi:
                log(f"[img] 无 file_info: {r.text[:150]}")
                return False
        except Exception as e:
            log(f"[img] 上传异常: {type(e).__name__}: {e}")
            return False
        # 发图
        seq = self.reply_seq.get(msg_id, 0) + 1
        body = {"msg_type": 7, "media": {"file_info": fi},
                "msg_id": msg_id, "msg_seq": seq}
        path = (f"/v2/users/{target}/messages" if scene == "c2c"
                else f"/v2/groups/{target}/messages")
        try:
            r = self.api_post(path, body)
        except Exception as e:
            log(f"[img] 发送异常: {type(e).__name__}: {e}")
            return False
        if r.status_code == 200:
            self.reply_seq[msg_id] = seq
            log(f"[img] ok {scene} seq={seq}")
            return True
        log(f"[img] 发送失败 {scene} HTTP {r.status_code}: {r.text[:150]}")
        return False
    def reply(self, scene, target, msg_id, text, at=None, img=None):
        """scene: 'c2c' | 'group'; target: openid / group_openid
        at: 要@的用户 openid(仅群聊生效, 私聊@无意义)
        img: 图片文件名(imgs/ 下), 先发文本再发图"""
        seq = self.reply_seq.get(msg_id, 0) + 1
        # 群聊带@: 先试 markdown(<@!openid> 可高亮@), 无权限则降级纯文本
        if scene == "group" and at:
            body = {"msg_type": 2, "content": f"<@!{at}> {text}",
                    "msg_id": msg_id, "msg_seq": seq}
            r = self.api_post(f"/v2/groups/{target}/messages", body)
            if r.status_code == 200:
                self.reply_seq[msg_id] = seq
                log(f"[reply] ok {scene} md seq={seq} len={len(text)} text={text[:200]!r}")
            else:
                log(f"[reply] md HTTP {r.status_code}, 降级纯文本: {r.text[:120]}")
                seq += 1
                body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": seq}
                r2 = self.api_post(f"/v2/groups/{target}/messages", body)
                if r2.status_code == 200:
                    self.reply_seq[msg_id] = seq
                    log(f"[reply] ok {scene} seq={seq} len={len(text)} text={text[:200]!r}")
        else:
            body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": seq}
            path = (f"/v2/users/{target}/messages" if scene == "c2c"
                    else f"/v2/groups/{target}/messages")
            r = self.api_post(path, body)
            if r.status_code == 200:
                self.reply_seq[msg_id] = seq
                log(f"[reply] ok {scene} seq={seq} len={len(text)} text={text[:200]!r}")
            else:
                log(f"[reply] FAIL {scene} HTTP {r.status_code}: {r.text[:200]}")
        # 文本之后再发图片 (可选)
        if img:
            try:
                self.send_image(scene, target, msg_id, img)
            except Exception as e:
                log(f"[img] 异常: {type(e).__name__}: {e}")
    def msg(self, key, scene, target, msg_id, at=None, **kw):
        """按话术 key 回复: 渲染占位符 + 附带图片(若配置)"""
        text = self.tmpl(key, **kw)
        img = self.msgs[key].get("img") or None
        self.reply(scene, target, msg_id, text, at=at, img=img)

    # ---------- 主动推送 / 群记录(方案A) ----------
    def save_groups(self):
        """持久化记录的群列表"""
        try:
            gdir = os.path.join(HERE, "groups")
            os.makedirs(gdir, exist_ok=True)
            gfile = os.path.join(gdir, self.name + ".json")
            tmp = gfile + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.know_groups, f, ensure_ascii=False, indent=2)
            os.replace(tmp, gfile)
        except Exception as e:
            log("[groups] 保存失败(%s): %s" % (self.name, e))

    def record_group(self, gid):
        """记录一次群消息(用于追踪机器人被拉入的群)"""
        if not gid:
            return
        now = time.time()
        with self.push_lock:
            rec = self.know_groups.setdefault(gid, {"first": 0, "last": 0, "msgs": 0})
            rec["first"] = rec["first"] or int(now)
            rec["last"] = int(now)
            rec["msgs"] = rec.get("msgs", 0) + 1

    def push_group(self, gid, text):
        """主动向群发纯文本消息(管理面板调用). 返回 (ok, err)"""
        text = (text or "").strip()
        if not gid or not text:
            return False, "缺 group_openid 或内容"
        with self.push_lock:
            self.push_seq += 1
            seq = self.push_seq
        body = {"msg_type": 0, "content": text, "msg_seq": seq}
        r = self.api_post(f"/v2/groups/{gid}/messages", body)
        if r.status_code == 200:
            log(f"[push] ok {self.name} -> {gid}: {text[:100]!r}")
            return True, "已推送: " + text[:60]
        err = r.text[:200]
        log(f"[push] FAIL {self.name} {gid} HTTP {r.status_code}: {err}")
        return False, f"推送失败 HTTP {r.status_code}: {err}"

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
            self.record_group(target)
        else:
            return
        openid = (d.get("author") or {}).get("id") or d.get("user_openid") or target
        content = AT_RE.sub("", (d.get("content") or "").strip()).strip()
        msg_id = d.get("id")
        log(f"[msg] {t} from {openid}: {content[:50]}")
        if not self.enabled:
            self.msg("stop", scene, target, msg_id)
            return
        # 必须以 /key 命令开头
        if not content.startswith("/"):
            self.msg("help", scene, target, msg_id)
            return

        if content.startswith("/help") or content.startswith("/菜单"):
            self.msg("help", scene, target, msg_id)
            return
        if content.startswith("/whoami"):
            self.msg("whoami", scene, target, msg_id, openid=openid)
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
        self.msg("help", scene, target, msg_id)

    def require_admin(self, scene, target, msg_id, openid, fn):
        if not self.is_admin(openid):
            log(f"[admin] 拒绝非管理员 {openid}")
            self.msg("no_admin", scene, target, msg_id)
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
            self.msg("help", scene, target, msg_id)
            return
        url = m.group(0)

        # 同一用户同时只跑一个求解
        with self.busy_lock:
            if openid in self.user_busy:
                log(f"[key] 忙碌拒绝 {openid} (已有任务在跑)")
                self.msg("busy", scene, target, msg_id)
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
                self.msg("bad_link", scene, target, msg_id)
                return
            key, cached, _ = cache_get(ticket)
            if cached:
                log(f"[solve] 命中缓存 {openid} ticket={ticket[:16]}")
                self.msg("success_cached", scene, target, msg_id, at=openid, key=key)
                return
            log(f"[solve] 开始求解 {openid} ticket={ticket[:16]}")
            self.msg("welcome", scene, target, msg_id, at=openid)
            t0 = time.time()
            key, err, st = run_solves(ticket)
            dur = time.time() - t0
            if key:
                cache_put(ticket, key, st)
                self.msg("success", scene, target, msg_id, at=openid,
                         key=key, dur=f"{dur:.1f}")
                log(f"[solve] 成功 {openid} 用时{dur:.1f}s ticket={ticket[:16]}")
            else:
                self.msg("fail", scene, target, msg_id, at=openid, err=err)
                log(f"[solve] 失败 {openid} 用时{dur:.1f}s err={err}")
        except Exception as e:
            log(f"[solve] 异常: {type(e).__name__}: {e}")
            try:
                self.msg("internal", scene, target, msg_id)
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
            self.msg("updating", scene, target, msg_id)
            return
        self.updating = True
        try:
            self.msg("update_start", scene, target, msg_id)
            try:
                changed, summary = self_update()
            except Exception as e:
                self.msg("update_fail", scene, target, msg_id, err=str(e))
                return
            if changed:
                self.msg("update_ok", scene, target, msg_id, summary=summary)
                time.sleep(2)
                os._exit(EXIT_UPDATED)
            else:
                self.reply(scene, target, msg_id, summary)
        finally:
            self.updating = False

    def cmd_restart(self, scene, target, msg_id):
        self.msg("restarting", scene, target, msg_id)
        time.sleep(2)
        os._exit(EXIT_UPDATED)

    def cmd_admin(self, scene, target, msg_id, content=None):
        """用法: /admin add <openid> | /admin rm <openid> | /admin ls"""
        parts = (content or "").split()
        admins = self.cfg.setdefault("admins", [])
        if len(parts) < 2 or parts[1] not in ("add", "rm", "ls"):
            self.msg("admin_usage", scene, target, msg_id)
            return
        op = parts[1]
        if op == "ls":
            if admins:
                self.reply(scene, target, msg_id, "\n".join(admins))
            else:
                self.msg("admin_empty", scene, target, msg_id)
            return
        if len(parts) < 3 or not parts[2]:
            self.msg("admin_usage", scene, target, msg_id)
            return
        oid = parts[2]
        if op == "add":
            if oid not in admins:
                admins.append(oid)
                self.save_config()
            self.msg("admin_added", scene, target, msg_id, openid=oid)
        else:  # rm
            if oid in admins:
                admins.remove(oid)
                self.save_config()
            self.msg("admin_removed", scene, target, msg_id, openid=oid)

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
            # 有效期检查: 过期软停(在线回停机票), 不退出线程
            exp = self.cfg.get("expire_ts")
            if exp and time.time() > exp:
                self.enabled = False
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
IMG_DIR = os.path.join(HERE, "imgs")
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
        if not entry:
            return
        if entry["thread"] and entry["thread"].is_alive():
            # 线程还活着(之前是软停止), 直接恢复
            if entry["bot"]:
                entry["bot"].enabled = True
            log(f"[mgr] 已恢复 {name}")
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
        bot.enabled = False
        bot.save_groups()
        log(f"[mgr] 已软停止 {name} (在线置灰, 只回停机票)")


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
