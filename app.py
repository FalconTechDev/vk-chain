#!/usr/bin/env python3
"""vk-chain as a desktop application.

The interface is `ui/`, opened in a native window; this file is the `Api`
class behind it, as the UI's own README lays out (variant A). Every method
here is what the corresponding button in the window calls, and each one is a
thin wrapper over the routines in `vk_chain.py` - the token handling, rate
limiting and `execute` batching are already correct there and are not
reimplemented.

Long searches report progress back into the page by calling
`window.vkChainProgress(event)`, the same event shapes the README describes
for the HTTP variant, so the two back ends stay interchangeable:

    {"type": "front",  "side": "A", "level": 1, "checked": 1240}
    {"type": "meet",   "via": 3291847}
    {"type": "result", "paths": [[...]], "profiles": {...}, "edges": [[a, b]]}
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque

import webview

import vk_chain as vc

__version__ = "2.1.0"

# Two places, kept apart. RES is where the program's own files live - the
# interface - and is read-only once installed. DATA is where it keeps what
# it learns: caches, history, settings. Run from a checkout they are the
# same folder; run from the installed program, DATA is under %APPDATA%,
# because the install folder is not somewhere a normal user may write.
FROZEN = bool(getattr(sys, "frozen", False))
RES = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parent))
if os.environ.get("VKCHAIN_DATA"):
    # An explicit override: how the build checks the program it just made
    # without leaving its test data in a real user's folder.
    DATA = pathlib.Path(os.environ["VKCHAIN_DATA"])
elif FROZEN:
    DATA = pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home()) / "vk-chain"
else:
    DATA = pathlib.Path(__file__).resolve().parent
DATA.mkdir(parents=True, exist_ok=True)
# vk_chain.py addresses its caches by bare file name, relative to the working
# directory, so the working directory has to be the data folder.
os.chdir(DATA)

LOG = DATA / "app.log"
UI = RES / "ui" / "source" / "vk-chain Start v4.dc.html"
PROFILES = DATA / "profiles.json"
SETTINGS = DATA / "settings.json"
TOKEN_FILE = pathlib.Path.home() / ".vk_chain_token"

# The VK application the token is issued for. It is a public identifier, not a
# secret; to use another application, put its id in settings.json as
# "client_id".
CLIENT_ID = "54574312"


def token_url(client_id: str = CLIENT_ID) -> str:
    return (
        "https://oauth.vk.ru/authorize?client_id=" + str(client_id)
        + "&display=page&redirect_uri=https://oauth.vk.ru/blank.html"
        + "&scope=2&response_type=token&v=5.131"
    )


def _setup_logging() -> None:
    """Gives the program somewhere to print to, in UTF-8.

    Two things make this necessary rather than tidy. Under `pythonw` there is
    no console at all and `sys.stdout` is None, so any `print` in the code
    this calls raises. And when there *is* a console, Windows gives it a
    code page that cannot encode the arrows vk_chain prints in its progress
    line - which killed the search thread with a UnicodeEncodeError and left
    the window waiting for a result that was never coming.
    """
    # The log grows by every progress line; start over when it gets large
    # instead of letting it fill the disk.
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.unlink()
    except OSError:
        pass
    stream = open(LOG, "a", encoding="utf-8", buffering=1, errors="replace")
    stream.write("\n--- запуск " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---\n")
    sys.stdout = stream
    sys.stderr = stream


def _guard(what: str, fn, on_error) -> None:
    """Runs a worker so that a failure reaches the window, not just the log.

    SystemExit is caught too: vk_chain calls sys.exit() when there is no
    token, and in a worker thread that would end it without a word.
    """
    try:
        fn()
    except (Exception, SystemExit) as e:
        traceback.print_exc()
        on_error(f"{what}: {e}" if str(e) and str(e) != "1" else f"{what}: сбой, подробности в app.log")


DISPLAY_MODES = ("maximized", "window", "borderless", "fullscreen")
WINDOW_SIZES = ((1280, 800), (1440, 900), (1600, 900), (1920, 1080))


def _screen_size() -> tuple[int, int]:
    """The primary screen in the same logical pixels create_window uses."""
    try:
        sc = webview.screens[0]
        return int(sc.width), int(sc.height)
    except Exception:
        return 1536, 960


def _display_args(settings: dict) -> dict:
    """What create_window needs to open the way the person last chose.

    The default is a maximised window. It used to be a fixed 1440x900 at the
    system's chosen position, which on a screen smaller than that after DPI
    scaling put the window half off the edge, in a corner.
    """
    d = settings.get("display") or {}
    mode = d.get("mode") if d.get("mode") in DISPLAY_MODES else "maximized"
    if mode == "fullscreen":
        return {"fullscreen": True}
    if mode == "borderless":
        return {"frameless": True, "maximized": True}
    if mode == "maximized":
        return {"maximized": True}
    sw, sh = _screen_size()
    saved = settings.get("_window") or {}
    w = int(saved.get("width") or d.get("w") or 1440)
    h = int(saved.get("height") or d.get("h") or 900)
    w, h = max(960, min(w, sw)), max(640, min(h, sh))
    return {"width": w, "height": h, "x": (sw - w) // 2, "y": (sh - h) // 2}


def _style_titlebar(window) -> None:
    """Makes the title bar match the interface instead of the system accent.

    On a machine with a purple accent colour the frame was a bright purple
    stripe over a nearly black window. Windows 11 lets the caption, its text
    and the border be coloured; Windows 10 only offers a dark mode. Either
    failing is harmless, so nothing here is allowed to raise.
    """
    try:
        import ctypes
        hwnd = int(window.native.Handle.ToInt64())
        dwm = ctypes.windll.dwmapi

        def attr(code: int, value: int) -> None:
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, code, ctypes.byref(v), 4)

        attr(20, 1)            # dark mode
        attr(35, 0x000E0C0B)   # caption   #0b0c0e  (COLORREF is 0x00BBGGRR)
        attr(36, 0x00DFE6E6)   # text      #e6e6df
        attr(34, 0x00312A26)   # border    #262a31
    except Exception:
        pass


def _set_frame(window, framed: bool) -> None:
    """Shows or hides the window frame while it is open.

    pywebview only takes `frameless` when a window is created, so for a change
    at run time the form's own border style is changed, on the thread that
    owns it.
    """
    try:
        import clr  # noqa: F401  (pythonnet, already loaded by pywebview)
        from System import Action
        from System.Windows.Forms import FormBorderStyle
        form = window.native

        def apply():
            form.FormBorderStyle = FormBorderStyle.Sizable if framed else getattr(FormBorderStyle, "None")

        form.Invoke(Action(apply))
    except Exception:
        traceback.print_exc()


def _scale() -> float:
    """The monitor's scale factor, the way pywebview reads it.

    Not Form.DeviceDpi: the process is not DPI-aware, so that says 96 on a
    125 % screen, and a window sized with it came out a fifth too small.
    """
    try:
        import ctypes
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0 or 1.0
    except Exception:
        return 1.0


def _place(window, width: int, height: int, x: int, y: int) -> bool:
    """Puts a window at an ordinary size and position, in one step.

    Done on the form itself because pywebview's own restore() did not take
    effect on a window just made borderless or maximised, and its resize() put
    a restored window straight back to maximised. Sizes here are the same
    logical pixels create_window uses; the form wants device pixels.
    """
    try:
        import clr  # noqa: F401
        from System import Action
        from System.Drawing import Point, Size
        from System.Windows.Forms import FormWindowState
        form = window.native

        def apply():
            k = _scale()
            form.WindowState = FormWindowState.Normal
            form.Size = Size(int(width * k), int(height * k))
            form.Location = Point(int(x * k), int(y * k))

        form.Invoke(Action(apply))
        return True
    except Exception:
        traceback.print_exc()
        return False


def _register_toast_identity() -> None:
    """Gives notifications a name and an icon.

    An unpackaged program has no identity of its own, and Windows then shows
    its toasts under the name of whatever process sent them. A per-user
    registry entry fixes that without administrator rights. The installer
    writes the same entry; this covers running from source.
    """
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\vk-chain")
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "vk-chain")
        icon = RES / "assets" / "logo.png"
        if not icon.exists():
            icon = pathlib.Path(__file__).resolve().parent / "assets" / "logo.png"
        if icon.exists():
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
        winreg.CloseKey(key)
    except Exception:
        traceback.print_exc()


_TOAST = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$t = [System.Security.SecurityElement]::Escape($env:VKC_TITLE)
$m = [System.Security.SecurityElement]::Escape($env:VKC_MSG)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('vk-chain').Show($toast)
"""


