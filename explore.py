#!/usr/bin/env python3
"""
VK Network Explorer — интерактивный проводник по сети VK.
Загружает сеть уровнями (BFS), без физики, без фото.

Usage:
    python explore.py
    python explore.py --port 5001
"""

import json
import sys
import time
import threading
import webbrowser
import argparse
import requests
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

API = "https://api.vk.com/method"
V = "5.131"
TOKEN_FILE = Path.home() / ".vk_chain_token"
FRIENDS_CACHE_FILE = Path("friends_cache.json")
DELAY = 0.35

_session = requests.Session()
_last = 0.0
_vk_lock = threading.Lock()

_user_cache: dict[int, dict] = {}
_friends_cache: dict[int, list | None] = {}


def load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    print("Токен не найден. Запусти vk_chain.py с --token <token> один раз.")
    sys.exit(1)


def load_disk_cache() -> None:
    if not FRIENDS_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(FRIENDS_CACHE_FILE.read_text(encoding="utf-8"))
        for k, v in raw.items():
            _friends_cache[int(k)] = v
        print(f"Кэш загружен: {len(_friends_cache)} пользователей")
    except Exception:
        pass


def vk_api(token: str, method: str, **params):
    global _last
    with _vk_lock:
        wait = DELAY - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        params["access_token"] = token
        params["v"] = V
        try:
            r = _session.get(f"{API}/{method}", params=params, timeout=10)
            data = r.json()
        except Exception:
            return None
        if "error" in data:
            code = data["error"]["error_code"]
            if code in (15, 18, 30):
                return None
            print(f"  [VK {code}] {data['error']['error_msg']}")
            return None
        return data.get("response")


TOKEN = ""


def fetch_user(query: str) -> dict | None:
    try:
        uid = int(str(query).replace("id", "").strip())
    except ValueError:
        uid = query
    if isinstance(uid, int) and uid in _user_cache:
        return _user_cache[uid]
    resp = vk_api(TOKEN, "users.get", user_ids=uid)
    if not resp:
        return None
    u = resp[0]
    if u.get("deactivated"):
        return None
    info = {"id": u["id"], "name": f"{u['first_name']} {u['last_name']}"}
    _user_cache[u["id"]] = info
    return info


def fetch_friends(user_id: int) -> list | None:
    if user_id in _friends_cache:
        raw = _friends_cache[user_id]
        if raw is None:
            return None
        if raw and isinstance(raw[0], dict) and "name" in raw[0]:
            return raw
        del _friends_cache[user_id]
    result = []
    offset = 0
    while True:
        resp = vk_api(TOKEN, "friends.get", user_id=user_id, count=5000, offset=offset, fields="online")
        if resp is None:
            _friends_cache[user_id] = None
            return None
        items = resp if isinstance(resp, list) else resp.get("items", [])
        if not items:
            break
        for u in items:
            if isinstance(u, dict) and not u.get("deactivated"):
                f = {"id": u["id"], "name": f"{u['first_name']} {u['last_name']}"}
                result.append(f)
                _user_cache[u["id"]] = f
        if len(items) < 5000:
            break
        offset += len(items)
    _friends_cache[user_id] = result
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/":
            self._html(HTML)
        elif parsed.path == "/api/user":
            q = qs.get("q", [""])[0].strip()
            user = fetch_user(q) if q else None
            self._json(user if user else {"error": "not found"})
        elif parsed.path == "/api/friends":
            try:
                uid = int(qs.get("id", ["0"])[0])
            except ValueError:
                uid = 0
            friends = fetch_friends(uid)
            self._json({"friends": friends} if friends is not None else {"error": "private", "friends": []})
        else:
            self.send_response(404); self.end_headers()

    def _html(self, c):
        b = c.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _json(self, d):
        b = json.dumps(d, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VK Network Explorer</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--surf:#161b22;--elev:#1c2128;--border:#30363d;
  --acc:#5b8af5;--acc2:rgba(91,138,245,.15);
  --text:#e6edf3;--muted:#7d8590;
  --gold:#f0c040;--green:#3fb950;--red:#f85149;
  --r:8px;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{display:flex;flex-direction:column}

/* WELCOME */
#welcome{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;
  justify-content:center;z-index:200;flex-direction:column;gap:20px;transition:opacity .35s}
#welcome.gone{opacity:0;pointer-events:none}
.wc-title{font-size:22px;font-weight:700}
.wc-sub{font-size:13px;color:var(--muted);text-align:center;line-height:1.7}
.wc-row{display:flex;gap:8px}
#wc-input{width:260px;background:var(--surf);border:1px solid var(--border);
  border-radius:var(--r);color:var(--text);font-size:14px;padding:10px 14px;
  outline:none;transition:border-color .15s}
#wc-input:focus{border-color:var(--acc)}
#wc-input::placeholder{color:var(--muted)}
#wc-btn{padding:10px 18px;background:var(--acc);border:none;border-radius:var(--r);
  color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
#wc-btn:hover{opacity:.85}
#wc-err{font-size:13px;color:var(--red);min-height:18px;text-align:center}

/* HEADER */
header{display:flex;align-items:center;gap:10px;padding:9px 14px;
  background:var(--surf);border-bottom:1px solid var(--border);flex-shrink:0;z-index:10}
