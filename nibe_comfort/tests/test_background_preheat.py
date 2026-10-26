import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_controller import config, fixtures
from controller import ControlError, Controller
from insights import Insights
from app import handler_for


class BackgroundPreheatTests(unittest.TestCase):
    def setUp(self):
        self.ha = Mock()
        self.ha.states.side_effect = lambda: fixtures()
        self.ha.integration_registry.return_value = []
        self.insights = Insights(self.ha)
        self.cfg = config(preheat_enabled=True, price_entity="sensor.price")
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.result = {"windows": [{"start": 0, "end": time.time() + 3600, "shift": 0.5, "kind": "preheat"}]}

        def slow(config):
            self.started.set()
            if not self.release.wait(5):
                raise ControlError("Test loader timed out")
            return self.result

        self.insights.preheat = Mock(side_effect=slow)
        original = self.insights._load_control_preheat

        def load(*args):
            try:
                return original(*args)
            finally:
                self.finished.set()

        self.insights._load_control_preheat = load
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        if self.started.is_set():
            self.finished.wait(3)

    def finish(self):
        self.release.set()
        self.assertTrue(self.finished.wait(3))

    def test_history_does_not_block_current_readings_and_only_one_worker_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = Controller(self.ha, folder)
            controller.save(self.cfg)
            handler_for(controller, insights=self.insights)
            controller.tick()
            self.assertTrue(self.started.wait(2))
            self.assertIsNotNone(controller.snapshot()["checked_at"])
            self.assertEqual(controller.snapshot()["target_shift"], 0)
            self.assertIn("bakgrunden", controller.snapshot()["preheat"]["note"])
            for _ in range(10):
                controller.tick()
            self.insights.preheat.assert_called_once()
            self.finish()
            controller.tick()
            self.assertEqual(controller.snapshot()["target_shift"], 0.5)
            snapshot = self.insights.control_preheat(self.cfg)
            snapshot["windows"].clear()
            self.assertTrue(self.insights.control_preheat(self.cfg)["windows"])
        self.ha.set_offset.assert_not_called()

    def test_configuration_change_never_reuses_an_old_schedule(self):
        with self.assertRaises(ControlError):
            self.insights.control_preheat(self.cfg)
        self.assertTrue(self.started.wait(2))
        self.finish()
        changed = {**self.cfg, "price_provider": "nordpool"}
        self.finished.clear()
        with self.assertRaisesRegex(ControlError, "bakgrunden"):
            self.insights.control_preheat(changed)
        self.assertTrue(self.finished.wait(2))
        self.assertEqual(self.insights.preheat.call_count, 2)

    def test_error_is_explicit_and_retries_are_rate_limited(self):
        self.insights.preheat.side_effect = ControlError("HA offline")
        with self.assertRaises(ControlError):
            self.insights.control_preheat(self.cfg)
        self.assertTrue(self.finished.wait(2))
        for _ in range(5):
            with self.assertRaisesRegex(ControlError, "HA offline"):
                self.insights.control_preheat(self.cfg)
        self.insights.preheat.assert_called_once()
        _, stamp, error = self.insights.control_schedule
        with patch("insights.time.monotonic", return_value=stamp + 61):
            self.finished.clear()
            self.insights.preheat.side_effect = None
            self.insights.preheat.return_value = self.result
            with self.assertRaisesRegex(ControlError, "HA offline"):
                self.insights.control_preheat(self.cfg)
            self.assertTrue(self.finished.wait(2))
            self.assertTrue(self.insights.control_preheat(self.cfg)["windows"])

    def test_expired_plan_not_used_while_refresh_is_slow(self):
        self.insights.control_schedule = (
            (json.dumps(self.cfg, sort_keys=True), []), time.monotonic() - 901, self.result)
        with self.assertRaisesRegex(ControlError, "bakgrunden"):
            self.insights.control_preheat(self.cfg)
        self.assertTrue(self.started.wait(2))
        self.finish()

    def test_current_price_validation_applies_to_background_schedules(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = Controller(self.ha, folder)
            controller.save(self.cfg)
            controller.preheat_provider = lambda cfg: self.result
            controller.tick()
            self.assertEqual(controller.snapshot()["target_shift"], 0.5)
            states = fixtures()
            next(e for e in states if e["entity_id"] == "sensor.price")["state"] = "unavailable"
            self.ha.states.side_effect = lambda: states
            controller.tick()
            self.assertEqual(controller.snapshot()["target_shift"], 0)
            self.assertIn("otillgänglig", controller.snapshot()["preheat"]["note"])


if __name__ == "__main__":
    unittest.main()