def _toast(title: str, message: str) -> bool:
    """Shows a Windows notification. The text goes in through the environment
    rather than the command line, so quotes and angle brackets in a person's
    name cannot break the script or inject into it."""
    if os.name != "nt":
        return False
    _register_toast_identity()
    env = dict(os.environ, VKC_TITLE=title, VKC_MSG=message)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST],
            env=env, timeout=25, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            print("toast failed:", r.stderr.decode("utf-8", "replace")[:300])
        return r.returncode == 0
    except Exception:
        traceback.print_exc()
        return False


# When VK refuses with error 29 ("Rate limit reached") the time is kept here.
_LIMIT = {"at": 0.0}

LIMIT_MESSAGE = (
    "VK ограничил запросы списка друзей для этого токена (ошибка 29, Rate limit reached). "
    "Обычно проходит за сутки. Поиск по уже сохранённому кэшу работает."
)


def _watch_vk_limits() -> None:
    """Notices VK's rate-limit refusal, which vk_chain does not report.

    friends.get is called through execute, and a refusal comes back as a
    well-formed answer whose entries are null, with the reason in a separate
    `execute_errors` list. vk_chain reads only the answer, so a refused person
    looks exactly like someone whose friends are hidden. Watching the replies
    as they pass is enough to tell the two apart.
    """
    session = vc._session
    if getattr(session, "_vkc_watched", False):
        return
    original = session.post

    def post(*args, **kwargs):
        reply = original(*args, **kwargs)
        try:
            body = reply.json()
            codes = [e.get("error_code") for e in (body.get("execute_errors") or [])]
            codes.append((body.get("error") or {}).get("error_code"))
            if 29 in codes:
                _LIMIT["at"] = time.time()
        except Exception:
            pass
        return reply

    session.post = post
    session._vkc_watched = True