.vkb{width:26px;height:26px;background:#0077ff;border-radius:6px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.vkb svg{width:16px;height:16px;fill:#fff}
#h-title{font-size:14px;font-weight:600}
#h-center{font-size:13px;color:var(--muted)}
.h-search{position:relative;margin-left:auto}
.h-search svg{position:absolute;left:9px;top:50%;transform:translateY(-50%);
  width:13px;height:13px;stroke:var(--muted);pointer-events:none}
#h-search{width:190px;background:var(--elev);border:1px solid var(--border);
  border-radius:var(--r);color:var(--text);font-size:13px;
  padding:6px 9px 6px 28px;outline:none;transition:border-color .15s,width .2s}
#h-search:focus{border-color:var(--acc);width:240px}
#h-search::placeholder{color:var(--muted)}
#h-sdrop{position:absolute;top:calc(100% + 5px);right:0;width:240px;
  background:var(--elev);border:1px solid var(--border);border-radius:var(--r);
  z-index:100;max-height:240px;overflow-y:auto;display:none}
#h-sdrop.on{display:block}
.hsi{display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:pointer;transition:background .1s}
.hsi:hover{background:var(--border)}
.hsi-ava{width:26px;height:26px;border-radius:50%;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}
.hsi-name{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hsi-sub{font-size:11px;color:var(--muted)}
.h-stats{display:flex;gap:8px;margin-left:10px}
.chip{background:var(--elev);border:1px solid var(--border);border-radius:16px;
  padding:3px 9px;font-size:12px;color:var(--muted)}
.chip b{color:var(--text)}

/* MAIN */
.wrap{display:flex;flex:1;overflow:hidden;position:relative}
#network{flex:1;background:var(--bg)}

/* LEGEND */
#legend{position:absolute;bottom:14px;right:278px;background:rgba(22,27,34,.92);
  border:1px solid var(--border);border-radius:var(--r);padding:9px 12px;
  font-size:11px;color:var(--muted);display:flex;flex-direction:column;gap:5px;pointer-events:none}
.leg{display:flex;align-items:center;gap:7px}
.leg-dot{width:11px;height:11px;border-radius:50%;flex-shrink:0;border:2px solid transparent}

/* SIDEBAR */
#sidebar{width:264px;flex-shrink:0;background:var(--surf);
  border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#ninfo{flex:1;overflow-y:auto}
