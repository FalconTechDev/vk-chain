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

    def test_window_size_is_kept_within_sane_bounds(self):
        self.assertEqual(app._window_geometry({}), (1440, 900))
        self.assertEqual(app._window_geometry({"_window": {"width": 50, "height": 50}}), (1120, 720))
        w, h = app._window_geometry({"_window": {"width": 10**6, "height": 10**6}})
        self.assertLessEqual(w, 7680)
        self.assertLessEqual(h, 4320)


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
