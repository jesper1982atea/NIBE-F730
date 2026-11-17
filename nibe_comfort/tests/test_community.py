import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from community import CACHE_SECONDS, RETRY_SECONDS, Community


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.calls = 0
        self.reply = json.dumps({"installations_14d": 7, "updated": "2026-11-17"}).encode()

    def fetch(self, url):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def make(self):
        return Community("https://example.invalid/x.json", fetch=self.fetch, clock=lambda: self.now)

    def test_reads_and_caches(self):
        c = self.make()
        self.assertEqual(c.get(), {"installations": 7, "updated": "2026-11-17"})
        self.now += CACHE_SECONDS - 1
        c.get()
        self.assertEqual(self.calls, 1)
        self.now += 2
        c.get()
        self.assertEqual(self.calls, 2)

    def test_failure_keeps_last_value_and_retries_sooner(self):
        c = self.make()
        c.get()
        self.now += CACHE_SECONDS + 1
        self.reply = OSError("offline")
        self.assertEqual(c.get()["installations"], 7)
        self.now += RETRY_SECONDS + 1
        self.reply = json.dumps({"installations_14d": 9}).encode()
        self.assertEqual(c.get(), {"installations": 9, "updated": None})

    def test_rejects_bad_data(self):
        for reply in (b"nope", b"{}", b'{"installations_14d": "5"}', b'{"installations_14d": true}',
                      b'{"installations_14d": -1}', b"[1]"):
            with self.subTest(reply=reply):
                self.reply = reply
                self.assertIsNone(self.make().get())


if __name__ == "__main__":
    unittest.main()