#nph{padding:28px 12px;text-align:center;color:var(--muted);font-size:13px;line-height:1.8}
#nph svg{width:34px;height:34px;stroke:var(--border);margin-bottom:10px;display:block;margin-inline:auto}
#ncontent{padding:12px;display:none}
#ncontent.on{display:block}
.np{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.np-ava{width:46px;height:46px;border-radius:50%;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:19px;font-weight:700;color:#fff;border:2px solid var(--acc)}
.np-name{font-size:14px;font-weight:600;line-height:1.3}
.np-sub{font-size:12px;color:var(--muted);margin-top:2px}
.n-actions{display:flex;flex-direction:column;gap:7px;margin-bottom:12px}
.btn-exp{display:flex;align-items:center;justify-content:center;padding:8px;
  border:1px solid rgba(91,138,245,.35);border-radius:var(--r);background:var(--acc2);
  color:var(--acc);font-size:13px;font-weight:500;cursor:pointer;
  transition:background .15s,border-color .15s;width:100%}
.btn-exp:hover{background:rgba(91,138,245,.28);border-color:rgba(91,138,245,.7)}
.btn-exp:disabled{opacity:.4;cursor:not-allowed}
.btn-exp.expanded{border-color:rgba(63,185,80,.35);background:rgba(63,185,80,.1);color:var(--green)}
.btn-exp.loading{border-color:rgba(240,192,64,.35);background:rgba(240,192,64,.1);color:var(--gold)}
.vk-btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;
  border:1px solid var(--border);border-radius:var(--r);background:transparent;
  color:var(--muted);font-size:13px;text-decoration:none;
  transition:border-color .15s,color .15s;width:100%}
.vk-btn:hover{border-color:var(--acc);color:var(--acc)}
.ml-title{font-size:10px;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;color:var(--muted);margin-bottom:7px}
.ml{display:flex;flex-direction:column;gap:4px;max-height:260px;overflow-y:auto}
.mi{display:flex;align-items:center;gap:8px;padding:4px 5px;
  border-radius:6px;cursor:pointer;transition:background .1s}
.mi:hover{background:var(--elev)}
.mi-ava{width:24px;height:24px;border-radius:50%;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#fff}
.mi-name{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.mi-ok{flex-shrink:0;font-size:10px;color:var(--green)}

/* CONTROLS */
#ctrls{position:absolute;bottom:14px;left:14px;display:flex;flex-direction:column;gap:5px;z-index:50}
.cb{width:34px;height:34px;background:var(--surf);border:1px solid var(--border);
  border-radius:7px;color:var(--muted);cursor:pointer;display:flex;align-items:center;
  justify-content:center;transition:all .15s;user-select:none}
.cb:hover{background:var(--elev);color:var(--text);border-color:var(--acc)}
.cb-sep{height:1px;background:var(--border);margin:1px 0}

/* IMPORT PANEL */
#imp{position:fixed;bottom:18px;right:18px;z-index:300;
  background:var(--surf);border:1px solid var(--border);border-radius:10px;
  width:248px;box-shadow:0 4px 28px rgba(0,0,0,.55);display:none;flex-direction:column}
#imp.on{display:flex}
#imp-head{display:flex;align-items:center;gap:7px;padding:9px 12px;
  background:var(--elev);border-bottom:1px solid var(--border);border-radius:10px 10px 0 0}
#imp-head span{font-size:12px;font-weight:600;flex:1}
#imp-min{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;line-height:1;padding:0 2px}
#imp-min:hover{color:var(--text)}
#imp-body{padding:10px 12px;display:flex;flex-direction:column;gap:9px}
#imp.collapsed #imp-body{display:none}

/* progress */
.imp-prog{display:flex;align-items:center;gap:7px}
.imp-track{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.imp-bar{height:100%;width:0;background:var(--acc);border-radius:2px;transition:width .3s}
.imp-pct{font-size:11px;color:var(--muted);width:32px;text-align:right;flex-shrink:0}
#imp-status{font-size:12px;color:var(--muted);line-height:1.5;min-height:18px}

/* countdown ring */
#imp-cd{display:none;flex-direction:column;align-items:center;gap:6px;padding:4px 0}
#imp-cd.on{display:flex}
#imp-cd-ring{position:relative;width:52px;height:52px}
#imp-cd-ring svg{position:absolute;inset:0;transform:rotate(-90deg)}
#imp-cd-ring circle{fill:none;stroke-width:3}
.cd-bg{stroke:var(--border)}
.cd-arc{stroke:var(--acc);stroke-linecap:round;transition:stroke-dashoffset .2s linear}
#imp-cd-num{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:16px;font-weight:700;color:var(--text)}
#imp-cd-lbl{font-size:11px;color:var(--muted);text-align:center}

/* buttons */
.imp-btns{display:flex;gap:6px}
.imp-btns button{flex:1;padding:7px 0;font-size:12px;font-weight:500;
  border-radius:6px;cursor:pointer;transition:all .15s;border:1px solid var(--border)}
#imp-pause{background:var(--acc2);border-color:rgba(91,138,245,.4);color:var(--acc)}
#imp-pause:hover{background:rgba(91,138,245,.28)}
#imp-pause.paused{background:rgba(63,185,80,.1);border-color:rgba(63,185,80,.4);color:var(--green)}
#imp-stop{background:transparent;color:var(--muted)}
#imp-stop:hover{background:rgba(248,81,73,.1);border-color:rgba(248,81,73,.4);color:var(--red)}

/* sliders */
.imp-sliders{display:flex;flex-direction:column;gap:7px}
.sl-row{display:flex;flex-direction:column;gap:3px}
.sl-label{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}
.sl-label b{color:var(--text)}
input[type=range]{width:100%;accent-color:var(--acc);cursor:pointer}

/* TOAST */
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  background:var(--surf);border:1px solid var(--border);border-radius:var(--r);
  padding:9px 16px;font-size:13px;color:var(--text);z-index:500;
  opacity:0;transition:opacity .25s;pointer-events:none}
#toast.show{opacity:1}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>

<!-- WELCOME -->
<div id="welcome">
  <div class="vkb" style="width:48px;height:48px;border-radius:12px;margin-bottom:-4px">
    <svg viewBox="0 0 24 24" style="width:30px;height:30px"><path d="M15.07 2H8.93C3.33 2 2 3.33 2 8.93v6.14C2 20.67 3.33 22 8.93 22h6.14C20.67 22 22 20.67 22 15.07V8.93C22 3.33 20.67 2 15.07 2zm2.08 13.5h-1.7c-.64 0-.84-.51-1.99-1.67-1-.98-1.44-.98-1.69-.98-.34 0-.44.1-.44.59v1.52c0 .42-.13.67-1.23.67-1.82 0-3.83-1.1-5.24-3.16C3.27 10.47 3 9 3 8.47c0-.25.1-.49.59-.49h1.7c.44 0 .61.2.78.67.86 2.47 2.3 4.64 2.9 4.64.22 0 .32-.1.32-.66v-2.57c-.07-1.19-.69-1.29-.69-1.72 0-.2.17-.41.44-.41h2.67c.37 0 .5.2.5.63v3.47c0 .37.17.5.27.5.22 0 .41-.13.83-.54 1.28-1.43 2.2-3.64 2.2-3.64.12-.25.32-.49.76-.49h1.7c.51 0 .62.26.51.62-.22.98-2.35 4.02-2.35 4.02-.19.31-.25.44 0 .78.19.27.8.83 1.2 1.33.75.88 1.32 1.62 1.47 2.13.17.51-.08.77-.59.77z"/></svg>
  </div>
  <div class="wc-title">VK Network Explorer</div>
  <div class="wc-sub">Введи ID или адрес страницы ВКонтакте</div>
  <div class="wc-row">
    <input id="wc-input" type="text" placeholder="id123456 или vk.com/durov" autofocus>
    <button id="wc-btn">Открыть</button>
  </div>
  <div id="wc-err"></div>
</div>

<!-- HEADER -->
<header>
  <div class="vkb">
    <svg viewBox="0 0 24 24"><path d="M15.07 2H8.93C3.33 2 2 3.33 2 8.93v6.14C2 20.67 3.33 22 8.93 22h6.14C20.67 22 22 20.67 22 15.07V8.93C22 3.33 20.67 2 15.07 2zm2.08 13.5h-1.7c-.64 0-.84-.51-1.99-1.67-1-.98-1.44-.98-1.69-.98-.34 0-.44.1-.44.59v1.52c0 .42-.13.67-1.23.67-1.82 0-3.83-1.1-5.24-3.16C3.27 10.47 3 9 3 8.47c0-.25.1-.49.59-.49h1.7c.44 0 .61.2.78.67.86 2.47 2.3 4.64 2.9 4.64.22 0 .32-.1.32-.66v-2.57c-.07-1.19-.69-1.29-.69-1.72 0-.2.17-.41.44-.41h2.67c.37 0 .5.2.5.63v3.47c0 .37.17.5.27.5.22 0 .41-.13.83-.54 1.28-1.43 2.2-3.64 2.2-3.64.12-.25.32-.49.76-.49h1.7c.51 0 .62.26.51.62-.22.98-2.35 4.02-2.35 4.02-.19.31-.25.44 0 .78.19.27.8.83 1.2 1.33.75.88 1.32 1.62 1.47 2.13.17.51-.08.77-.59.77z"/></svg>
  </div>
  <span id="h-title">Network Explorer</span>
  <span id="h-center"></span>
  <div class="h-search">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
    <input id="h-search" type="text" placeholder="Найти в графе...">
    <div id="h-sdrop"></div>
  </div>
  <div class="h-stats">
    <div class="chip"><b id="st-nodes">0</b> узлов</div>
    <div class="chip"><b id="st-edges">0</b> связей</div>
  </div>
</header>

<!-- MAIN -->
<div class="wrap">
  <div id="network"></div>

  <div id="legend">
    <div class="leg"><div class="leg-dot" style="background:#1c2128;border-color:#30363d"></div> Не раскрыт</div>
    <div class="leg"><div class="leg-dot" style="background:#1c2128;border-color:#f0c040"></div> Загружается</div>
    <div class="leg"><div class="leg-dot" style="background:#1c2128;border-color:#3fb950"></div> Раскрыт</div>
    <div class="leg"><div class="leg-dot" style="background:#1c2128;border-color:#f85149"></div> Закрытый</div>
    <div class="leg"><div style="width:32px;height:2px;background:rgba(240,192,64,.55);border-radius:1px"></div> Замкнутая цепь</div>
  </div>

  <div id="sidebar">
    <div id="ninfo">
      <div id="nph">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5">
          <circle cx="12" cy="8" r="4"/><path d="M6 20v-1a6 6 0 0 1 12 0v1"/>
        </svg>
        Кликни на узел<br>чтобы увидеть информацию
      </div>
      <div id="ncontent"></div>
    </div>
  </div>
</div>

<!-- CONTROLS -->
<div id="ctrls">
  <button class="cb" id="c-in"  title="Увеличить" style="font-size:20px;font-weight:300">+</button>
  <button class="cb" id="c-out" title="Уменьшить"  style="font-size:20px;font-weight:300">&#8722;</button>
  <div class="cb-sep"></div>
  <button class="cb" id="c-fit" title="Вписать">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
    </svg>
  </button>
</div>

<!-- IMPORT PANEL -->
<div id="imp">
  <div id="imp-head">
    <span>&#9889; Авто-импорт</span>
    <button id="imp-min">&#8722;</button>
  </div>
  <div id="imp-body">
    <div class="imp-prog">
      <div class="imp-track"><div class="imp-bar" id="imp-bar"></div></div>
      <span class="imp-pct" id="imp-pct">0%</span>
    </div>
    <div id="imp-status"></div>

    <!-- countdown ring (shown between batches) -->
    <div id="imp-cd">
      <div id="imp-cd-ring">
        <svg viewBox="0 0 52 52" width="52" height="52">
          <circle class="cd-bg" cx="26" cy="26" r="23"/>
          <circle class="cd-arc" id="cd-arc" cx="26" cy="26" r="23"
            stroke-dasharray="144.51" stroke-dashoffset="0"/>
        </svg>
        <div id="imp-cd-num">0</div>
      </div>
      <div id="imp-cd-lbl">секунд до следующего<br>уровня</div>
    </div>

    <div class="imp-btns">
      <button id="imp-pause">Пауза</button>
      <button id="imp-stop">Стоп</button>
    </div>
    <div class="imp-sliders">
      <div class="sl-row">
        <div class="sl-label"><span>Задержка между узлами</span><b id="nd-val">1.0 с</b></div>
        <input type="range" id="nd-sl" min="300" max="4000" step="100" value="1000">
      </div>
      <div class="sl-row">
        <div class="sl-label"><span>Пауза между уровнями</span><b id="bp-val">5 с</b></div>
        <input type="range" id="bp-sl" min="2000" max="20000" step="1000" value="5000">
      </div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
// ── Avatar helpers ────────────────────────────────────────────────
var COLORS = ['#5b6af5','#e85d8a','#3db87a','#e8a83b','#9b5de5','#3bb8d9','#e87c3b','#3b8de8'];
function aColor(name) {
  return COLORS[name.split('').reduce(function(s,c){return s+c.charCodeAt(0);},0) % COLORS.length];
}
function aLetter(name) { return (name||'?')[0].toUpperCase(); }
function aStyle(name) { return 'background:' + aColor(name); }
function mkSvg(name) {
  var c = aColor(name), l = aLetter(name);
  return 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    + '<circle cx="50" cy="50" r="50" fill="' + c + '"/>'
    + '<text x="50" y="50" dy=".35em" text-anchor="middle" font-size="44"'
    + ' font-family="Arial,sans-serif" fill="white">' + l + '</text></svg>'
  )));
}
function sleep(ms) { return new Promise(function(r){ setTimeout(r, ms); }); }

