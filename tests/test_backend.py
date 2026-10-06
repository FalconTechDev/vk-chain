"""Tests for the parts of app.py that need no network.

    python -m unittest tests.test_backend -v

Anything that would call VK is replaced with a stand-in, so these run in a
fraction of a second and cannot spend a request or touch a real cache.
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Keep every file the program writes inside a scratch folder.
_DATA = tempfile.mkdtemp(prefix="vkchain-unit-")
os.environ["VKCHAIN_DATA"] = _DATA

import app  # noqa: E402
import vk_chain as vc  # noqa: E402


class FakeWindow:
    """Collects what would have been pushed into the page."""

    def __init__(self):
        self.events = []
        self.done = threading.Event()

    def evaluate_js(self, code):
        ev = json.loads(code[code.find("(") + 1:-1])
        self.events.append(ev)
        if ev["type"] in ("result", "error"):
            self.done.set()


class Settings(unittest.TestCase):
    def setUp(self):
        self._old = app.SETTINGS
        app.SETTINGS = pathlib.Path(tempfile.mkdtemp(prefix="vkchain-set-")) / "settings.json"
        self.api = app.Api()

    def tearDown(self):
        app.SETTINGS = self._old

    def test_nothing_saved_reads_as_empty(self):
        self.assertEqual(self.api.get_settings(), {})

    def test_round_trip(self):
        values = {"cfg": {"trim": True, "size": 1.5}, "pathsN": 3, "mode": "explore"}
        self.assertTrue(self.api.save_settings(values)["ok"])
        self.assertEqual(self.api.get_settings(), values)

    def test_saving_keeps_the_windows_own_entry(self):
        # The interface does not know about the window's size, so saving its
        # settings must not wipe it.
        app.SETTINGS.write_text(json.dumps({"_window": {"width": 1600, "height": 950}}))
        self.api.save_settings({"pathsN": 2})
        got = self.api.get_settings()
        self.assertEqual(got["pathsN"], 2)
        self.assertEqual(got["_window"], {"width": 1600, "height": 950})

    def test_a_corrupt_file_is_survived(self):
        app.SETTINGS.write_text("{ not json")
        self.assertEqual(self.api.get_settings(), {})


class Token(unittest.TestCase):
    def test_the_sign_in_address_asks_for_a_token(self):
        url = app.token_url("123")
        self.assertIn("client_id=123", url)
        self.assertIn("response_type=token", url)

    def test_junk_is_rejected_without_asking_vk(self):
        api = app.Api()
        r = api.set_token("abc")
        self.assertFalse(r["ok"])
        self.assertIn("токен", r["error"])


class Context(unittest.TestCase):
    """The neighbourhood drawn around a chain, taken from the cache."""

    def setUp(self):
        self._old = dict(vc._friends_cache)
        vc._friends_cache.clear()
        self.api = app.Api()

    def tearDown(self):
        vc._friends_cache.clear()
        vc._friends_cache.update(self._old)

    def test_includes_the_path_and_its_neighbours(self):
        vc._friends_cache.update({1: [2, 10, 11], 2: [1, 3, 10], 3: [2, 12]})
        edges, near = self.api._context([1, 2, 3])
        self.assertEqual({tuple(e) for e in edges if e[0] in (1, 2) and e[1] in (2, 3)}, {(1, 2), (2, 3)})
        self.assertEqual(set(near), {10, 11, 12})

    def test_a_hub_cannot_bury_the_path(self):
        vc._friends_cache[1] = list(range(100, 1100))   # a thousand friends
        vc._friends_cache[2] = [1]
        _, near = self.api._context([1, 2], cap=40)
        self.assertEqual(len(near), 40)

    def test_a_friend_of_two_chain_members_outranks_a_friend_of_one(self):
        shared = 500
        vc._friends_cache[1] = [shared] + list(range(100, 140))
        vc._friends_cache[2] = [shared, 1]
        _, near = self.api._context([1, 2], cap=3)
        self.assertIn(shared, near)

    def test_links_among_neighbours_are_kept(self):
        vc._friends_cache.update({1: [10, 11], 2: [1], 10: [11, 1], 11: [10, 1]})
        edges, _ = self.api._context([1, 2])
        self.assertIn([10, 11], edges)

    def test_people_missing_from_the_cache_are_fine(self):
        edges, near = self.api._context([7, 8, 9])
        self.assertEqual(near, [])
        self.assertEqual(edges, [[7, 8], [8, 9]])


class MultiplePaths(unittest.TestCase):
    """_chain asks for several routes, each avoiding the people of the last."""

    def setUp(self):
        self.api = app.Api()
        self.window = FakeWindow()
        self.api.bind(self.window)
        self.calls = []
        self._saved = (vc.find_chain, vc.save_caches, vc.load_caches,
                       app.Api._token, app.Api._resolve, app.Api._profiles_for, app.Api._remember)

        routes = [[1, 2, 3, 9], [1, 4, 5, 9], [1, 6, 7, 9]]

        def find_chain(token, a, b, banned=None, include_relatives=False):
            self.calls.append(set(banned or ()))
            for r in routes:
                if not (set(r[1:-1]) & set(banned or ())):
                    return list(r)
            return None

        vc.find_chain = find_chain
        vc.save_caches = lambda: None
        vc.load_caches = lambda: None
        app.Api._token = lambda self: "t"
        app.Api._resolve = lambda self, token, who: (int(str(who).lstrip("id")), "n", "f", "l")
        app.Api._profiles_for = lambda self, token, ids: {str(i): {"name": str(i)} for i in ids}
        app.Api._remember = lambda self, *a: None

    def tearDown(self):
        (vc.find_chain, vc.save_caches, vc.load_caches,
         app.Api._token, app.Api._resolve, app.Api._profiles_for, app.Api._remember) = self._saved

    def result(self):
        return next(e for e in self.window.events if e["type"] == "result")

    def test_one_path_by_default(self):
        self.api._chain("id1", "id9", {})
        self.assertEqual(len(self.result()["paths"]), 1)

    def test_each_further_route_avoids_the_earlier_ones(self):
        self.api._chain("id1", "id9", {"paths": 3})
        paths = self.result()["paths"]
        self.assertEqual(len(paths), 3)
        middles = [set(p[1:-1]) for p in paths]
        self.assertTrue(all(not (middles[i] & middles[j]) for i in range(3) for j in range(i + 1, 3)))
        # and the ban grew with each search
        self.assertEqual([len(c) for c in self.calls], [0, 2, 4])

    def test_stops_quietly_when_there_are_no_more_routes(self):
        self.api._chain("id1", "id9", {"paths": 6})
        self.assertEqual(len(self.result()["paths"]), 3)

    def test_direct_friends_are_one_path_not_an_endless_loop(self):
        vc.find_chain = lambda token, a, b, banned=None, include_relatives=False: [a, b]
        self.api._chain("id1", "id2", {"paths": 5})
        self.assertEqual(len(self.result()["paths"]), 1)

    def test_no_route_is_reported_as_an_error(self):
        vc.find_chain = lambda *a, **k: None
        self.api._chain("id1", "id9", {})
        self.assertEqual(self.window.events[-1]["type"], "error")

    def test_the_path_count_is_clamped(self):
        self.api._chain("id1", "id9", {"paths": 99})
        self.assertLessEqual(len(self.result()["paths"]), 8)


class DisplayModes(unittest.TestCase):
    """How the window opens, and switching between the ways of showing it."""

    def setUp(self):
        self._old = (app.SETTINGS, app._screen_size)
        app.SETTINGS = pathlib.Path(tempfile.mkdtemp(prefix="vkchain-disp-")) / "settings.json"
        app._screen_size = lambda: (1536, 960)

    def tearDown(self):
        app.SETTINGS, app._screen_size = self._old

    def test_the_default_is_a_maximised_window_not_a_fixed_one_in_a_corner(self):
        self.assertEqual(app._display_args({}), {"maximized": True})

    def test_each_mode_maps_to_what_create_window_takes(self):
        self.assertEqual(app._display_args({"display": {"mode": "fullscreen"}}), {"fullscreen": True})
        self.assertEqual(app._display_args({"display": {"mode": "borderless"}}), {"frameless": True, "maximized": True})
        self.assertEqual(app._display_args({"display": {"mode": "nonsense"}}), {"maximized": True})

    def test_a_plain_window_is_centred_and_never_larger_than_the_screen(self):
        got = app._display_args({"display": {"mode": "window", "w": 1920, "h": 1080}})
        self.assertEqual((got["width"], got["height"]), (1536, 960))
        self.assertEqual((got["x"], got["y"]), (0, 0))
        got = app._display_args({"display": {"mode": "window", "w": 1280, "h": 800}})
        self.assertEqual((got["x"], got["y"]), ((1536 - 1280) // 2, (960 - 800) // 2))

    def test_a_size_the_person_dragged_to_wins_over_a_preset(self):
        got = app._display_args({"display": {"mode": "window", "w": 1280, "h": 800}, "_window": {"width": 1100, "height": 700}})
        self.assertEqual((got["width"], got["height"]), (1100, 700))

    def test_absurd_sizes_are_clamped(self):
        got = app._display_args({"display": {"mode": "window"}, "_window": {"width": 10, "height": 10}})
        self.assertGreaterEqual(got["width"], 960)
        self.assertGreaterEqual(got["height"], 640)


class FakeNative:
    def __init__(self):
        self.calls = []
        self.native = None

    def toggle_fullscreen(self): self.calls.append("fullscreen")
    def maximize(self): self.calls.append("maximize")
    def restore(self): self.calls.append("restore")
    def resize(self, w, h): self.calls.append(("resize", w, h))
    def move(self, x, y): self.calls.append(("move", x, y))


class SwitchingModes(unittest.TestCase):
    def setUp(self):
        self._old = (app.SETTINGS, app._screen_size, app._set_frame)
        app.SETTINGS = pathlib.Path(tempfile.mkdtemp(prefix="vkchain-sw-")) / "settings.json"
        app._screen_size = lambda: (1536, 960)
        self.frames = []
        app._set_frame = lambda win, framed: self.frames.append(framed)
        self.win = FakeNative()
        self.api = app.Api()
        self.api.bind(self.win)

    def tearDown(self):
        app.SETTINGS, app._screen_size, app._set_frame = self._old

    def test_full_screen_is_toggled_once_not_every_time(self):
        self.api.set_display("fullscreen")
        self.api.set_display("fullscreen")
        self.assertEqual(self.win.calls.count("fullscreen"), 1)

    def test_leaving_full_screen_toggles_it_back(self):
        self.api.set_display("fullscreen")
        self.api.set_display("maximized")
        self.assertEqual(self.win.calls.count("fullscreen"), 2)
        self.assertIn("maximize", self.win.calls)

    def test_a_window_is_resized_centred_and_remembered(self):
        r = self.api.set_display("window", 1280, 800)
        self.assertTrue(r["ok"])
        self.assertIn(("resize", 1280, 800), self.win.calls)
        self.assertIn(("move", 128, 80), self.win.calls)
        self.assertEqual(self.api.get_settings()["_window"], {"width": 1280, "height": 800})

    def test_borderless_takes_the_frame_off_and_the_others_put_it_back(self):
        self.api.set_display("borderless")
        self.api.set_display("window", 1280, 800)
        self.assertEqual(self.frames, [False, True])

    def test_an_unknown_mode_is_refused(self):
        self.assertFalse(self.api.set_display("sideways")["ok"])

    def test_f11_flips_and_reports(self):
        self.assertTrue(self.api.toggle_fullscreen()["fullscreen"])
        self.assertFalse(self.api.toggle_fullscreen()["fullscreen"])


class Notifications(unittest.TestCase):
    def setUp(self):
        self._run = app.subprocess.run
        self._reg = app._register_toast_identity
        app._register_toast_identity = lambda: None
        self.seen = {}

        def fake_run(args, **kw):
            self.seen["args"], self.seen["env"] = args, kw.get("env", {})

            class R:
                returncode = 0
                stderr = b""
            return R()

        app.subprocess.run = fake_run

    def tearDown(self):
        app.subprocess.run = self._run
        app._register_toast_identity = self._reg

    @unittest.skipUnless(os.name == "nt", "toasts are Windows-only")
    def test_text_travels_in_the_environment_not_the_command_line(self):
        nasty = "Ann \"O'Neil\" <script>$(calc)"
        self.assertTrue(app._toast(nasty, "msg & more"))
        self.assertEqual(self.seen["env"]["VKC_TITLE"], nasty)
        self.assertEqual(self.seen["env"]["VKC_MSG"], "msg & more")
        # nothing a person controls may appear in the script that is run
        self.assertNotIn("O'Neil", " ".join(self.seen["args"]))
        self.assertNotIn("calc", " ".join(self.seen["args"]))

    @unittest.skipUnless(os.name == "nt", "toasts are Windows-only")
    def test_the_script_names_the_application(self):
        app._toast("t", "m")
        self.assertIn("CreateToastNotifier('vk-chain')", " ".join(self.seen["args"]))

    def test_the_api_call_returns_at_once(self):
        calls = []
        old = app._toast
        app._toast = lambda t, m: calls.append((t, m)) or True
        try:
            r = app.Api().notify("a", "b")
            for _ in range(50):
                if calls:
                    break
                threading.Event().wait(0.02)
        finally:
            app._toast = old
        self.assertTrue(r["ok"])
        self.assertEqual(calls, [("a", "b")])


class Names(unittest.TestCase):
    """Profiles are fetched a hundred at a time, and kept."""

    def setUp(self):
        self._old = (app.PROFILES, vc.api)
        app.PROFILES = pathlib.Path(tempfile.mkdtemp(prefix="vkchain-nm-")) / "profiles.json"
        self.requests = []

        def fake_api(token, method, **params):
            ids = [int(x) for x in params["user_ids"].split(",")]
            self.requests.append(ids)
            return [{"id": i, "first_name": "Имя", "last_name": f"Фамилия{i}",
                     "photo_100": f"https://example.invalid/{i}.jpg", "domain": f"id{i}",
                     "city": {"title": "Москва"}} for i in ids]

        vc.api = fake_api
        self.api = app.Api()

    def tearDown(self):
        app.PROFILES, vc.api = self._old

    def test_never_more_than_a_hundred_per_request(self):
        got = self.api._names("t", list(range(1, 251)))
        self.assertEqual([len(r) for r in self.requests], [100, 100, 50])
        self.assertEqual(len(got), 250)

    def test_the_avatar_is_kept(self):
        got = self.api._names("t", [7])
        self.assertEqual(got[7]["photo_url"], "https://example.invalid/7.jpg")

    def test_known_people_are_not_asked_for_again(self):
        self.api._names("t", [1, 2, 3])
        self.requests.clear()
        self.api._names("t", [1, 2, 3, 4])
        self.assertEqual(self.requests, [[4]])

    def test_somebody_vk_will_not_describe_still_gets_an_entry(self):
        vc.api = lambda *a, **k: []
        got = self.api._names("t", [99])
        self.assertEqual(got[99]["name"], "99")

    def test_an_old_entry_without_a_picture_is_fetched_again(self):
        app.PROFILES.write_text(json.dumps({"5": {"name": "Старый", "city": ""}}), encoding="utf-8")
        got = self.api._names("t", [5])
        self.assertEqual(self.requests, [[5]])
        self.assertTrue(got[5]["photo_url"])


class Relatives(unittest.TestCase):
    """Namesakes are looked for among every friend and every friend of friends."""

    ROOT = 1

    def setUp(self):
        self.api = app.Api()
        self.window = FakeWindow()
        self.api.bind(self.window)
        self._saved = (vc.get_friends_batch, vc.resolve_user, vc.save_caches, vc.load_caches,
                       app.Api._token, app.Api._resolve, app.Api._names, app.Api._remember)
        # 1 knows 10, 11, 12. They know 20..23 between them.
        self.lists = {1: [10, 11, 12], 10: [1, 20, 21], 11: [1, 22], 12: [1, 23]}
        self.last = {1: "Петров", 10: "Орлов", 11: "Орлов", 12: "Орлов",
                     20: "Петрова", 21: "Сидоров", 22: "Петров", 23: "Иванова"}
        vc.get_friends_batch = lambda token, ids: {i: self.lists.get(i, []) for i in ids}
        vc.resolve_user = lambda token, ident: (self.ROOT, "Он Петров", "Он", "Петров")
        vc.save_caches = vc.load_caches = lambda: None
        app.Api._token = lambda self: "t"
        app.Api._resolve = lambda self, token, who: (1, "Он Петров", "Он", "Петров")
        app.Api._remember = lambda self, *a: None
        case = self   # inside the lambda, `self` is the Api it is patched onto
        app.Api._names = lambda self, token, ids, progress=None: {
            i: {"name": f"N{i}", "last_name": case.last.get(i, "Другой"), "maiden_name": "", "photo_url": ""} for i in ids}
        vc._friends_cache.clear()

    def tearDown(self):
        (vc.get_friends_batch, vc.resolve_user, vc.save_caches, vc.load_caches,
         app.Api._token, app.Api._resolve, app.Api._names, app.Api._remember) = self._saved

    def run_it(self, opts=None):
        self.api._relatives("id1", opts or {})
        return self.window.events[-1]

    def test_finds_namesakes_among_friends_of_friends_not_only_one_persons(self):
        res = self.run_it()
        self.assertEqual(res["type"], "result")
        ids = {i for e in res["edges"] for i in e}
        # 20 is a friend of 10, and 22 a friend of 11: two different people's friends
        self.assertTrue({20, 22} <= ids)
        self.assertNotIn(21, ids)   # not a namesake
        self.assertNotIn(23, ids)

    def test_the_friends_who_link_them_are_drawn_and_the_rest_are_not(self):
        res = self.run_it()
        ids = {i for e in res["edges"] for i in e}
        self.assertTrue({10, 11} <= ids)    # they introduce 20 and 22
        self.assertNotIn(12, ids)           # knows only a non-namesake

    def test_the_centre_is_joined_to_the_connectors(self):
        res = self.run_it()
        self.assertIn([1, 10], res["edges"])
        self.assertIn([10, 20], res["edges"])

    def test_a_namesake_who_is_a_direct_friend_is_joined_directly(self):
        self.last[12] = "Петрова"
        res = self.run_it()
        self.assertIn([1, 12], res["edges"])

    def test_nobody_found_is_said_so(self):
        for k in (20, 22):
            self.last[k] = "Другой"
        res = self.run_it()
        self.assertEqual(res["type"], "error")
        self.assertIn("не найдено", res["message"])

    def test_a_maiden_name_counts(self):
        app.Api._names = lambda self, token, ids, progress=None: {
            i: {"name": f"N{i}", "last_name": "Орлова", "maiden_name": "Петрова" if i == 21 else "", "photo_url": ""} for i in ids}
        res = self.run_it()
        self.assertIn(21, {i for e in res["edges"] for i in e})


class History(unittest.TestCase):
    def setUp(self):
        self.api = app.Api()
        hist = app.DATA / "history.json"
        if hist.exists():
            hist.unlink()

    def test_an_entry_keeps_what_it_takes_to_repeat_it(self):
        self.api._remember("chain", "Имя → Имя", "3 рукопожатия", {"from": "id1", "to": "durov"})
        got = self.api.history()[0]
        self.assertEqual(got["args"], {"from": "id1", "to": "durov"})
        self.assertEqual(got["type"], "chain")

    def test_newest_first_and_bounded(self):
        for i in range(60):
            self.api._remember("explore", f"q{i}", "m", {"center": str(i)})
        got = self.api.history()
        self.assertEqual(got[0]["query"], "q59")
        self.assertLessEqual(len(got), 40)

    def test_an_old_entry_without_arguments_still_reads(self):
        (app.DATA / "history.json").write_text(json.dumps([{"type": "chain", "query": "q", "meta": "m", "ts": 1}]), encoding="utf-8")
        self.assertEqual(self.api.history()[0]["query"], "q")


class RateLimit(unittest.TestCase):
    """VK's error 29 must not look like "this person has no friends"."""

    def setUp(self):
        self.api = app.Api()
        self.window = FakeWindow()
        self.api.bind(self.window)
        self._saved = (vc.get_friends_batch, vc.resolve_user, vc.save_caches, vc.load_caches,
                       app.Api._token, app.Api._resolve)
        vc.save_caches = vc.load_caches = lambda: None
        vc.resolve_user = lambda token, ident: (1, "Он Петров", "Он", "Петров")
        app.Api._token = lambda self: "t"
        app.Api._resolve = lambda self, token, who: (1, "Он Петров", "Он", "Петров")

    def tearDown(self):
        (vc.get_friends_batch, vc.resolve_user, vc.save_caches, vc.load_caches,
         app.Api._token, app.Api._resolve) = self._saved

    def refused(self, token, ids):
        _ = app._LIMIT.update(at=time.time())
        return {i: [] for i in ids}

    def test_a_refusal_is_reported_as_one(self):
        vc.get_friends_batch = self.refused
        self.api._started = time.time() - 1
        self.api._relatives("id1", {})
        msg = self.window.events[-1]["message"]
        self.assertIn("ошибка 29", msg)

    def test_a_private_profile_is_still_just_private(self):
        vc.get_friends_batch = lambda token, ids: {i: [] for i in ids}
        self.api._started = time.time()
        app._LIMIT["at"] = 0.0
        self.api._relatives("id1", {})
        self.assertEqual(self.window.events[-1]["message"], "друзья скрыты или их нет")

    def test_people_refused_during_a_search_are_not_cached_as_empty(self):
        vc._friends_cache.clear()
        done = threading.Event()

        def fn():
            vc._friends_cache[99] = []          # looks like "no friends"
            vc._friends_cache[100] = [1, 2]     # a real answer
            app._LIMIT["at"] = time.time()
            done.set()

        self.api._run("тест", fn)
        done.wait(2)
        for _ in range(100):
            if 99 not in vc._friends_cache:
                break
            time.sleep(0.02)
        self.assertNotIn(99, vc._friends_cache)
        self.assertIn(100, vc._friends_cache)

    def test_without_a_refusal_an_empty_list_is_kept(self):
        vc._friends_cache.clear()
        app._LIMIT["at"] = 0.0
        done = threading.Event()

        def fn():
            vc._friends_cache[7] = []
            done.set()

        self.api._run("тест", fn)
        done.wait(2)
        time.sleep(0.2)
        self.assertIn(7, vc._friends_cache)


class Guard(unittest.TestCase):
    def test_a_failure_reaches_the_window(self):
        got = []

        def boom():
            raise ValueError("плохо")

        app._guard("цепочка", boom, got.append)
        self.assertEqual(len(got), 1)
        self.assertIn("плохо", got[0])

    def test_vk_chain_exiting_is_not_silent(self):
        # vk_chain calls sys.exit() when there is no token; in a worker thread
        # that used to end the thread without a word.
        got = []

        def quit_():
            raise SystemExit(1)

        app._guard("граф", quit_, got.append)
        self.assertEqual(len(got), 1)
        self.assertIn("граф", got[0])


if __name__ == "__main__":
    unittest.main()