class Api:
    """What the page can call. Every method returns JSON-safe values."""

    def __init__(self):
        self._window = None
        self._cancel = threading.Event()
        self._loaded = False
        self._me = None
        self._me_token = None
        self._fullscreen = False
        self._started = 0.0
        _watch_vk_limits()

    # -- plumbing ---------------------------------------------------------

    def bind(self, window):
        self._window = window

    def _emit(self, event: dict) -> None:
        """Pushes one progress event into the page."""
        if not self._window:
            return
        payload = json.dumps(event, ensure_ascii=False)
        try:
            self._window.evaluate_js(
                f"window.vkChainProgress && window.vkChainProgress({payload})"
            )
        except Exception:
            # The window can go away mid-search; that is not an error worth
            # stopping the search for.
            pass

    def _run(self, what: str, fn) -> None:
        """Starts a worker on its own thread, guarded.

        If VK refused anything while it ran, the people fetched during that
        time and found "empty" are forgotten again. vk_chain caches an empty
        list as a fact, so a refusal would otherwise turn into "this person
        has no friends" permanently, and they would never be asked again.
        """
        def job() -> None:
            self._started = t0 = time.time()
            before = set(vc._friends_cache)
            try:
                fn()
            finally:
                if _LIMIT["at"] >= t0:
                    for k in [k for k in vc._friends_cache if k not in before and not vc._friends_cache[k]]:
                        vc._friends_cache.pop(k, None)
                    try:
                        vc.save_caches()
                    except Exception:
                        traceback.print_exc()

        threading.Thread(
            target=_guard,
            args=(what, job, lambda m: self._emit({"type": "error", "message": m})),
            daemon=True,
        ).start()

    def _limited(self) -> bool:
        """Did VK refuse for rate reasons since this search began?"""
        return _LIMIT["at"] >= self._started > 0

    def _apply_opts(self, opts: dict) -> None:
        """Options that change how requests are made, not what is asked."""
        try:
            delay = float(opts.get("api_delay", 0))
            if 0.05 <= delay <= 5:
                vc.DELAY = delay
        except (TypeError, ValueError):
            pass

    def _ensure_caches(self) -> None:
        if not self._loaded:
            vc.load_caches()
            self._loaded = True

    def _token(self) -> str:
        if not TOKEN_FILE.exists():
            raise RuntimeError("нет токена: открой настройки и вставь токен VK")
        return vc.load_token(None)

    # -- settings, token and cache ------------------------------------------

    def get_settings(self) -> dict:
        """What the interface saved last time, or {} on a first run."""
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save_settings(self, values: dict) -> dict:
        """Stores the interface's settings, keeping the window's own entry."""
        try:
            current = self.get_settings()
            window = current.get("_window")
            current = dict(values or {})
            if window:
                current["_window"] = window
            SETTINGS.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def status(self) -> dict:
        self._ensure_caches()
        cache = DATA / "friends_cache.json"
        profiles = self._profiles_on_disk()
        have_token = TOKEN_FILE.exists()
        token = ""
        if have_token:
            try:
                token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        hint = (token[:7] + "…" + token[-4:]) if len(token) > 14 else ""
        return {
            "version": __version__,
            "token_ok": bool(token),
            "token_hint": hint,
            "me": self._whoami(token),
            "scopes": "friends",
            "cache_profiles": len(profiles),
            "cache_relatives": len(vc._relatives_cache),
            "cache_friends": len(vc._friends_cache),
            "cache_bytes": sum(
                f.stat().st_size for f in DATA.glob("*_cache.json") if f.exists()
            ) + (PROFILES.stat().st_size if PROFILES.exists() else 0),
        }

    def _whoami(self, token: str):
        """The token's owner, asked once per token rather than on every status."""
        if not token:
            return None
        if self._me_token == token:
            return self._me
        self._me_token = token
        self._me = None
        try:
            resp = vc.api(token, "users.get")
            if resp and isinstance(resp, list):
                u = resp[0]
                self._me = {
                    "id": u["id"],
                    "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                }
        except Exception:
            traceback.print_exc()
        return self._me

    def _profiles_on_disk(self) -> dict:
        if not PROFILES.exists():
            return {}
        try:
            return json.loads(PROFILES.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def set_token(self, token: str) -> dict:
        """Saves a token after checking that VK accepts it.

        Takes the whole address VK redirects to as well as the bare token,
        since that address is what a person actually has in front of them.
        """
        raw = (token or "").strip()
        m = re.search(r"access_token=([^&\s]+)", raw)
        tok = m.group(1) if m else raw
        if len(tok) < 20:
            return {"ok": False, "error": "это не похоже на токен"}
        try:
            resp = vc.api(tok, "users.get")
        except Exception as e:
            return {"ok": False, "error": f"VK не ответил: {e}"}
        if not resp or not isinstance(resp, list):
            return {"ok": False, "error": "VK не принял этот токен"}
        TOKEN_FILE.write_text(tok, encoding="utf-8")
        self._me_token = None
        return {"ok": True}

    def open_token_page(self) -> dict:
        """Opens VK's sign-in page in the default browser."""
        client = self.get_settings().get("client_id") or CLIENT_ID
        webbrowser.open(token_url(client))
        return {"ok": True}

    def clear_cache(self) -> dict:
        for f in ("friends_cache.json", "relatives_cache.json", "profiles.json"):
            p = DATA / f
            if p.exists():
                p.unlink()
        vc._friends_cache.clear()
        vc._relatives_cache.clear()
        self._loaded = False
        return {"ok": True}

    def history(self) -> list:
        p = DATA / "history.json"
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []

    def _remember(self, kind: str, query: str, meta: str, args: dict | None = None) -> None:
        """Adds a search to the history, with what it took to repeat it.

        The label shown is made of names; the arguments are what was typed, so
        clicking an old entry can run the same search again.
        """
        p = DATA / "history.json"
        items = self.history()
        items.insert(0, {"type": kind, "query": query, "meta": meta, "args": args or {}, "ts": time.time()})
        p.write_text(json.dumps(items[:40], ensure_ascii=False, indent=1), encoding="utf-8")

    def set_display(self, mode: str, width: int = 0, height: int = 0) -> dict:
        """Switches the window between the four ways of showing it.

        window      a normal framed window of a chosen size, centred
        maximized   a framed window filling the screen
        borderless  no frame, filling the screen but not the taskbar
        fullscreen  no frame, covering everything

        What is applied is also what is remembered, so the next start opens
        the same way.
        """
        win = self._window
        if not win or mode not in DISPLAY_MODES:
            return {"ok": False, "error": "режим недоступен"}
        try:
            want_full = mode == "fullscreen"
            if self._fullscreen != want_full:
                win.toggle_fullscreen()
                self._fullscreen = want_full
            if want_full:
                return {"ok": True}
            _set_frame(win, mode != "borderless")
            if mode in ("maximized", "borderless"):
                win.maximize()
            else:
                sw, sh = _screen_size()
                w = max(960, min(int(width or 1440), sw))
                h = max(640, min(int(height or 900), sh))
                if not _place(win, w, h, (sw - w) // 2, (sh - h) // 2):
                    win.restore()
                    win.resize(w, h)
                    win.move((sw - w) // 2, (sh - h) // 2)
                current = self.get_settings()
                current["_window"] = {"width": w, "height": h}
                SETTINGS.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def toggle_fullscreen(self) -> dict:
        """F11: full screen on and off, without disturbing the chosen mode."""
        if self._window:
            self._window.toggle_fullscreen()
            self._fullscreen = not self._fullscreen
        return {"ok": True, "fullscreen": self._fullscreen}

    def notify(self, title: str, message: str) -> dict:
        """A desktop notification, for when a long search finishes."""
        threading.Thread(target=_toast, args=(str(title), str(message)), daemon=True).start()
        return {"ok": True}

    def cancel(self) -> dict:
        self._cancel.set()
        return {"ok": True}

    # -- the searches --------------------------------------------------------

    def chain(self, a: str, b: str, opts: dict | None = None) -> dict:
        """Starts a chain search. The answer arrives as progress events."""
        o = opts or {}
        self._run("цепочка", lambda: self._chain(a, b, o))
        return {"started": True}

    def _chain(self, a: str, b: str, opts: dict) -> None:
        self._cancel.clear()
        self._apply_opts(opts)
        self._ensure_caches()
        token = self._token()
        start = self._resolve(token, a)
        end = self._resolve(token, b)
        if not start or not end:
            self._emit({"type": "error", "message": "пользователь не найден"})
            return

        # vk_chain draws its progress with print(), one line per batch of 25
        # people. Taking that over is what gives the two fronts on screen
        # their real depth and a real count of people looked at, rather than
        # a number that only grows when a layer ends.
        done = {"A": 0, "B": 0}      # people in layers already finished
        last = {"A": (0, 0), "B": (0, 0)}  # (level, size of that layer)
        sent = [0.0]
        side = {"A→": "A", "←B": "B"}
        rx = re.compile(r"\[(A→|←B)\]\s*depth=(\d+)\s+(\d+)/(\d+)")

        def progress(*args, **kw):
            text = " ".join(str(x) for x in args)
            m = rx.search(text)
            if not m:
                # Anything else vk_chain prints still belongs in the log.
                print(*args, **{k: v for k, v in kw.items() if k != "end"})
                return
            name, level, got, total = side[m.group(1)], int(m.group(2)), int(m.group(3)), int(m.group(4))
            prev_level, prev_total = last[name]
            if level != prev_level:
                done[name] += prev_total
                last[name] = (level, total)
            now = time.time()
            if now - sent[0] < 0.12 and got < total:
                return
            sent[0] = now
            self._emit({
                "type": "front", "side": name, "level": level,
                "checked": done["A"] + done["B"] + got,
            })

        want = max(1, min(8, int(opts.get("paths", 1) or 1)))
        paths: list = []
        banned: set = set()
        vc.print = progress
        try:
            for k in range(want):
                if self._cancel.is_set():
                    break
                path = vc.find_chain(
                    token, start[0], end[0], banned,
                    include_relatives=bool(opts.get("include_relatives")),
                )
                if not path or path in paths:
                    break
                paths.append(path)
                # Each further route must avoid the people of the ones before
                # it, or it would simply find the same chain again.
                banned.update(set(path[1:-1]) - {start[0], end[0]})
                if len(path) <= 2:
                    break
        finally:
            del vc.print
            vc.save_caches()

        if not paths:
            self._emit({"type": "error", "message": LIMIT_MESSAGE if self._limited() else "путь не найден"})
            return
        first = paths[0]
        if len(first) > 2:
            self._emit({"type": "meet", "via": first[len(first) // 2]})

        # The path on its own is a line of five dots, which is not a graph
        # and not what the chain is for: the point is to see the path inside
        # the crowd it runs through. The neighbourhood comes from the cache
        # the search just filled, so it costs no further requests.
        cap = int(opts.get("max_nodes", 400))
        edges, around = [], []
        for p in paths:
            e, n = self._context(p, max(12, min(60, cap // (len(p) * len(paths) + 1))))
            edges += e
            around += n
        ids = list({i for p in paths for i in p} | set(around))
        profiles = self._profiles_for(token, ids)
        meta = f"{len(first) - 1} рукопожатий" + (f" · {len(paths)} пути" if len(paths) > 1 else "")
        self._remember("chain", f"{start[1]} → {end[1]}", meta, {"from": a, "to": b})
        self._emit({"type": "result", "paths": paths, "profiles": profiles, "edges": edges})

    def _context(self, path: list, cap: int = 60) -> tuple[list, list]:
        """The path plus everyone one step off it, from the cache alone.

        Capped per person: one hub with two thousand friends would otherwise
        bury the path it is supposed to sit in.
        """
        on_path = set(path)
        edges = [[path[i], path[i + 1]] for i in range(len(path) - 1)]
        near: set[int] = set()
        for person in path:
            friends = vc._friends_cache.get(person) or []
            # Keep the ones that tie back into the picture first: a friend of
            # two chain members says more than a friend of one.
            ranked = sorted(
                friends,
                key=lambda f: -sum(1 for q in path if f in (vc._friends_cache.get(q) or [])),
            )
            for f in ranked[:cap]:
                if f in on_path:
                    continue
                near.add(f)
                edges.append([person, f])
        # Links among the neighbours themselves, which is what makes it look
        # like a social graph rather than a set of stars.
        near_list = list(near)
        index = set(near_list)
        for f in near_list:
            for g in (vc._friends_cache.get(f) or []):
                if g in index and f < g:
                    edges.append([f, g])
        return edges, near_list

    def explore(self, center: str, opts: dict | None = None) -> dict:
        o = opts or {}
        self._run("граф", lambda: self._explore(center, o))
        return {"started": True}

    def _explore(self, center: str, opts: dict) -> None:
        """Someone's friends, and the friends of friends who matter most.

        The second ring is the whole difficulty: a person with 250 friends
        has forty thousand friends of friends, and drawing them all gives a
        grey disc. So the second ring is ranked by how many of the first ring
        each person knows - someone who knows twelve of your friends is part
        of your world, someone who knows one is not - and cut at the cap.
        """
        self._cancel.clear()
        self._apply_opts(opts)
        self._ensure_caches()
        token = self._token()
        who = self._resolve(token, center)
        if not who:
            self._emit({"type": "error", "message": "пользователь не найден"})
            return
        root = who[0]
        cap = max(60, int(opts.get("max_nodes", 400)))

        self._emit({"type": "front", "side": "A", "level": 1, "checked": 0})
        friends = vc.get_friends_batch(token, [root]).get(root, [])
        if not friends:
            self._emit({"type": "error", "message": LIMIT_MESSAGE if self._limited() else "друзья скрыты или их нет"})
            return
        level1 = friends[: int(cap * 0.65)]
        l1set = set(level1)
        self._emit({"type": "front", "side": "A", "level": 1, "checked": len(level1)})

        # Their friend lists: one request per 25 people, and none at all for
        # anyone already cached.
        self._emit({"type": "front", "side": "A", "level": 2, "checked": len(level1)})
        inner = vc.get_friends_batch(token, level1)
        score: dict[int, int] = {}
        for f in level1:
            for g in inner.get(f, []):
                if g != root and g not in l1set:
                    score[g] = score.get(g, 0) + 1
        room = max(0, cap - len(level1) - 1)
        level2 = [g for g, _ in sorted(score.items(), key=lambda kv: -kv[1])[:room]]
        keep = {root} | l1set | set(level2)
        self._emit({"type": "front", "side": "A", "level": 2, "checked": len(keep)})

        edges = [[root, f] for f in level1]
        for f in level1:
            for g in inner.get(f, []):
                if g in keep and g != root and f < g:
                    edges.append([f, g])
                elif g in keep and g != root and g not in l1set:
                    edges.append([f, g])
        # Links among the second ring come from the cache only: fetching them
        # would be another round of requests for a detail few people look at.
        l2set = set(level2)
        for g in level2:
            for h in (vc._friends_cache.get(g) or []):
                if h in l2set and g < h:
                    edges.append([g, h])
        vc.save_caches()
        self._remember("explore", who[1], f"{len(keep)} человек", {"center": center})
        self._emit({
            "type": "result", "paths": [], "root": root,
            "profiles": self._profiles_for(token, list(keep)),
            "edges": edges,
        })

    def relatives(self, user: str, opts: dict | None = None) -> dict:
        o = opts or {}
        self._run("родня", lambda: self._relatives(user, o))
        return {"started": True}

    def _relatives(self, user: str, opts: dict) -> None:
        """Namesakes among everyone's friends and everyone's friends of friends.

        This used to look at one person's friends and stop. Relatives are not
        found that way: they are the people your friends know, and your
        friends' friends know. So both rings are read in full - every
        friend's friend list, not one - and every candidate's surname is
        compared with the stem of the one being looked for.

        A person with 250 friends has some forty thousand friends of friends,
        and each name costs a share of a request. The pool is cut to the
        candidates the most of your friends know, which is also where
        relatives are: they sit in the same circles.
        """
        self._cancel.clear()
        self._apply_opts(opts)
        self._ensure_caches()
        token = self._token()
        who = self._resolve(token, user)
        if not who:
            self._emit({"type": "error", "message": "пользователь не найден"})
            return
        uid = who[0]
        info = vc.resolve_user(token, str(uid))
        surname = (opts.get("surname") or (info[3] if info else "")).strip()
        stems = {vc.get_surname_stem(w) for w in surname.split() if len(w) > 2}
        if not stems:
            self._emit({"type": "error", "message": "не удалось определить фамилию"})
            return
        deep = bool(opts.get("deep"))
        pool_cap = 20000 if deep else 4000
        node_cap = max(60, int(opts.get("max_nodes", 400)))

        # Ring one: all of their friends.
        self._emit({"type": "front", "side": "A", "level": 1, "checked": 0})
        friends = vc.get_friends_batch(token, [uid]).get(uid, [])
        if not friends:
            self._emit({"type": "error", "message": LIMIT_MESSAGE if self._limited() else "друзья скрыты или их нет"})
            return

        # Ring two: the friends of every one of those friends, a few dozen at
        # a time so the window can show it moving.
        inner: dict[int, list[int]] = {}
        for k in range(0, len(friends), 50):
            if self._cancel.is_set():
                return
            inner.update(vc.get_friends_batch(token, friends[k:k + 50]))
            self._emit({"type": "front", "side": "A", "level": 2, "checked": len(friends) + k + 50})

        known = set(friends) | {uid}
        score: dict[int, int] = {}
        for f in friends:
            for g in inner.get(f, []):
                if g not in known:
                    score[g] = score.get(g, 0) + 1
        ranked = [g for g, _ in sorted(score.items(), key=lambda kv: -kv[1])]
        pool = friends + ranked[: max(0, pool_cap - len(friends))]

        def progress(done: int, total: int) -> None:
            self._emit({"type": "front", "side": "A", "level": 3, "checked": done})

        self._emit({"type": "front", "side": "A", "level": 3, "checked": 0})
        names = self._names(token, pool, progress)

        def is_kin(pid: int) -> bool:
            p = names.get(pid) or {}
            for last in (p.get("last_name"), p.get("maiden_name")):
                if last and any(vc.is_stem_match(vc.get_surname_stem(last), s) for s in stems):
                    return True
            return False

        matched = [pid for pid in pool if pid != uid and is_kin(pid)]
        if not matched:
            self._emit({"type": "error", "message": f"однофамильцев «{surname}» среди друзей и друзей друзей не найдено"})
            return
        # Closest first: a namesake known to five of your friends before one
        # known to a single friend.
        matched.sort(key=lambda m: (m not in known, -score.get(m, 0)))
        matched = matched[: int(node_cap * 0.7)]
        mset = set(matched)

        edges: list = []
        reach: dict[int, int] = {}
        for f in friends:
            if f in mset:
                edges.append([uid, f])
            for g in inner.get(f, []):
                if g in mset and g != f:
                    edges.append([f, g])
                    reach[f] = reach.get(f, 0) + 1
        # The friends through whom namesakes were found, strongest first.
        room = max(0, node_cap - len(matched) - 1)
        connectors = [f for f, _ in sorted(reach.items(), key=lambda kv: -kv[1])[:room] if f not in mset]
        cset = set(connectors)
        edges = [e for e in edges if (e[0] == uid or e[0] in cset or e[0] in mset) and (e[1] in mset or e[1] in cset)]
        for f in connectors:
            edges.append([uid, f])
        # Namesakes who know each other, from what is already cached.
        for m in matched:
            for g in (vc._friends_cache.get(m) or []):
                if g in mset and m < g:
                    edges.append([m, g])

        vc.save_caches()
        self._remember("relatives", who[1], f"{len(matched)} однофамильцев", {"relUser": user})
        keep = [uid] + matched + connectors
        self._emit({
            "type": "result", "paths": [], "root": uid, "surname": surname,
            "profiles": {str(i): names.get(i) or {"name": str(i)} for i in keep},
            "edges": edges,
        })

    def intersect(self, users: list, opts: dict | None = None) -> dict:
        o = opts or {}
        self._run("пересечения", lambda: self._intersect(users, o))
        return {"started": True}

    def _intersect(self, users: list, opts: dict | None = None) -> None:
        """Friends shared by everyone named. New in the UI, so new here."""
        self._cancel.clear()
        self._apply_opts(opts or {})
        self._ensure_caches()
        token = self._token()
        ids = [w[0] for w in (self._resolve(token, u) for u in users) if w]
        if len(ids) < 2:
            self._emit({"type": "error", "message": "нужно хотя бы двое"})
            return
        got = vc.get_friends_batch(token, ids)
        sets = [set(got.get(i, [])) for i in ids]
        common = set.intersection(*sets) if sets else set()
        if not common:
            self._emit({"type": "error", "message": LIMIT_MESSAGE if self._limited() else "общих друзей нет"})
            return
        edges = [[i, c] for i in ids for c in common]
        # Links among the shared friends: the shape of the group they form.
        shared = list(common)
        inner = vc.get_friends_batch(token, shared[:300])
        for c in shared:
            for g in (inner.get(c) or vc._friends_cache.get(c) or []):
                if g in common and c < g:
                    edges.append([c, g])
        vc.save_caches()
        self._remember("intersect", ", ".join(map(str, ids)), f"{len(common)} общих", {"people": list(users)})
        self._emit({
            "type": "result", "paths": [], "roots": ids,
            "profiles": self._profiles_for(token, ids + shared),
            "edges": edges,
        })

    def clusters(self, center: str, opts: dict | None = None) -> dict:
        o = opts or {}
        self._run("круги", lambda: self._clusters(center, o))
        return {"started": True}

    def _clusters(self, center: str, opts: dict) -> None:
        """Splits someone's friends into circles by who knows whom.

        Label propagation rather than Louvain: it needs no tuning, runs in a
        few passes over the edge list, and on a friend graph - where circles
        are dense and barely touch - it finds the same groups a person would
        name themselves.
        """
        self._cancel.clear()
        self._apply_opts(opts)
        self._ensure_caches()
        token = self._token()
        who = self._resolve(token, center)
        if not who:
            self._emit({"type": "error", "message": "пользователь не найден"})
            return
        root = who[0]
        cap = max(60, int(opts.get("max_nodes", 400)))
        self._emit({"type": "front", "side": "A", "level": 1, "checked": 0})
        friends = vc.get_friends_batch(token, [root]).get(root, [])[:cap]
        if not friends:
            self._emit({"type": "error", "message": LIMIT_MESSAGE if self._limited() else "друзей не видно"})
            return

        # Who among the friends knows whom: the circles live in these links,
        # not in the links back to the centre.
        self._emit({"type": "front", "side": "A", "level": 2, "checked": len(friends)})
        inner = vc.get_friends_batch(token, friends)
        among = {f: set(inner.get(f, [])) & set(friends) for f in friends}

        label = {f: i for i, f in enumerate(friends)}
        for _ in range(12):
            moved = 0
            for f in friends:
                if self._cancel.is_set():
                    break
                near = among.get(f) or set()
                if not near:
                    continue
                tally: dict[int, int] = {}
                for n in near:
                    tally[label[n]] = tally.get(label[n], 0) + 1
                best = max(tally, key=lambda k: (tally[k], -k))
                if best != label[f]:
                    label[f] = best
                    moved += 1
            if not moved:
                break

        groups: dict[int, list[int]] = {}
        for f in friends:
            groups.setdefault(label[f], []).append(f)
        min_size = int(opts.get("min_size", 3))
        kept = {k: v for k, v in groups.items() if len(v) >= min_size}

        # Edges inside each circle, plus the spokes from the centre, so the
        # picture shows both the circles and that they are all one person's.
        edges = [[root, f] for f in friends]
        for members in kept.values():
            seen = set(members)
            for f in members:
                for n in among.get(f, ()):
                    if n in seen and f < n:
                        edges.append([f, n])
        vc.save_caches()
        self._remember("clusters", who[1], f"{len(kept)} кругов из {len(friends)}", {"center": center})
        self._emit({
            "type": "result", "paths": [], "root": root,
            "groups": [sorted(v) for v in kept.values()],
            "profiles": self._profiles_for(token, [root] + friends),
            "edges": edges,
        })

    # -- helpers ----------------------------------------------------------

    def _resolve(self, token: str, who: str):
        ident = str(who).strip().rstrip("/").split("/")[-1]
        return vc.resolve_user(token, ident)

    def _profiles_for(self, token: str, ids: list) -> dict:
        """Name, city and avatar for each id, from disk where known."""
        return {str(k): v for k, v in self._names(token, list(ids)).items()}

    def _names(self, token: str, ids: list, progress=None) -> dict:
        """Profiles for `ids`: what is on disk, and the rest from VK.

        Asked for 100 at a time. vk_chain's own routine asks for 1000, but
        users.get returns at most 100 once any extra field is requested and
        drops the rest without saying so, which showed up as people with no
        name in a graph of 400.
        """
        have = self._profiles_on_disk()
        out, missing = {}, []
        for i in ids:
            rec = have.get(str(i))
            # An entry from before avatars were stored has no picture; fetch
            # it again rather than showing a blank forever.
            if rec and rec.get("last_name") is not None and rec.get("photo_url"):
                out[i] = rec
            else:
                missing.append(i)
        fetched = 0
        for k in range(0, len(missing), 100):
            if self._cancel.is_set():
                break
            chunk = missing[k:k + 100]
            resp = vc.api(
                token, "users.get", user_ids=",".join(map(str, chunk)),
                fields="photo_100,domain,city,maiden_name",
            )
            for u in resp or []:
                first, last = u.get("first_name", ""), u.get("last_name", "")
                rec = {
                    "name": f"{first} {last}".strip() or str(u["id"]),
                    "first_name": first,
                    "last_name": last,
                    "maiden_name": u.get("maiden_name", ""),
                    "city": (u.get("city") or {}).get("title", ""),
                    "photo_url": u.get("photo_100", ""),
                    "domain": u.get("domain", f"id{u['id']}"),
                }
                out[u["id"]] = rec
                have[str(u["id"])] = rec
            fetched += len(chunk)
            if progress:
                progress(fetched, len(missing))
        if missing:
            PROFILES.write_text(json.dumps(have, ensure_ascii=False, indent=1), encoding="utf-8")
        for i in ids:
            # Whoever VK would not describe still needs an entry to be drawn.
            out.setdefault(i, {"name": str(i), "city": "", "photo_url": ""})
        return out


_selftest_ok = [False]


def _selftest(window) -> None:
    """Opens the page hidden, checks that it mounts without a render error,
    and records the verdict in selftest.txt next to the other data.

    This is what proves a built program works: a frozen executable that
    starts and shows nothing looks exactly like one that works, from outside.
    """
    verdict = "FAIL page never mounted"
    try:
        for _ in range(150):
            if window.evaluate_js("!!window.vkApp"):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("window.vkApp never appeared")
        time.sleep(1.0)
        body = window.evaluate_js("document.body.innerText.slice(0, 600)") or ""
        if "renderVals" in body or "Cannot read" in body:
            raise RuntimeError("render error on screen: " + body[:160].replace("\n", " "))
        # Fonts load lazily, so counting the loaded ones measures timing, not
        # whether they are there. Ask for each face the interface uses - Latin
        # and Cyrillic, since it is a Russian interface - and wait for the
        # answer. A font file missing from the built program fails this.
        window.evaluate_js(
            "window.__fonts = 'wait';"
            "Promise.all(["
            "document.fonts.load('800 16px Unbounded', 'A'),"
            "document.fonts.load('800 16px Unbounded', 'Ж'),"
            "document.fonts.load('400 16px \"JetBrains Mono\"', 'A'),"
            "document.fonts.load('400 16px \"JetBrains Mono\"', 'Ж')"
            "]).then(r => { window.__fonts = r.map(x => x.length).join(','); })"
            ".catch(e => { window.__fonts = 'error ' + e; });"
        )
        fonts = "wait"
        for _ in range(60):
            fonts = window.evaluate_js("window.__fonts")
            if fonts != "wait":
                break
            time.sleep(0.2)
        if not fonts or not all(n.isdigit() and int(n) >= 1 for n in str(fonts).split(",")):
            raise RuntimeError(f"fonts did not load: {fonts}")
        _selftest_ok[0] = True
        verdict = f"PASS version {__version__}, fonts ok (latin and cyrillic: {fonts})"
    except Exception as e:
        traceback.print_exc()
        verdict = f"FAIL {e}"
    finally:
        try:
            (DATA / "selftest.txt").write_text(verdict, encoding="utf-8")
        except OSError:
            pass
        window.destroy()


def main() -> None:
    if not UI.exists():
        raise SystemExit(f"интерфейс не найден: {UI}")
    _setup_logging()
    testing = "--selftest" in sys.argv
    api = Api()
    settings = api.get_settings()
    shape = _display_args(settings)
    api._fullscreen = bool(shape.get("fullscreen"))
    window = webview.create_window(
        "vk-chain",
        str(UI),
        js_api=api,
        # Below this the two side panels overlap the graph entirely.
        min_size=(960, 640),
        background_color="#08090b",
        text_select=False,
        zoomable=False,
        hidden=testing,
        **shape,
    )
    api.bind(window)

    def remember_size() -> None:
        """Written when the window closes, so it opens the same size again.

        Only for a plain window: a maximised or full-screen window's size is
        the screen's, and saving it would make "window" mean "maximised".
        """
        try:
            current = api.get_settings()
            if (current.get("display") or {}).get("mode", "maximized") != "window":
                return
            old = current.get("_window") or {}
            width, height = window.width, window.height
            # The window reports a pixel less than it was asked for. Saving
            # that every time would wear it down by a pixel per run, so a
            # difference that small is taken to mean "not resized".
            if abs(width - int(old.get("width", 0))) <= 2 and abs(height - int(old.get("height", 0))) <= 2:
                return
            current["_window"] = {"width": width, "height": height}
            SETTINGS.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            traceback.print_exc()

    try:
        window.events.closing += remember_size
        window.events.loaded += lambda: _style_titlebar(window)
    except Exception:
        pass
    # No debug menu, no right-click inspector: this is an application, not a
    # page being worked on.
    if testing:
        webview.start(_selftest, window, debug=False)
        sys.exit(0 if _selftest_ok[0] else 1)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