// ── Graph state ───────────────────────────────────────────────────
var gNodes   = new vis.DataSet();
var gEdges   = new vis.DataSet();
var nodeMap  = {};   // id -> {id,name,vk_url}
var stateMap = {};   // id -> 'new'|'loading'|'expanded'|'private'
var edgeSet  = new Set();
var edgeCtr  = 0;

// layout: nodeId -> {depth}
// depthReg[depth] = [ids at this depth, in order added]
var layoutInfo = {};
var depthReg   = {};   // depth -> [nodeId, ...]
var LEVEL_R    = 380;  // base radius per depth level (px)
var NODE_GAP   = 70;   // min arc-gap between node centers (px)

// ── Network (physics OFF) ─────────────────────────────────────────
var net = new vis.Network(
  document.getElementById('network'),
  { nodes: gNodes, edges: gEdges },
  {
    physics: { enabled: false },
    interaction: { hover: true, tooltipDelay: 150, hideEdgesOnDrag: true },
    layout:      { improvedLayout: false },
  }
);

// ── Node helpers ──────────────────────────────────────────────────
function bCol(state) {
  return {new:'#30363d',loading:'#f0c040',expanded:'#3fb950',private:'#f85149'}[state]||'#30363d';
}
function mkNode(user, state, isCenter) {
  return {
    id: user.id, label: user.name.split(' ')[0],
    image: mkSvg(user.name), shape: 'circularImage',
    size: isCenter ? 28 : 15,
    borderWidth: isCenter ? 3.5 : 1.5, borderWidthSelected: 4,
    color: {border:bCol(state||'new'), background:'#1c2128',
      highlight:{border:'#7ba4ff',background:'#252c3a'},
      hover:{border:'#7ba4ff',background:'#1c2128'}},
    font: {color:'#e6edf3', size:isCenter?12:10, face:'system-ui,sans-serif', vadjust:2},
  };
}
function setNodeState(id, state) {
  stateMap[id] = state;
  gNodes.update({id:id, borderWidth:state==='expanded'?2.5:1.5,
    color:{border:bCol(state),background:'#1c2128',
      highlight:{border:'#7ba4ff',background:'#252c3a'},
      hover:{border:'#7ba4ff',background:'#1c2128'}}});
}
function addUser(user, isCenter) {
  if (nodeMap[user.id]) return;
  nodeMap[user.id] = {id:user.id, name:user.name, vk_url:'https://vk.com/id'+user.id};
  stateMap[user.id] = 'new';
  var n = mkNode(user, 'new', isCenter);
  n.x = 0; n.y = 0;
  gNodes.add(n);
  updateStats();
}
function addEdge(a, b, isCycle) {
  var key = Math.min(a,b)+'_'+Math.max(a,b);
  if (edgeSet.has(key)) return;
  edgeSet.add(key);
  gEdges.add({id:edgeCtr++,from:a,to:b,
    color:isCycle?{color:'rgba(240,192,64,.55)',highlight:'rgba(240,192,64,.9)'}
                 :{color:'rgba(48,54,61,.85)',  highlight:'rgba(91,138,245,.85)'},
    width:isCycle?1.5:1, dashes:isCycle?[4,3]:false, smooth:{type:'dynamic'}});
  updateStats();
}

