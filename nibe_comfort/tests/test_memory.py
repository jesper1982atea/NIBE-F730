import threading
import unittest
from io import BytesIO
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from test_controller import config, entity, fixtures
from controller import ControlError, HomeAssistant
from insights import Insights


class CacheLoadTests(unittest.TestCase):
    def concurrent_load(self, failure=False):
        insights = Insights(Mock())
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        counter_lock = threading.Lock()
        calls, waiters = [], []

        class TrackedFuture(Future):
            def result(self, timeout=None):
                with counter_lock:
                    waiters.append(1)
                    if len(waiters) == 3:
                        waiting.set()
                return super().result(timeout)

        def load():
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release loader")
            if failure:
                raise ControlError("Historikfel")
            return {"series": [[1, 21]]}

        with patch("insights.Future", TrackedFuture), ThreadPoolExecutor(max_workers=4) as pool:
            owner = pool.submit(insights.cached, "history", 300, load)
            try:
                self.assertTrue(entered.wait(2))
                followers = [pool.submit(insights.cached, "history", 300, load) for _ in range(3)]
                self.assertTrue(waiting.wait(2))
                self.assertEqual(len(calls), 1, "Only one large history allocation is allowed per cache key")
                # Other keys, including nested dependencies, must still make progress.
                self.assertEqual(insights.cached("other", 30, lambda: insights.cached("nested", 30, lambda: 7)), 7)
            finally:
                release.set()
            futures = [owner, *followers]
            if failure:
                for future in futures:
                    with self.assertRaisesRegex(ControlError, "Historikfel"):
                        future.result(2)
                self.assertNotIn("history", insights.cache)
                self.assertEqual(insights.cached("history", 300, lambda: "recovered"), "recovered")
            else:
                results = [future.result(2) for future in futures]
                results[0]["series"].clear()
                self.assertEqual(results[1]["series"], [[1, 21]])
                self.assertEqual(insights.cached("history", 300, load)["series"], [[1, 21]])
            self.assertEqual(insights.loading, {})

    def test_four_simultaneous_requests_load_once_and_return_independent_results(self):
        self.concurrent_load()

    def test_failure_reaches_all_waiters_and_later_requests_can_retry(self):
        self.concurrent_load(failure=True)

    def test_cache_variants_are_bounded_and_old_entries_expire(self):
        insights = Insights(Mock())
        with patch("insights.time.time", return_value=100):
            for i in range(100):
                insights.cached(("changed-settings", i), 300, lambda: [0] * 100)
        self.assertLessEqual(len(insights.cache), 24)
        with patch("insights.time.time", return_value=3800):
            insights.cached("new", 300, lambda: 1)
        self.assertEqual(list(insights.cache), ["new"])

    def test_busy_history_returns_explicit_error_and_releases_after_failure(self):
        insights = Insights(Mock())
        insights.history_lock = Mock()
        insights.history_lock.acquire.return_value = False
        with self.assertRaisesRegex(ControlError, "upptagen"):
            insights.history_request("/history/period/test")
        insights.ha.request.assert_not_called()
        insights.history_lock.acquire.return_value = True
        insights.ha.request.side_effect = ControlError("HA offline")
        with self.assertRaisesRegex(ControlError, "HA offline"):
            insights.history_request("/history/period/test")
        insights.history_lock.release.assert_called_once()

    def test_different_history_requests_are_serialized(self):
        insights = Insights(Mock())
        entered, attempted, release = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()

        class TrackedLock:
            def acquire(self, timeout):
                if entered.is_set():
                    attempted.set()
                return lock.acquire(timeout=timeout)

            def release(self):
                lock.release()

        insights.history_lock = TrackedLock()

        def request(path):
            if path == "first":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release history")
            return path

        insights.ha.request.side_effect = request
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(insights.history_request, "first")
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(insights.history_request, "second")
                self.assertTrue(attempted.wait(2))
                insights.ha.request.assert_called_once_with("first")
            finally:
                release.set()
            self.assertEqual([first.result(2), second.result(2)], ["first", "second"])

    def test_identical_request_wait_has_bounded_timeout(self):
        insights = Insights(Mock())
        pending = Mock()
        pending.result.side_effect = TimeoutError()
        pending.done.return_value = False
        insights.loading["busy"] = pending
        with self.assertRaisesRegex(ControlError, "redan på att hämtas"):
            insights.cached("busy", 300, lambda: self.fail("Must not duplicate active load"))
        pending.result.assert_called_once_with(timeout=15)

    def test_history_response_is_bounded_before_json_decoding_and_closed(self):
        stream = BytesIO(b"[" + b" " * (8 * 1024 * 1024) + b"]")
        with patch("controller.urlopen", return_value=stream), patch("controller.json.loads") as decode:
            with self.assertRaisesRegex(ControlError, "8 MiB"):
                HomeAssistant("test").request("/history/period/test")
            decode.assert_not_called()
        self.assertTrue(stream.closed)
        with patch("controller.urlopen", return_value=BytesIO(b'[[{"state":"21"}]]')):
            self.assertEqual(HomeAssistant("test").request("/history/period/test"), [[{"state": "21"}]])
        with patch("controller.urlopen", return_value=BytesIO(b"[]" + b" " * (8 * 1024 * 1024 - 2))):
            self.assertEqual(HomeAssistant("test").request("/history/period/test"), [])


