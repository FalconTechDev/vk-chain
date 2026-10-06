"""Drives the real interface headlessly, in the real browser engine.

Opens the window *hidden* - it never appears and never takes focus - loads the
page, and then asks the page questions with evaluate_js. That catches what a
syntax check cannot: a template name with no value, a layout that throws on
real data, settings that are not actually written.

    python tests/ui_selftest.py

Exit status is 0 when everything held, 1 otherwise.
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Everything the program writes goes to a scratch folder, set before the app
# is imported. The test presses buttons such as "clear cache"; it must not be
# able to reach a real one.
SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="vkchain-test-"))
os.environ["VKCHAIN_DATA"] = str(SCRATCH)

import webview  # noqa: E402

import app  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))


def js(window, code: str):
    """Evaluates `code` in the page and returns the decoded result."""
    raw = window.evaluate_js(f"JSON.stringify((function(){{ {code} }})())")
    return json.loads(raw) if raw not in (None, "undefined") else None


def step(window, n: int = 60) -> None:
    """Runs n frames of the page's own loop with a fixed 16 ms clock."""
    js(window, f"""
      const a = window.vkApp;
      // Never behind the real clock: the page times its reveal animation from it.
      a.__t = Math.max(a.__t || 0, performance.now());
      for (let i = 0; i < {n}; i++) {{ a.__t += 16; a.frame(a.__t); }}
      return 1;""")


def snapshot(window, name: str) -> str:
    """Saves what the canvas is showing, so the result can be looked at."""
    import base64
    data = window.evaluate_js("document.querySelector('canvas').toDataURL('image/png')")
    path = SCRATCH / (name + ".png")
    path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
    return str(path)


def sample_result() -> dict:
    """A made-up chain with a crowd around it.

    Invented people, so the test needs no cache and no real person's
    identifier ever appears in this repository. The shape is what matters: a
    five-person chain, a circle of friends around each member whose members
    mostly know each other, and a few friendships between neighbouring circles.
    """
    import random

    rnd = random.Random(7)
    path = [1, 2, 3, 4, 5]
    edges = [[path[i], path[i + 1]] for i in range(len(path) - 1)]
    ids = set(path)
    circles = []
    nxt = 100
    for hub in path:
        circle = []
        for _ in range(45):
            circle.append(nxt)
            ids.add(nxt)
            edges.append([hub, nxt])
            nxt += 1
        for a in circle:
            for b in circle:
                if a < b and rnd.random() < 0.12:
                    edges.append([a, b])
        circles.append(circle)
    for left, right in zip(circles, circles[1:]):
        for _ in range(6):
            edges.append([rnd.choice(left), rnd.choice(right)])
    profiles = {str(i): {"name": f"Тестов {i}", "city": "Тест"} for i in ids}
    return {"type": "result", "paths": [path], "edges": edges, "profiles": profiles}