// ── Layout: position all nodes at a depth in an even circle ───────
function layoutDepth(depth) {
  var ids = depthReg[depth];
  if (!ids || !ids.length) return;
  var n = ids.length;
  // Radius = enough for NODE_GAP between adjacent nodes
  var r = Math.max(depth * LEVEL_R, n * NODE_GAP / (2 * Math.PI));
  ids.forEach(function(id, i) {
    var angle = (i / n) * 2 * Math.PI - Math.PI / 2;
    gNodes.update({id:id, x:Math.cos(angle)*r, y:Math.sin(angle)*r});
  });
}

function registerNode(id, depth) {
  layoutInfo[id] = {depth: depth};
  if (!depthReg[depth]) depthReg[depth] = [];
  if (depthReg[depth].indexOf(id) < 0) depthReg[depth].push(id);
}

// ── Batch-by-depth queue ──────────────────────────────────────────
var batches     = {};    // depth -> [nodeIds to expand]
var batchRunning = false;
var autoPaused   = false;
var nodeDelay    = 1000;  // ms between single node expansions
var batchPause   = 5000;  // ms between depth levels
var totalInBatch = 0;
var doneInBatch  = 0;

function addToBatch(ids, depth) {
  if (!batches[depth]) batches[depth] = [];
  var b = batches[depth];
  ids.forEach(function(id) {
    if (stateMap[id] === 'new' && b.indexOf(id) < 0) b.push(id);
  });
}