class ModelHistoryMemoryTests(unittest.TestCase):
    def test_chunk_boundaries_preserve_unavailable_hours_and_fahrenheit(self):
        now = 1_800_000_000 // 3600 * 3600
        ha = Mock()
        ha._temperature_unit = "°F"
        ha.states.return_value = fixtures(now) + [
            entity("climate.room", "heat", now, current_temperature=69.8)]
        cfg = config(room_sensors=["climate.room"], outdoor_entity="sensor.outdoor")
        missing = []

        def request(path):
            parsed = urlsplit(path)
            start = datetime.fromisoformat(unquote(parsed.path).rsplit("/", 1)[1]).timestamp()
            ids = parse_qs(parsed.query)["filter_entity_id"][0].split(",")
            if "climate.room" in ids:
                missing.append(start)
                return [
                    [entity("climate.room", "unavailable", start),
                     entity("climate.room", "heat", start + 3600, current_temperature=69.8)],
                    [entity("sensor.outdoor", 41, start, unit_of_measurement="°F")],
                ]
            return [[entity("number.offset", 0, start)]]

        ha.request.side_effect = request
        with patch("insights.time.time", return_value=now):
            result = Insights(ha).house_model(cfg)
        self.assertEqual(len(result["indoor"]), 161)
        for hour in missing:
            self.assertNotIn(hour, result["indoor"], "No carry of the previous day's temperature over unavailable data")
        for value in result["indoor"].values():
            self.assertAlmostEqual(value, 21)
        for value in result["outdoor"].values():
            self.assertAlmostEqual(value, 5)

    def test_daily_backfill_then_incremental_refresh_preserves_climate_attributes(self):
        now = 1_800_000_000 // 3600 * 3600
        ha = Mock()
        ha._temperature_unit = "°C"
        ha.states.return_value = fixtures(now) + [
            entity("climate.room", "heat", now, current_temperature=21, temperature=22)]
        cfg = config(room_sensors=["climate.room"], outdoor_entity="sensor.outdoor")
        windows = []

        def request(path):
            parsed = urlsplit(path)
            start = datetime.fromisoformat(unquote(parsed.path).rsplit("/", 1)[1]).timestamp()
            query = parse_qs(parsed.query, keep_blank_values=True)
            end = datetime.fromisoformat(query["end_time"][0]).timestamp()
            windows.append((start, end, query))
            self.assertLessEqual(end - start, 86400, "Never allocate a seven-day temperature response")
            ids = query["filter_entity_id"][0].split(",")
            if "climate.room" in ids:
                self.assertNotIn("no_attributes", query, "Climate temperatures are attributes, not the HVAC state")
                self.assertNotIn("minimal_response", query, "Every climate measurement must retain its attributes")
            return [[entity(eid, "heat" if eid.startswith("climate.") else (0 if eid.startswith("number.") else 5),
                            start, current_temperature=21, temperature=22, unit_of_measurement="°C")]
                    for eid in ids]

        ha.request.side_effect = request
        insights = Insights(ha)
        with patch("insights.time.time", return_value=now):
            first = insights.house_model(cfg)
        self.assertEqual(len(windows), 14)
        self.assertEqual(first["model"]["hours"], 168)
        self.assertTrue(all(value == 21 for value in first["indoor"].values()))
        windows.clear()
        with patch("insights.time.time", return_value=now + 3601):
            second = insights.house_model(cfg)
        self.assertLessEqual(len(windows), 8)
        self.assertGreaterEqual(min(start for start, _, _ in windows), now - 3 * 86400)
        self.assertGreaterEqual(second["model"]["hours"], 168, "Existing persisted history is not discarded")


if __name__ == "__main__":
    unittest.main()
