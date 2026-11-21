import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from community import (APP, CACHE_SECONDS, PING_RETRY_SECONDS, PING_SECONDS, RETRY_SECONDS, Community, Reporter)
from controller import DEFAULTS, ControlError, validate

BASE = "https://example.invalid"


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.urls = []
        self.reply = json.dumps({"active_7d": 7, "active_30d": 9, "total": 12, "updated": "2026-11-21T10:00:00Z"}).encode()

    def fetch(self, url):
        self.urls.append(url)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def make(self, base=BASE):
        return Community(base, fetch=self.fetch, clock=lambda: self.now)

    def test_reads_and_caches(self):
        c = self.make()
        self.assertEqual(c.get(), {"installations": 7, "total": 12, "updated": "2026-11-21T10:00:00Z"})
        self.assertEqual(self.urls, [f"{BASE}/v1/stats?app={APP}"])
        self.now += CACHE_SECONDS - 1
        c.get()
        self.assertEqual(len(self.urls), 1)
        self.now += 2
        c.get()
        self.assertEqual(len(self.urls), 2)

    def test_failure_keeps_last_value_and_retries_sooner(self):
        c = self.make()
        c.get()
        self.now += CACHE_SECONDS + 1
        self.reply = OSError("offline")
        self.assertEqual(c.get()["installations"], 7)
        self.now += RETRY_SECONDS + 1
        self.reply = json.dumps({"active_7d": 9}).encode()
        self.assertEqual(c.get(), {"installations": 9, "total": 9, "updated": None})

    def test_rejects_bad_data(self):
        for reply in (b"nope", b"{}", b'{"active_7d": "5"}', b'{"active_7d": true}', b'{"active_7d": -1}',
                      b'{"active_7d": 1, "total": "x"}', b"[1]"):
            with self.subTest(reply=reply):
                self.reply = reply
                self.assertIsNone(self.make().get())

    def test_empty_base_disables(self):
        self.assertIsNone(self.make("").get())
        self.assertEqual(self.urls, [])


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.now = 5000.0
        self.posts = []
        self.fail = False

    def tearDown(self):
        self.dir.cleanup()

    def post(self, url, body):
        if self.fail:
            raise OSError("offline")
        self.posts.append((url, body))

    def make(self, base=BASE):
        return Reporter(base, "0.6.2", self.dir.name, post=self.post, clock=lambda: self.now)

    def test_pings_with_only_app_id_and_version(self):
        self.make().tick(True)
        url, body = self.posts[0]
        self.assertEqual(url, f"{BASE}/v1/ping")
        self.assertEqual(set(body), {"app", "id", "version"})
        self.assertEqual(body["app"], APP)
        self.assertEqual(body["version"], "0.6.2")
        self.assertRegex(body["id"], r"^[0-9a-f]{32}$")

    def test_id_survives_restart(self):
        self.make().tick(True)
        first = self.posts[0][1]["id"]
        self.make().tick(True)
        self.assertEqual(self.posts[1][1]["id"], first)

    def test_pings_every_6_hours_and_retries_hourly(self):
        r = self.make()
        r.tick(True)
        self.now += PING_SECONDS - 1
        r.tick(True)
        self.assertEqual(len(self.posts), 1)
        self.now += 1
        self.fail = True
        r.tick(True)
        self.fail = False
        self.now += PING_RETRY_SECONDS - 1
        r.tick(True)
        self.assertEqual(len(self.posts), 1)
        self.now += 1
        r.tick(True)
        self.assertEqual(len(self.posts), 2)

    def test_turning_off_forgets_once_right_away(self):
        r = self.make()
        r.tick(True)
        self.now += 60
        r.tick(False)
        r.tick(False)
        self.assertEqual(self.posts[1], (f"{BASE}/v1/forget", {"app": APP, "id": self.posts[0][1]["id"]}))
        self.assertEqual(len(self.posts), 2)
        self.make().tick(False)  # nothing more after a restart either
        self.assertEqual(len(self.posts), 2)
        r.tick(True)  # turning it on again pings at once
        self.assertEqual(self.posts[2][0], f"{BASE}/v1/ping")

    def test_never_shared_sends_nothing(self):
        self.make().tick(False)
        self.assertEqual(self.posts, [])
        self.assertFalse((Path(self.dir.name) / "usage.json").exists())

    def test_forget_retries_after_failure(self):
        r = self.make()
        r.tick(True)
        self.fail = True
        r.tick(False)
        self.fail = False
        r.tick(False)
        self.assertEqual(len(self.posts), 1)
        self.now += PING_RETRY_SECONDS
        r.tick(False)
        self.assertEqual(self.posts[-1][0], f"{BASE}/v1/forget")

    def test_corrupt_state_gets_new_id(self):
        (Path(self.dir.name) / "usage.json").write_text('{"id": "NOT-HEX-NOT-HEX-NOT-HEX-NOT-HEX!"}')
        self.make().tick(True)
        self.assertRegex(self.posts[0][1]["id"], r"^[0-9a-f]{32}$")

    def test_empty_base_disables(self):
        r = self.make("")
        r.tick(True)
        r.tick(False)
        self.assertEqual(self.posts, [])


class SettingTests(unittest.TestCase):
    def test_on_by_default_and_must_be_bool(self):
        self.assertIs(DEFAULTS["share_usage"], True)
        self.assertIs(validate({**DEFAULTS, "share_usage": False})["share_usage"], False)
        with self.assertRaises(ControlError):
            validate({**DEFAULTS, "share_usage": "nej"})

    def test_settings_from_older_version_get_it_on(self):
        import tempfile
        from unittest.mock import Mock
        from controller import Controller
        with tempfile.TemporaryDirectory() as path:
            old = {k: v for k, v in DEFAULTS.items() if k != "share_usage"}
            (Path(path) / "settings.json").write_text(json.dumps(old))
            self.assertIs(Controller(Mock(), path).config["share_usage"], True)


if __name__ == "__main__":
    unittest.main()
