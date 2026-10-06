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
        check("full-screen button is offered", "ВО ВЕСЬ ЭКРАН" in body)
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

        # ---- every mode's form opens without an error ----------------------------------------------
        for mode in ("relatives", "explore", "intersect", "clusters", "chain"):
            body = text_after(f"window.vkApp.setState({{mode: '{mode}'}})", 0.5)
            check(f"{mode} form renders", "renderVals" not in body and "Cannot read" not in body)

        # ---- errors are shown, and can be dismissed ---------------------------------------------------
        body = text_after("window.vkApp.showErr('проверка сообщения')")
        check("an error toast appears", "проверка сообщения" in body)
        body = text_after("window.vkApp.renderVals().dismissErr()")
        check("and goes away", "проверка сообщения" not in body)

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


def main() -> int:
    ui = app.UI
    window = webview.create_window(
        "selftest", str(ui), js_api=app.Api(), width=1400, height=900, hidden=True,
    )
    t = threading.Thread(target=lambda: None)
    webview.start(run, window, debug=False)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
