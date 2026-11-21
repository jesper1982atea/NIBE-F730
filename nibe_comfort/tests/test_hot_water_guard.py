"""Varmvatten inför dyr el: laddning före dyra timmar och planens starttid."""

import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller, next_expensive_start
from test_controller import config, entity
from test_energywizard_ideas import cfg, house

NOW = 1_800_000_000.0


class NextExpensiveStartTests(unittest.TestCase):
    def test_finds_the_next_expensive_slot_within_the_horizon(self):
        # Cheap for an hour, then expensive for two hours (15-minute slots).
        prices = [[NOW + k * 900, p] for k, p in enumerate([1] * 4 + [3] * 8 + [1] * 4)]
        self.assertEqual(next_expensive_start(cfg(), prices, NOW + 60, 7200), NOW + 3600)
        self.assertEqual(next_expensive_start(cfg(), prices, NOW + 60, 3600), NOW + 3600)
        self.assertIsNone(next_expensive_start(cfg(), prices, NOW + 60, 1800), "outside the horizon")
        self.assertIsNone(next_expensive_start(cfg(), prices, NOW + 3600 + 60, 7200), "already expensive")
        self.assertIsNone(next_expensive_start(cfg(), prices, NOW + 60, 0), "turned off")
        self.assertIsNone(next_expensive_start(cfg(), [], NOW, 7200))
        self.assertIsNone(next_expensive_start(cfg(), prices, NOW - 7200, 7200), "now is not in the series")

    def test_relative_mode_ranks_within_the_day(self):
        day = datetime(2026, 11, 17).timestamp()
        series = [[day + h * 3600, float(h)] for h in range(24)]
        relative = cfg(price_mode="relative")
        # 75 % of the day's prices: hour 18 onwards (rank 18/23 ≥ 0.75) is expensive.
        self.assertEqual(next_expensive_start(relative, series, day + 16 * 3600 + 60, 7200), day + 18 * 3600)
        self.assertIsNone(next_expensive_start(relative, series, day + 3 * 3600, 7200))


class PrechargeTests(unittest.TestCase):
    MODE = "select.pump_comfort_mode"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = [
            {"entity_id": "number.offset", "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
            {"entity_id": self.MODE, "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
        ]
        self.ha.states.side_effect = lambda: house(now=time.time())
        self.controller = Controller(self.ha, self.temp.name)

    def step(self, starts_at, level=None, minutes=120.0, current="Economy", now=NOW):
        self.controller.save(config(enabled=True, price_entity="sensor.price", hot_water_price_enabled=True,
                                    hot_water_mode_entity=self.MODE, hot_water_precharge_minutes=minutes))
        state = entity(self.MODE, current, now, options=["Economy", "Normal"])
        return self.controller.hot_water_step([state], now, {"price_level": level, "price": 2.0,
                                                             "expensive_starts_at": starts_at}, {"kind": None}, False)

    def test_charges_before_an_expensive_period(self):
        report = self.step(NOW + 3600)
        self.assertEqual(report["desired"], "Normal")
        self.assertIn("inför dyr el kl", report["reason"])
        self.ha.set_select.assert_called_once_with(self.MODE, "Normal")

    def test_no_expensive_period_ahead_or_turned_off_keeps_the_usual_mode(self):
        self.assertEqual(self.step(None)["desired"], "Economy")
        self.assertEqual(self.step(NOW + 3600, minutes=0)["desired"], "Economy")
        self.ha.set_select.assert_not_called()

    def test_cheap_price_reason_wins(self):
        self.assertEqual(self.step(NOW + 3600, level="cheap", current="Normal")["reason"], "billig el")

    def test_control_plan_and_schedule_need_follow_the_setting(self):
        prices = [[NOW + k * 900, p] for k, p in enumerate([1] * 4 + [3] * 8)]
        self.controller.save(config(price_entity="sensor.price", hot_water_price_enabled=True,
                                    hot_water_mode_entity=self.MODE, price_low=1.5, price_high=2.5))
        plan = self.controller.control_plan(NOW + 60, {"prices": prices})
        self.assertEqual(plan["expensive_starts_at"], NOW + 3600)
        # The background price schedule is fetched for the pre-charge alone.
        self.controller.preheat_provider = Mock(return_value={"windows": [], "prices": prices})
        self.assertEqual(self.controller.preheat_schedule()["prices"], prices)
        self.controller.save(config(price_entity="sensor.price", hot_water_price_enabled=True,
                                    hot_water_mode_entity=self.MODE, hot_water_precharge_minutes=0))
        self.assertIsNone(self.controller.preheat_schedule())


if __name__ == "__main__":
    unittest.main()
