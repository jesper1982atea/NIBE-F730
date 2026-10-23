import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller import ControlError, Controller, evaluate, validate
from insights import Insights, planned_offsets
from learning import (DEFAULT_LEAD, LearningStore, hourly_means, lead_hours, learn, predict, preheat_windows,
                      shift_at)
from test_controller import config, fixtures

HOUR = 3600


def synthetic_house(hours=240, tau=10.0, gain=0.2, seed=1):
    """First-order house: indoor drifts towards 18 + gain*offset with time constant tau (lag 0)."""
    rng = random.Random(seed)
    start = 1_700_000_000 // HOUR * HOUR
    offsets = [(-2, 0, 3, 1, -1, 2)[(h // 12) % 6] for h in range(hours + 1)]
    indoor, outdoor, offset = {}, {}, {}
    value = 18.0
    for h in range(hours):
        stamp = start + h * HOUR
        indoor[stamp], outdoor[stamp], offset[stamp] = value, 0.0 + rng.uniform(-1, 1), offsets[h]
        u = (offsets[h] + offsets[h - 1]) / 2 if h else offsets[h]
        value += (18 + gain * u * tau - value) / tau + rng.gauss(0, 0.005)
    return start, indoor, outdoor, offset


class StoreTests(unittest.TestCase):
    def test_memory_and_sqlite_store_replace_and_prune(self):
        with tempfile.TemporaryDirectory() as folder:
            for store in (LearningStore(), LearningStore(f"{folder}/sub/learning.sqlite")):
                now = 200 * 86400
                store.add([("a", now - HOUR, 1.0), ("a", now - HOUR, 2.0), ("a", 0, 5.0), ("b", now, float("nan"))], now)
                self.assertEqual(store.load(["a", "b"]), {"a": {now - HOUR: 2.0}, "b": {}})
            self.assertEqual(LearningStore(f"{folder}/sub/learning.sqlite").load(["a"])["a"], {now - HOUR: 2.0})

    def test_hourly_means_need_half_the_hour(self):
        steps = [[0, 20.0], [1800, 22.0], [3 * HOUR, None], [3 * HOUR + 2000, 21.0]]
        means = hourly_means(steps, 0, 4 * HOUR)
        self.assertAlmostEqual(means[0], 21.0)
        self.assertEqual(means[HOUR], 22.0)
        self.assertNotIn(3 * HOUR, means)
        self.assertEqual(hourly_means([], 0, HOUR), {})


class ModelTests(unittest.TestCase):
    def test_fit_recovers_time_constant_and_gain(self):
        _, indoor, outdoor, offset = synthetic_house()
        model = learn(indoor, outdoor, offset, max(indoor))
        self.assertEqual(model["status"], "good")
        facts = model["facts"]
        self.assertAlmostEqual(facts["degrees_per_step"], 2.0, delta=0.3)
        self.assertAlmostEqual(facts["time_constant_hours"], 10, delta=2)
        self.assertLessEqual(facts["lag_hours"], 1)
        self.assertGreater(model["skill"], 0.5)
        self.assertLess(lead_hours(model, 0.5), DEFAULT_LEAD + 1)

    def test_learning_status_is_honest(self):
        _, indoor, outdoor, offset = synthetic_house(hours=48)
        self.assertEqual(learn(indoor, outdoor, offset, 0)["status"], "learning")
        _, indoor, outdoor, offset = synthetic_house(hours=100)
        model = learn(indoor, outdoor, offset, 0)
        self.assertEqual(model["status"], "rough")
        self.assertIn("4,2 av 7 dygn", model["message"])
        self.assertEqual(lead_hours(model, 0.5), DEFAULT_LEAD)
        flat = {h: 21.0 for h in indoor}
        self.assertEqual(learn(flat, outdoor, {h: 0 for h in indoor}, 0)["status"], "learning")

    def test_prediction_moves_towards_new_equilibrium(self):
        start, indoor, outdoor, offset = synthetic_house()
        model = learn(indoor, outdoor, offset, 0)
        points = predict(model, 18.0, 0, lambda t: 3, lambda t: 0)
        self.assertEqual(len(points), 25)
        self.assertGreater(points[-1][1], points[1][1])
        self.assertEqual(predict(None, 20, 0, lambda t: 0, lambda t: 0), [])


class PreheatWindowTests(unittest.TestCase):
    def prices(self, expensive=(17, 18, 19), base=1.0, high=3.0, start=0):
        return [[start + h * HOUR, high if h in expensive else base] for h in range(24)]

    def test_preheats_before_and_coasts_during_expensive_block(self):
        result = preheat_windows(self.prices(), 0, 3, 0.5)
        self.assertEqual([(w["kind"], w["start"] // HOUR, w["end"] // HOUR, w["shift"]) for w in result["windows"]],
                         [("preheat", 14, 17, 0.5), ("coast", 17, 20, -0.5)])
        self.assertEqual(shift_at(result["windows"], 15 * HOUR)["kind"], "preheat")
        self.assertIsNone(shift_at(result["windows"], 21 * HOUR))

    def test_no_preheat_when_before_is_not_cheaper_or_prices_flat(self):
        prices = [[h * HOUR, 1.2 if 14 <= h < 17 else (1.4 if 17 <= h < 20 else 1.0)] for h in range(24)]
        kinds = [w["kind"] for w in preheat_windows(prices, 0, 3, 0.5)["windows"]]
        self.assertEqual(kinds, ["coast"])
        flat = preheat_windows([[h * HOUR, 1.0] for h in range(24)], 0, 3, 0.5)
        self.assertEqual(flat["windows"], [])
        self.assertTrue(flat["note"])
        self.assertEqual(preheat_windows([[0, 1.0]], 0, 3, 0.5)["windows"], [])
        self.assertEqual(preheat_windows([[h * HOUR, 1.0] for h in range(4)], 0, 3, 0.5)["windows"], [])

    def test_quarter_hour_prices_merge_into_one_block(self):
        prices = [[q * 900, 3.0 if 68 <= q < 76 else 1.0] for q in range(96)]
        windows = preheat_windows(prices, 0, 2, 0.4)["windows"]
        self.assertEqual([(w["kind"], w["start"], w["end"]) for w in windows],
                         [("preheat", 15 * HOUR, 17 * HOUR), ("coast", 17 * HOUR, 19 * HOUR)])


class ControllerPreheatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.states.side_effect = lambda: fixtures()
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)

    def test_validation(self):
        self.assertFalse(config()["preheat_enabled"])
        with self.assertRaisesRegex(ControlError, "elprisgivare"):
            config(preheat_enabled=True)
        with self.assertRaises(ControlError):
            config(preheat_enabled="ja", price_entity="sensor.price")
        with self.assertRaises(ControlError):
            config(preheat_degrees=3, price_entity="sensor.price")

    def test_evaluate_shift_only_when_enabled_and_clamped(self):
        now = time.time()
        plain = evaluate(config(target_temperature=21), fixtures(now), now, target_shift=1)
        self.assertEqual(plain["target_shift"], 0)
        cfg = config(target_temperature=21, preheat_enabled=True, preheat_degrees=0.5, price_entity="sensor.price")
        shifted = evaluate(cfg, fixtures(now), now, target_shift=1)
        self.assertEqual(shifted["target_shift"], 0.5)
        self.assertAlmostEqual(shifted["error"], plain["error"] + 0.5)
        self.assertGreater(shifted["desired_offset"], plain["desired_offset"])

    def test_tick_uses_window_and_logs_once(self):
        now = time.time()
        self.controller.save(config(enabled=True, target_temperature=21, preheat_enabled=True, price_entity="sensor.price"))
        self.controller.preheat_provider = lambda cfg: {"windows": [{"kind": "preheat", "start": now - 60, "end": now + 3600, "shift": 0.5}]}
        self.controller.tick()
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertEqual(status["target_shift"], 0.5)
        self.assertEqual(status["preheat"]["kind"], "preheat")
        self.assertEqual(sum(1 for e in self.controller.events.recent() if e["kind"] == "preheat"), 1)

    def test_provider_failure_falls_back_to_plain_control(self):
        self.controller.save(config(enabled=True, target_temperature=21, preheat_enabled=True, price_entity="sensor.price"))

        def broken(cfg):
            raise RuntimeError("Tibber svarar inte")
        self.controller.preheat_provider = broken
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertIsNone(status["error_message"])
        self.assertEqual(status["target_shift"], 0)
        self.assertIn("Tibber svarar inte", status["preheat"]["note"])

    def test_old_settings_file_gets_new_keys(self):
        saved = config()
        del saved["preheat_enabled"], saved["preheat_degrees"]
        (Path(self.temp.name) / "settings.json").write_text(__import__("json").dumps(saved))
        controller = Controller(self.ha, self.temp.name)
        self.assertFalse(controller.settings()["preheat_enabled"])
        self.assertEqual(controller.settings()["preheat_degrees"], 0.5)


class PlanWithPreheatTests(unittest.TestCase):
    def test_plan_raises_before_and_lowers_during_expensive_hours(self):
        now = 1_700_000_000.0
        cfg = config(preheat_enabled=True, price_entity="sensor.price", room_gain=2, deadband=0.3)
        status = {"error_message": None, "error": 0.0, "target_shift": 0, "current_offset": 0,
                  "offset_limits": {"min": -10, "step": 1}, "contributions": {"room": 0, "weather": 0, "price": 0}}
        windows = [{"kind": "preheat", "start": now + 2 * HOUR, "end": now + 5 * HOUR, "shift": 0.5},
                   {"kind": "coast", "start": now + 5 * HOUR, "end": now + 8 * HOUR, "shift": -0.5}]
        plan = planned_offsets(cfg, status, [], [], now, now + 24 * HOUR, False, windows)
        offsets = [(c["timestamp"] - now) / HOUR for c in plan["changes"]]
        self.assertEqual(offsets[0], 2)
        self.assertIn("förvärmer", plan["changes"][0]["reasons"][0])
        self.assertGreater(plan["changes"][0]["offset"], 0)
        self.assertTrue(any(c["offset"] < 0 for c in plan["changes"]))
        plain = planned_offsets(config(), status, [], [], now, now + 24 * HOUR, False, windows)
        self.assertEqual(plain["changes"], [])


class InsightsLearningTests(unittest.TestCase):
    def test_model_and_preheat_cache_follow_priority_rooms(self):
        from urllib.parse import parse_qs, urlparse
        ha = Mock()
        ha.request.return_value = []
        ha.states.return_value = fixtures()
        ha.integration_registry.return_value = []
        with tempfile.TemporaryDirectory() as folder:
            insights = Insights(ha, folder)
            cfg = config(priority_sensors=["sensor.living"])
            insights.house_model(cfg)
            query = parse_qs(urlparse(ha.request.call_args_list[0].args[0]).query)
            self.assertEqual(query["filter_entity_id"], ["sensor.living"])
            calls = ha.request.call_count
            insights.house_model(cfg)
            self.assertEqual(ha.request.call_count, calls)
            cfg["priority_sensors"] = ["sensor.bedroom"]
            insights.house_model(cfg)
            query = parse_qs(urlparse(ha.request.call_args_list[calls].args[0]).query)
            self.assertEqual(query["filter_entity_id"], ["sensor.bedroom"])
            with patch.object(insights, "house_model", return_value={"model": None}) as model, \
                    patch.object(insights, "prices", return_value=[]):
                insights.preheat(cfg)
                insights.preheat(cfg)
                self.assertEqual(model.call_count, 1)
                cfg["priority_sensors"] = ["sensor.living"]
                insights.preheat(cfg)
                self.assertEqual(model.call_count, 2)

    def test_learning_without_history_or_tibber_reports_learning(self):
        ha = Mock()
        ha.request.return_value = []
        ha.states.return_value = fixtures()
        ha.integration_registry.return_value = []
        with tempfile.TemporaryDirectory() as folder:
            insights = Insights(ha, folder)
            cfg = config(preheat_enabled=True, price_entity="sensor.price")
            result = insights.learning(cfg, {"indoor": 21, "current_offset": 0})
            self.assertEqual(result["model"]["status"], "learning")
            self.assertEqual(result["prediction"], [])
            self.assertIn("Tibber", result["preheat"]["note"])
            self.assertTrue((Path(folder) / "learning.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