def run(window) -> None:
    try:
        # ---- the page comes up -------------------------------------------------
        for _ in range(100):
            if window.evaluate_js("!!window.vkApp"):
                break
            time.sleep(0.2)
        else:
            check("page mounts a component", False, "window.vkApp never appeared")
            return
        check("page mounts a component", True)
        time.sleep(1.0)

        js(window, "window.__errs=[]; window.addEventListener('error', e => window.__errs.push(e.message));")

        body = js(window, "return document.body.innerText.slice(0, 400)") or ""
        check("no render error on screen", "renderVals" not in body and "Cannot read" not in body, body[:80].replace("\n", " "))

        # ---- offline: nothing is fetched from the network ------------------------
        # The page is served from pywebview's own loopback server, so those
        # addresses are local. What must not appear is any other host.
        ext = js(window, r"""
          return performance.getEntriesByType('resource')
            .map(r => r.name).filter(n => /^https?:/.test(n) && !/^https?:\/\/(127\.0\.0\.1|localhost)/.test(n)).slice(0, 8);""")
        check("no network requests for the page itself", not ext, ", ".join(ext or []))

        fams = js(window, """
          return Array.from(new Set(Array.from(document.fonts)
            .filter(f => f.status === 'loaded').map(f => f.family.replace(/"/g, ''))));""")
        check("bundled fonts load", fams and "Unbounded" in fams and "JetBrains Mono" in fams, str(fams))

        # ---- fills the window --------------------------------------------------
        window.resize(1700, 1000)
        time.sleep(1.2)
        # A hidden window never gets animation frames, so step the page's own
        # frame function by hand. That draws exactly what the loop would.
        step(window, 4)
        size = js(window, """
          const c = document.querySelector('canvas'), r = c.getBoundingClientRect();
          return { cw: c.width, ch: c.height, w: r.width, h: r.height,
                   iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio };""")
        fills = size and abs(size["w"] - size["iw"]) < 2 and abs(size["h"] - size["ih"]) < 2
        check("canvas fills the window", fills, str(size))
        backing = size and abs(size["cw"] - size["iw"] * size["dpr"]) < 3
        check("canvas resolution follows the window", backing, str(size))
        no_skip = js(window, "return !/ПРОПУСТИТЬ/.test(document.body.innerText)")
        check("no skip button", no_skip)
        no_hint = js(window, "return !/shift\\+тяни/.test(document.body.innerText)")
        check("no tutorial hint", no_hint)

        # ---- a real result goes through the layout --------------------------------
        res = sample_result()
        js(window, f"window.vkApp.applyResult({json.dumps(res)}); return 1;")
        time.sleep(1.5)
        g = js(window, """
          const a = window.vkApp, g = a.g;
          let bad = 0;
          for (const n of g.nodes) {
            if (!n.p2 || !n.p3 || !isFinite(n.p2[0]) || !isFinite(n.p2[1]) || !isFinite(n.p3[0]) || !isFinite(n.p3[1]) || !isFinite(n.p3[2])) bad++;
          }
          return { screen: a.state.screen, nodes: g.nodes.length, edges: g.edges.length,
                   keep: g.keep.filter(Boolean).length, bad, fit2: a.fit2, fit3: a.fit3,
                   paths: a.paths.length, errs: window.__errs };""")
        step(window, 120)
        print("  3D view:", snapshot(window, "chain_3d"))
        js(window, "window.vkApp.setState({view: '2d'}); return 1;")
        step(window, 120)
        print("  2D view:", snapshot(window, "chain_2d"))
        js(window, "window.vkApp.setCfg({trim: true}); return 1;")
        step(window, 10)
        print("  2D trimmed:", snapshot(window, "chain_2d_trim"))
        js(window, "window.vkApp.setCfg({trim: false}); window.vkApp.setState({view: '3d'}); return 1;")
        step(window, 5)
        check("result screen opens", g and g["screen"] == "graph", str(g and g["screen"]))
        check("every node has finite positions", g and g["bad"] == 0, f"{g and g['nodes']} nodes")
        check("links are de-duplicated and trimmed", g and 0 < g["keep"] < g["edges"], f"{g and g['keep']} of {g and g['edges']}")
        check("camera fit is sane", g and 0.2 <= g["fit2"] <= 1.8 and 0.2 <= g["fit3"] <= 1.8, f"{g and g['fit2']:.2f}/{g and g['fit3']:.2f}")
        check("no script errors while drawing", g and not g["errs"], str(g and g["errs"])[:120])

        # ---- resizing after a result arrived re-fits it ----------------------------------------
        # A hidden WebView2 window stops updating innerWidth after its first
        # resize, so the size the page sees is overridden instead. What is
        # under test is the page's reaction to a new size, not the browser's.
        def pretend(w, h):
            js(window, f"""
              window.__d = window.__d || {{ w: Object.getOwnPropertyDescriptor(window, 'innerWidth'),
                                          h: Object.getOwnPropertyDescriptor(window, 'innerHeight') }};
              Object.defineProperty(window, 'innerWidth', {{ configurable: true, get: () => {w} }});
              Object.defineProperty(window, 'innerHeight', {{ configurable: true, get: () => {h} }});
              return 1;""")
            step(window, 4)
            return js(window, "return [window.vkApp.fit2, window.vkApp.fit3]")

        before = js(window, "return [window.vkApp.fit2, window.vkApp.fit3]")
        small = pretend(1000, 640)
        check("the fit follows the window when it is made smaller", small[0] < before[0] and small[1] < before[1], f"{before} -> {small}")
        big = pretend(1800, 1100)
        check("and when it is made larger", big[0] > small[0] and big[1] > small[1], f"{small} -> {big}")
        js(window, """
          Object.defineProperty(window, 'innerWidth', window.__d.w);
          Object.defineProperty(window, 'innerHeight', window.__d.h);
          return 1;""")
        step(window, 4)

        # ---- toggles that touch drawing ---------------------------------------------
        js(window, "window.vkApp.setCfg({trim: true}); return 1;")
        time.sleep(0.6)
        js(window, "window.vkApp.setCfg({trim: false}); return 1;")
        time.sleep(0.4)
        errs = js(window, "return window.__errs")
        check("trimming edges does not throw", not errs, str(errs)[:120])

        # ---- going home brings the cover back, centred -----------------------------
        js(window, "window.vkApp.user.panX = 300; window.vkApp.user.zoom = 2; window.vkApp.setScreen('home'); return 1;")
        time.sleep(0.8)
        home = js(window, """
          const a = window.vkApp;
          return { cover: a.g === a.cover.g, panX: a.user.panX, zoom: a.user.zoom, fit: [a.fit2, a.fit3] };""")
        check("home shows the cover graph again", home and home["cover"], str(home))
        check("cover is re-centred", home and home["panX"] == 0 and home["zoom"] == 1, str(home))

        # ---- settings are written and read back ----------------------------------------
        js(window, "window.vkApp.setCfg({trim: true, size: 1.7}); window.vkApp.setState({pathsN: 4, mode: 'explore', view: '2d'}); return 1;")
        time.sleep(2.0)
        saved = json.loads(app.SETTINGS.read_text(encoding="utf-8")) if app.SETTINGS.exists() else {}
        ok = saved.get("cfg", {}).get("trim") is True and saved.get("cfg", {}).get("size") == 1.7 \
            and saved.get("pathsN") == 4 and saved.get("mode") == "explore" and saved.get("view") == "2d"
        check("settings are written to disk", ok, str({k: saved.get(k) for k in ("pathsN", "mode", "view")}))

        # a restart: wipe the live state, restore, and compare
        js(window, "clearInterval(window.vkApp.saveTimer); window.vkApp.setState({pathsN: 1, mode: 'chain', view: '3d'}); window.vkApp.setCfg({trim: false, size: 1}); window.__r = 'pending'; return 1;")
        time.sleep(0.3)
        window.evaluate_js("window.vkApp.restore().then(() => { window.__r = 'ok'; }).catch(e => { window.__r = String(e); })")
        time.sleep(1.2)
        check("restore() completes", js(window, "return window.__r") == "ok", str(js(window, "return window.__r")))
        back = js(window, "const s = window.vkApp.state; return { n: s.pathsN, m: s.mode, v: s.view, t: s.cfg.trim, z: s.cfg.size };")
        check("settings are restored", back == {"n": 4, "m": "explore", "v": "2d", "t": True, "z": 1.7}, str(back))

        # ---- the settings drawer, with its real token and cache panel ---------------------------
        def text_after(code: str, wait: float = 0.6) -> str:
            js(window, code + "; return 1;")
            time.sleep(wait)
            return js(window, "return document.body.innerText") or ""

        body = text_after("window.vkApp.setState({settings: 'main'})")
        check("settings drawer renders", "ТОКЕН VK" in body and "Сохранить" in body and "renderVals" not in body)
        check("token panel shows real status, not the sample token", "••••3f9c" not in body)
        check("cache panel shows real numbers", "18 660" not in body and "42 МБ" not in body)
        body = text_after("window.vkApp.setState({settings: 'graph'})")
        check("graph tab has the trim switch", "Обрезать лишние связи" in body, "")
        check("graph tab has the node-limit slider", "МАКС. УЗЛОВ" in body)

        # clearing the cache needs two presses; only the first is made here
        label = js(window, "window.vkApp.setState({settings: 'main'}); window.vkApp.clearCache(); return 1;")
        time.sleep(0.5)
        armed = js(window, "return window.vkApp.renderVals().clearLabel")
        check("clearing the cache asks to be confirmed", "ещё раз" in (armed or ""), str(armed))
        js(window, "window.vkApp.setState({confirmClear: false, settings: null}); return 1;")

        # a bad token is refused, and the refusal is shown
        body = text_after("window.vkApp.setState({tokenDraft: 'abc'}); window.vkApp.saveToken()", 1.0)
        check("a junk token is refused on screen", "не похоже на токен" in body)

        # ---- the search form ---------------------------------------------------------------
        body = text_after("window.vkApp.setScreen('home'); window.vkApp.setState({mode: 'chain', err: null})", 1.0)
        check("chain form has the path-count control", "КОЛИЧЕСТВО ПУТЕЙ" in body)
        check("full-screen button is no longer in the top bar", "ВО ВЕСЬ ЭКРАН" not in body)
        n = js(window, """
          const a = window.vkApp; a.setState({pathsN: 1});
          return null;""")
        counts = []
        for _ in range(10):
            counts.append(js(window, "const a = window.vkApp; a.renderVals().pathsInc(); return a.state.pathsN;"))
            time.sleep(0.15)
        check("path count stops at 8", max(c for c in counts if c) <= 8, str(counts))
        for _ in range(10):
            js(window, "window.vkApp.renderVals().pathsDec(); return 1;")
            time.sleep(0.15)
        low = js(window, "return window.vkApp.state.pathsN")
        check("path count stops at 1", low == 1, str(low))

        # ---- history: clicking an entry runs that search again -----------------------------------------------
        js(window, """
          window.__launched = 0;
          window.vkApp.launch = () => { window.__launched = 1; };    // never reach VK from a test
          window.vkApp.rerun('chain', { from: 'id111', to: 'id222' });
          return 1;""")
        time.sleep(0.5)
        st = js(window, "const s = window.vkApp.state; return { m: s.mode, f: s.from, t: s.to, l: window.__launched };")
        check("a history entry refills the form and starts the search", st == {"m": "chain", "f": "id111", "t": "id222", "l": 1}, str(st))
        js(window, "window.vkApp.rerun('relatives', { relUser: 'someone' }); return 1;")
        time.sleep(0.4)
        st = js(window, "const s = window.vkApp.state; return { m: s.mode, u: s.relUser };")
        check("and does the same for relatives", st == {"m": "relatives", "u": "someone"}, str(st))
        js(window, "window.vkApp.rerun('intersect', { people: ['a', 'b'] }); return 1;")
        time.sleep(0.4)
        st = js(window, "const s = window.vkApp.state; return { m: s.mode, p: s.people };")
        check("and for intersections", st == {"m": "intersect", "p": ["a", "b"]}, str(st))

        # ---- every mode's form opens without an error ----------------------------------------------
        for mode in ("relatives", "explore", "intersect", "clusters", "chain"):
            body = text_after(f"window.vkApp.setState({{mode: '{mode}'}})", 0.5)
            check(f"{mode} form renders", "renderVals" not in body and "Cannot read" not in body)

        # ---- errors are shown, and can be dismissed ---------------------------------------------------
        body = text_after("window.vkApp.showErr('проверка сообщения')")
        check("an error toast appears", "проверка сообщения" in body)
        body = text_after("window.vkApp.renderVals().dismissErr()")
        check("and goes away", "проверка сообщения" not in body)


        # ---- pixels: link colours, hover and avatars are really drawn -----------------------------
        def reds(rgb: str) -> int:
            """How many canvas pixels are close to a colour, at any opacity."""
            r, g, b = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
            return js(window, f"""
              const c = document.querySelector('canvas'), x = c.getContext('2d');
              const d = x.getImageData(0, 0, c.width, c.height).data;
              let n = 0;
              for (let i = 0; i < d.length; i += 4) {{
                if (d[i + 3] > 10 && Math.abs(d[i] - {r}) < 40 && Math.abs(d[i + 1] - {g}) < 40 && Math.abs(d[i + 2] - {b}) < 40) n++;
              }}
              return n;""")

        js(window, f"window.vkApp.applyResult({json.dumps(res)}); window.vkApp.setCfg({{trim: false, avatars: false, edges: true}}); window.vkApp.setState({{view: '2d', settings: null}}); return 1;")
        time.sleep(0.4)
        step(window, 150)
        base = reds("#ff0000")
        js(window, "const r = '#ff0000'; window.vkApp.setState({edgeStyle: {normal: [r, r, r], hover: ['#00ffff', '#00ffff', '#00ffff']}}); return 1;")
        step(window, 4)
        painted = reds("#ff0000")
        check("link colour setting changes what is drawn", painted > base + 100, f"{base} -> {painted} red pixels")

        hub = js(window, "const nb = window.vkApp.g.nb; let b = 0; nb.forEach((l, i) => { if (l.length > nb[b].length) b = i; }); return b;")
        before = reds("#00ffff")
        js(window, f"window.vkApp.hover = {hub}; return 1;")
        step(window, 4)
        lit = reds("#00ffff")
        js(window, "window.vkApp.hover = null; return 1;")
        step(window, 4)
        after = reds("#00ffff")
        check("a hovered node's links light up in the hover colour", lit > before + 30 and after < lit, f"{before} -> {lit} -> {after} cyan pixels")

        # a magenta square as a stand-in picture; a data address is same-origin, so it does not taint the canvas
        js(window, """
          const c = document.createElement('canvas'); c.width = c.height = 64;
          const x = c.getContext('2d'); x.fillStyle = '#ff00ff'; x.fillRect(0, 0, 64, 64);
          window.__avatar = c.toDataURL('image/png');
          window.vkApp.g.nodes.forEach(n => { n.photo = window.__avatar; });
          window.vkApp.setCfg({avatars: true});
          return 1;""")
        drawn = mag = 0
        for _round in range(4):     # the picture loads asynchronously
            step(window, 6)
            time.sleep(0.5)
            step(window, 8)
            drawn = js(window, "return window.vkApp.avatarsDrawn")
            mag = reds("#ff00ff")
            if drawn and mag > 150:
                break
        check("avatars are drawn on the nodes", drawn and drawn > 0 and mag > 150, f"{drawn} drawn, {mag} magenta pixels")
        js(window, "window.vkApp.setCfg({avatars: false}); return 1;")
        step(window, 4)
        check("and can be switched off", js(window, "return window.vkApp.avatarsDrawn") == 0)

        card = js(window, "window.vkApp.setCfg({avatars: true}); window.vkApp.select(0); return window.vkApp.renderVals().sel.avatarBg;")
        check("the profile card shows the picture", "url(" in (card or ""), str(card)[:50])
        js(window, "window.vkApp.select(null); return 1;")

        # ---- intersections: an id can be typed in -----------------------------------------------------
        js(window, "window.vkApp.setState({people: [], personDraft: 'id777'}); return 1;")
        js(window, "window.vkApp.setScreen('home'); window.vkApp.setState({mode: 'intersect'}); return 1;")
        time.sleep(0.8)
        body = js(window, "return document.body.innerText") or ""
        check("intersection form has a field for an id", "+ добавить" in body and "+ человек" not in body)
        js(window, "window.vkApp.renderVals().addPerson(); return 1;")
        time.sleep(0.3)
        people = js(window, "return window.vkApp.state.people")
        check("the typed id is added, not an invented one", people == ["id777"], str(people))
        js(window, "window.vkApp.setState({personDraft: 'https://vk.com/some_name/'}); window.vkApp.renderVals().onPersonKey({key: 'Enter', preventDefault() {}}); return 1;")
        time.sleep(0.3)
        people = js(window, "return window.vkApp.state.people")
        check("Enter adds it, and a link is reduced to the name", people == ["id777", "some_name"], str(people))
        js(window, "window.vkApp.setState({people: ['a','b','c','d','e'], personDraft: 'f'}); window.vkApp.addPerson(); return 1;")
        time.sleep(0.4)
        body = js(window, "return document.body.innerText") or ""
        check("a sixth person is refused with a message", "не больше пяти" in body)
        js(window, "window.vkApp.setState({people: [], personDraft: '', err: null}); return 1;")

        # ---- settings: link colours and screen mode --------------------------------------------------------
        body = text_after("window.vkApp.setScreen('home'); window.vkApp.setState({settings: 'graph'})", 0.8)
        check("graph tab has link colours for both states", "ЦВЕТ СВЯЗЕЙ" in body and "БЕЗ НАВЕДЕНИЯ" in body and "ПРИ НАВЕДЕНИИ" in body)
        check("graph tab has the avatars switch", "Аватарки" in body)
        js(window, "window.vkApp.renderVals().edgeGroups[1].rows[0].colors[2].pick(); return 1;")
        time.sleep(0.3)
        picked = js(window, "return window.vkApp.state.edgeStyle.hover[0]")
        check("picking a swatch sets that level's colour", picked == "#7b8cff", str(picked))

        body = text_after("window.vkApp.setState({settings: 'main'})", 0.8)
        check("main tab has the screen mode", "ЭКРАН" in body and "Развёрнуто" in body and "Весь экран" in body and "Без рамки" in body)
        js(window, "window.vkApp.renderVals().displaySeg[0].pick(); return 1;")      # window
        time.sleep(0.8)
        body = js(window, "return document.body.innerText") or ""
        check("a window size can be chosen", "1280×800" in body and "1920×1080" in body)
        js(window, "window.vkApp.renderVals().sizeSeg[0].pick(); return 1;")
        time.sleep(1.0)
        disp = js(window, "return window.vkApp.state.display")
        check("the chosen size is kept in the state", disp == {"mode": "window", "w": 1280, "h": 800}, str(disp))
        shown = js(window, "return document.body.innerText.slice(0, 2000)") or ""
        check("changing the mode raised no error", "не удалось" not in shown and "режим недоступен" not in shown)

        # these two are written with the rest, and read back
        time.sleep(1.5)
        saved = json.loads(app.SETTINGS.read_text(encoding="utf-8"))
        check("screen mode and link colours are written to disk",
              saved.get("display") == {"mode": "window", "w": 1280, "h": 800} and saved.get("edgeStyle", {}).get("hover", [""])[0] == "#7b8cff",
              str({k: saved.get(k) for k in ("display",)}))

        # ---- notifications -------------------------------------------------------------------------------------
        TOASTS.clear()
        js(window, "window.vkApp.setCfg({notify: false}); window.vkApp.setState({settings: 'main'}); return 1;")
        time.sleep(0.4)
        js(window, "window.vkApp.renderVals().mainToggles.find(t => /Уведомлен/.test(t.label)).toggle(); return 1;")
        time.sleep(1.0)
        check("switching notifications on shows a test one", ("vk-chain", "Уведомления включены") in TOASTS, str(TOASTS))
        TOASTS.clear()
        js(window, "window.vkApp.launchedAt = performance.now() - 20000; window.vkApp.notifyDone('vk-chain', 'готово'); return 1;")
        time.sleep(0.8)
        check("a long search finishing sends one", ("vk-chain", "готово") in TOASTS, str(TOASTS))
        TOASTS.clear()
        js(window, "window.vkApp.setCfg({notify: false}); window.vkApp.notifyDone('vk-chain', 'тихо'); return 1;")
        time.sleep(0.8)
        check("and nothing is sent when they are off", not TOASTS, str(TOASTS))
        summary = js(window, "window.vkApp.setState({mode: 'chain'}); return window.vkApp.summarise({paths: [[1,2,3,4]], edges: [[1,2]]});")
        check("the notification says what was found", "3 рукопожатий" in (summary or ""), str(summary))
        js(window, "window.vkApp.setState({settings: null}); return 1;")

        # ---- nodes are only ever drawn as the shaded solids -------------------------------------------------
        src = app.UI.read_text(encoding="utf-8")
        i = src.index("this.ball3d(ctx, q.x, q.y, rr, col, shp, i);")
        tail = src[i:i + 1500]
        check("no flat shape is painted over the solids",
              "shapePath(ctx, shp, q.x" not in tail,
              "a stray `else` after ball3d binds to whatever follows it")

        # ---- bad input does not break the page ---------------------------------------------
        js(window, "window.vkApp.applyResult({edges: [], paths: []}); return 1;")
        time.sleep(0.5)
        body = js(window, "return document.body.innerText.slice(0, 400)") or ""
        check("an empty result is survived", "renderVals" not in body)
    except Exception as e:  # a failure of the test itself is a failure
        import traceback
        traceback.print_exc()
        check("self-test ran to the end", False, repr(e))
    finally:
        window.destroy()


TOASTS: list = []


def main() -> int:
    ui = app.UI
    # A real notification would pop up on whoever is running the test.
    app._toast = lambda title, message: TOASTS.append((title, message)) or True
    api = app.Api()
    window = webview.create_window(
        "selftest", str(ui), js_api=api, width=1400, height=900, hidden=True,
    )
    api.bind(window)
    t = threading.Thread(target=lambda: None)
    webview.start(run, window, debug=False)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