async function processBatch(depth) {
  if (batchRunning) return;

  var ids = batches[depth] || [];
  delete batches[depth];
  if (!ids.length) return;

  batchRunning = true;
  totalInBatch = ids.length;
  doneInBatch  = 0;
  refreshPanel(depth, false);

  for (var i = 0; i < ids.length; i++) {
    if (autoPaused) {
      // Save remaining back to batch
      batches[depth] = ids.slice(i);
      batchRunning = false;
      refreshPanel(depth, false);
      return;
    }
    var id = ids[i];
    if (stateMap[id] !== 'new') { doneInBatch++; refreshPanel(depth, false); continue; }
    await expandNode(id, true);
    doneInBatch++;
    refreshPanel(depth, false);
    if (i < ids.length - 1) await sleep(nodeDelay);
  }

  batchRunning = false;

  // Reposition all newly collected depth+1 nodes in a proper circle
  if (depthReg[depth + 1] && depthReg[depth + 1].length) {
    layoutDepth(depth + 1);
  }

  var nextIds = batches[depth + 1];
  if (nextIds && nextIds.length && !autoPaused) {
    // Countdown
    await countdown(batchPause, depth + 1);
    if (!autoPaused) processBatch(depth + 1);
  } else {
    // All done
    document.getElementById('imp-cd').classList.remove('on');
    document.getElementById('imp-status').textContent = 'Готово — ' + gNodes.length + ' узлов в графе';
    setTimeout(function() {
      if (!batchRunning) document.getElementById('imp').classList.remove('on');
    }, 5000);
  }
}

async function countdown(ms, nextDepth) {
  var cdDiv  = document.getElementById('imp-cd');
  var cdNum  = document.getElementById('imp-cd-num');
  var cdLbl  = document.getElementById('imp-cd-lbl');
  var cdArc  = document.getElementById('cd-arc');
  var circum = 144.51;
  cdLbl.textContent = 'уровень ' + nextDepth + ' через';
  cdDiv.classList.add('on');
  document.getElementById('imp-status').textContent = 'Ожидание перед уровнем ' + nextDepth + '...';

  var start = Date.now();
  while (true) {
    if (autoPaused) {
      cdDiv.classList.remove('on');
      return;
    }
    var elapsed  = Date.now() - start;
    var remaining = Math.max(0, ms - elapsed);
    var secs = Math.ceil(remaining / 1000);
    cdNum.textContent = secs;
    cdArc.style.strokeDashoffset = circum * (1 - elapsed / ms);
    if (remaining <= 0) break;
    await sleep(100);
  }
  cdDiv.classList.remove('on');
}

function refreshPanel(depth, counting) {
  var pct = totalInBatch > 0 ? Math.round(doneInBatch / totalInBatch * 100) : 0;
  document.getElementById('imp-bar').style.width = pct + '%';
  document.getElementById('imp-pct').textContent = pct + '%';
  if (!counting) {
    document.getElementById('imp-status').textContent = autoPaused
      ? 'Пауза — ' + doneInBatch + ' из ' + totalInBatch
      : 'Уровень ' + depth + ' — ' + doneInBatch + ' / ' + totalInBatch;
  }
}

// ── Expand a single node ──────────────────────────────────────────
async function expandNode(userId, silent) {
  if (stateMap[userId]==='loading'||stateMap[userId]==='expanded'||stateMap[userId]==='private') return;
  setNodeState(userId, 'loading');
  updateSidebar(userId);

  var resp = await fetch('/api/friends?id=' + userId);
  var data = await resp.json();

  if (data.error === 'private') {
    setNodeState(userId, 'private');
    updateSidebar(userId);
    if (!silent) toast('Профиль закрытый');
    return;
  }

  var friends = data.friends || [];
  var myDepth  = layoutInfo[userId] ? layoutInfo[userId].depth : 0;
  var childDepth = myDepth + 1;
  var newIds = [];

  friends.forEach(function(f) {
    var isNew = !nodeMap[f.id];
    if (isNew) {
      addUser(f, false);
      registerNode(f.id, childDepth);
      newIds.push(f.id);
    }
    addEdge(userId, f.id, !isNew);
  });

  // Temporary positions: scatter in the child depth ring
  newIds.forEach(function(id, i) {
    var angle = Math.random() * 2 * Math.PI;
    var r = childDepth * LEVEL_R;
    gNodes.update({id:id, x:Math.cos(angle)*r, y:Math.sin(angle)*r});
  });

  setNodeState(userId, 'expanded');
  updateSidebar(userId);
  if (!silent) toast('Загружено ' + friends.length + ' друзей');

  // Register new nodes in the next batch (not processed yet)
  if (newIds.length) addToBatch(newIds, childDepth);
}

// ── Sidebar ───────────────────────────────────────────────────────
var selId = null;
var nph   = document.getElementById('nph');
var ncc   = document.getElementById('ncontent');

function updateSidebar(id) { if (selId === id) renderSidebar(id); }

function renderSidebar(id) {
  var u = nodeMap[id]; if (!u) return;
  var st  = stateMap[id];
  var dep = layoutInfo[id] ? layoutInfo[id].depth : '?';
  var con = net.getConnectedNodes(id).map(function(i){return nodeMap[i];}).filter(Boolean);
  var stL = {new:'Не раскрыт',loading:'Загружается...',expanded:'Раскрыт',private:'Закрытый'}[st]||'';
  var bCl = st==='expanded'?'expanded':st==='loading'?'loading':'';
  var bTx = st==='expanded'?'Друзья загружены':st==='loading'?'Загружаю...':st==='private'?'Закрытый профиль':'Раскрыть друзей';
  var bDi = (st==='expanded'||st==='loading'||st==='private')?'disabled':'';

  ncc.innerHTML =
    '<div class="np"><div class="np-ava" style="' + aStyle(u.name) + '">' + aLetter(u.name) + '</div>'
    + '<div><div class="np-name">' + u.name + '</div>'
    + '<div class="np-sub">id' + u.id + ' &middot; уровень ' + dep + ' &middot; ' + stL + '</div></div></div>'
    + '<div class="n-actions">'
    + '<button class="btn-exp ' + bCl + '" id="btn-exp" ' + bDi + '>' + bTx + '</button>'
    + '<a href="' + u.vk_url + '" target="_blank" rel="noopener" class="vk-btn">'
    + '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M15.07 2H8.93C3.33 2 2 3.33 2 8.93v6.14C2 20.67 3.33 22 8.93 22h6.14C20.67 22 22 20.67 22 15.07V8.93C22 3.33 20.67 2 15.07 2zm2.08 13.5h-1.7c-.64 0-.84-.51-1.99-1.67-1-.98-1.44-.98-1.69-.98-.34 0-.44.1-.44.59v1.52c0 .42-.13.67-1.23.67-1.82 0-3.83-1.1-5.24-3.16C3.27 10.47 3 9 3 8.47c0-.25.1-.49.59-.49h1.7c.44 0 .61.2.78.67.86 2.47 2.3 4.64 2.9 4.64.22 0 .32-.1.32-.66v-2.57c-.07-1.19-.69-1.29-.69-1.72 0-.2.17-.41.44-.41h2.67c.37 0 .5.2.5.63v3.47c0 .37.17.5.27.5.22 0 .41-.13.83-.54 1.28-1.43 2.2-3.64 2.2-3.64.12-.25.32-.49.76-.49h1.7c.51 0 .62.26.51.62-.22.98-2.35 4.02-2.35 4.02-.19.31-.25.44 0 .78.19.27.8.83 1.2 1.33.75.88 1.32 1.62 1.47 2.13.17.51-.08.77-.59.77z"/></svg>'
    + 'Открыть профиль</a></div>'
    + (con.length
      ? '<div class="ml-title">Связи в графе (' + con.length + ')</div><div class="ml">'
        + con.map(function(m){
            return '<div class="mi" data-id="'+m.id+'">'
              +'<div class="mi-ava" style="'+aStyle(m.name)+'">'+aLetter(m.name)+'</div>'
              +'<div class="mi-name">'+m.name+'</div>'
              +'<div class="mi-ok">'+(stateMap[m.id]==='expanded'?'&#10003;':'')+'</div></div>';
          }).join('') + '</div>'
      : '');

  nph.style.display = 'none';
  ncc.classList.add('on');
  var btnE = document.getElementById('btn-exp');
  if (btnE) btnE.addEventListener('click', function(){ expandNode(id, false); });
  ncc.querySelectorAll('.mi').forEach(function(el){
    el.addEventListener('click', function(){ focusNode(parseInt(el.dataset.id)); });
  });
}

net.on('click', function(p) {
  if (!p.nodes.length) {
    selId = null; nph.style.display=''; ncc.classList.remove('on'); return;
  }
  selId = p.nodes[0]; renderSidebar(selId); expandNode(selId, false);
});

function focusNode(id) {
  if (!nodeMap[id]) return;
  net.selectNodes([id]);
  net.focus(id, {scale:1.3, animation:{duration:400,easingFunction:'easeInOutCubic'}});
  selId = id; renderSidebar(id);
}

// ── Start with user ───────────────────────────────────────────────
async function startWithUser(query) {
  var errEl = document.getElementById('wc-err');
  var btn   = document.getElementById('wc-btn');
  btn.disabled = true; btn.textContent = 'Ищу...'; errEl.textContent = '';

  var resp = await fetch('/api/user?q=' + encodeURIComponent(query));
  var user = await resp.json();
  btn.disabled = false; btn.textContent = 'Открыть';
  if (user.error) { errEl.textContent = 'Пользователь не найден'; return; }

  document.getElementById('welcome').classList.add('gone');
  document.getElementById('h-center').textContent = user.name;

  addUser(user, true);
  registerNode(user.id, 0);
  gNodes.update({id:user.id, x:0, y:0});

  // Load root's friends manually (depth-1 bootstrap)
  setNodeState(user.id, 'loading');
  var r2 = await fetch('/api/friends?id=' + user.id);
  var d2 = await r2.json();
  var friends = d2.friends || [];
  var depth1ids = [];

  friends.forEach(function(f) {
    var isNew = !nodeMap[f.id];
    if (isNew) { addUser(f, false); registerNode(f.id, 1); depth1ids.push(f.id); }
    addEdge(user.id, f.id, !isNew);
  });

  setNodeState(user.id, 'expanded');

  // Immediately layout depth-1 in a proper circle
  layoutDepth(1);
  net.fit({animation:{duration:700}});
  toast('Загружено ' + friends.length + ' друзей (уровень 1)');

  // Queue depth-1 nodes for batch processing
  if (depth1ids.length) {
    addToBatch(depth1ids, 1);
    document.getElementById('imp').classList.add('on');
    refreshPanel(1, false);
    await sleep(800);
    processBatch(1);
  }
}

document.getElementById('wc-btn').addEventListener('click', function(){
  var q = document.getElementById('wc-input').value.trim(); if(q) startWithUser(q);
});
document.getElementById('wc-input').addEventListener('keydown', function(e){
  if(e.key==='Enter'){var q=e.target.value.trim(); if(q) startWithUser(q);}
});

// ── Import panel controls ─────────────────────────────────────────
document.getElementById('imp-min').addEventListener('click', function(){
  document.getElementById('imp').classList.toggle('collapsed');
  this.innerHTML = document.getElementById('imp').classList.contains('collapsed') ? '&#43;' : '&#8722;';
});
document.getElementById('imp-pause').addEventListener('click', function(){
  autoPaused = !autoPaused;
  this.classList.toggle('paused', autoPaused);
  this.textContent = autoPaused ? 'Продолжить' : 'Пауза';
  if (!autoPaused && !batchRunning) {
    // Resume: find the current depth batch and continue
    var depths = Object.keys(batches).map(Number).sort(function(a,b){return a-b;});
    if (depths.length) processBatch(depths[0]);
  }
});
document.getElementById('imp-stop').addEventListener('click', function(){
  autoPaused = true; batchRunning = false;
  batches = {}; totalInBatch = 0; doneInBatch = 0;
  document.getElementById('imp').classList.remove('on');
  document.getElementById('imp-cd').classList.remove('on');
});

document.getElementById('nd-sl').addEventListener('input', function(){
  nodeDelay = parseInt(this.value);
  document.getElementById('nd-val').textContent = (nodeDelay/1000).toFixed(1) + ' с';
});
document.getElementById('bp-sl').addEventListener('input', function(){
  batchPause = parseInt(this.value);
  document.getElementById('bp-val').textContent = (batchPause/1000).toFixed(0) + ' с';
});

// ── Search ────────────────────────────────────────────────────────
var hS = document.getElementById('h-search'), hD = document.getElementById('h-sdrop');
hS.addEventListener('input', function(){
  var q = this.value.trim().toLowerCase();
  if (!q) { hD.classList.remove('on'); hD.innerHTML=''; return; }
  var hits = Object.values(nodeMap).filter(function(u){return u.name.toLowerCase().indexOf(q)>=0;}).slice(0,9);
  if (!hits.length) { hD.classList.remove('on'); return; }
  hD.innerHTML = hits.map(function(u){
    return '<div class="hsi" data-id="'+u.id+'">'
      +'<div class="hsi-ava" style="'+aStyle(u.name)+'">'+aLetter(u.name)+'</div>'
      +'<div><div class="hsi-name">'+u.name+'</div>'
      +'<div class="hsi-sub">'+(stateMap[u.id]==='expanded'?'раскрыт':'не раскрыт')+'</div></div></div>';
  }).join('');
  hD.classList.add('on');
  hD.querySelectorAll('.hsi').forEach(function(el){
    el.addEventListener('click', function(){ hD.classList.remove('on'); hS.value=''; focusNode(parseInt(el.dataset.id)); });
  });
});
hS.addEventListener('blur', function(){ setTimeout(function(){hD.classList.remove('on');},200); });

// ── Controls ──────────────────────────────────────────────────────
document.getElementById('c-in') .addEventListener('click', function(){ net.moveTo({scale:net.getScale()*1.3,animation:{duration:200}}); });
document.getElementById('c-out').addEventListener('click', function(){ net.moveTo({scale:net.getScale()*.77,animation:{duration:200}}); });
document.getElementById('c-fit').addEventListener('click', function(){ net.fit({animation:{duration:400}}); });

// ── Stats & toast ─────────────────────────────────────────────────
function updateStats(){
  document.getElementById('st-nodes').textContent = gNodes.length;
  document.getElementById('st-edges').textContent = gEdges.length;
}
var _tt;
function toast(msg){
  var el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(_tt); _tt=setTimeout(function(){el.classList.remove('show');},3000);
}
</script>
</body>
</html>
"""


def main():
    global TOKEN
    parser = argparse.ArgumentParser(description="VK Network Explorer")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    TOKEN = load_token()
    load_disk_cache()

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Сервер запущен: {url}")
    print("Нажми Ctrl+C чтобы остановить.")

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")


if __name__ == "__main__":
    main()
