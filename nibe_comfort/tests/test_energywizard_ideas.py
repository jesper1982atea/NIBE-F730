"""Behovsanpassad sänkning, solstyrning, baskorrigering med kort minne, varmvattenvanor och skuggläge."""

import math
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import (ControlError, Controller, SUN_HEATING_DROP, adaptive_reduction, evaluate, expensive_until,
                        habit_due, indoor_rate)
from insights import habit_profile, merge_habits, sunny_hours
from test_controller import config, entity

NOW = 1_800_000_000.0
# indoor[h+1] = indoor + 0.42 - 0.02 * indoor + 0.04 * offset + 0.01 * outdoor: 21 °C at offset 0 and 0 °C outside.
BETA = [0.42, -0.02, 0.04, 0.01]
GOOD = {"status": "good", "beta": BETA, "lag": 0}


def house(living=21, bedroom=21, kitchen=None, price=3, outdoor=0, solar=None, now=NOW):
    rows = [
        entity("sensor.living", living, now, unit_of_measurement="°C"),
        entity("sensor.bedroom", bedroom, now, unit_of_measurement="°C"),
        entity("sensor.outdoor", outdoor, now, unit_of_measurement="°C"),
        entity("sensor.price", price, now, unit_of_measurement="SEK/kWh"),
        entity("number.offset", 0, now, min=-10, max=10, step=1),
    ]
    if kitchen is not None:
        rows.append(entity("sensor.kitchen", kitchen, now, unit_of_measurement="°C"))
    if solar is not None:
        rows.append(entity("sensor.pv_power", solar, now, unit_of_measurement="W"))
    return rows


def cfg(**overrides):
    return config(price_entity="sensor.price", outdoor_entity="sensor.outdoor", expensive_reduction=1, **overrides)


class ValidationTests(unittest.TestCase):
    def test_new_rules_are_off_by_default_and_validated(self):
        base = config()
        self.assertEqual((base["adaptive_reduction_mode"], base["solar_mode"], base["hot_water_habit_mode"]),
                         ("off", "off", "off"))
        self.assertEqual(base["auto_base_memory_days"], 0)
        for bad in ({"solar_mode": "maybe"}, {"solar_mode": "on"}, {"solar_mode": "shadow", "solar_power_entity": "number.x"},
                    {"adaptive_reduction_mode": "on"}, {"hot_water_habit_mode": "shadow"},
                    {"auto_base_memory_days": 31}, {"solar_power_threshold": 0}):
            with self.assertRaises(ControlError, msg=bad):
                config(**bad)
        config(solar_mode="shadow", solar_power_entity="sensor.pv_power")
        cfg(adaptive_reduction_mode="on")

    def test_without_the_new_rules_control_is_unchanged(self):
        result = evaluate(cfg(), house(), NOW)
        self.assertIsNone(result["adaptive_reduction"])
        self.assertEqual(result["solar"]["mode"], "off")
        self.assertIsNone(result["solar"]["power"])
        self.assertEqual(result["contributions"]["price"], -1)


class AdaptiveReductionTests(unittest.TestCase):
    def plan(self, hours=3, model=GOOD):
        return {"model": model, "expensive_until": NOW + hours * 3600}

    def test_full_reduction_when_the_house_can_take_it(self):
        result = evaluate(cfg(adaptive_reduction_mode="on"), house(), NOW, plan=self.plan())
        self.assertEqual(result["adaptive_reduction"]["reduction"], 1)
        self.assertEqual(result["contributions"]["price"], -1)

    def test_smaller_reduction_when_the_coldest_rooms_are_near_the_floor(self):
        # Mean 21 (no comfort deficit), but the two coldest rooms are at 20.75 with a warm third room.
        settings = cfg(adaptive_reduction_mode="on", room_sensors=["sensor.living", "sensor.bedroom", "sensor.kitchen"])
        result = evaluate(settings, house(living=20.75, bedroom=20.75, kitchen=21.5), NOW, plan=self.plan())
        info = result["adaptive_reduction"]
        self.assertEqual((info["start"], info["floor"]), (20.75, 20.7))
        self.assertLess(info["reduction"], 1)
        self.assertGreaterEqual(info["predicted_min"], 20.7)
        self.assertEqual(result["contributions"]["price"], -info["reduction"])

    def test_long_expensive_periods_reduce_less(self):
        short = adaptive_reduction(cfg(adaptive_reduction_mode="on"), self.plan(1), {"a": 20.8, "b": 20.8}, 0, 0, NOW)
        long = adaptive_reduction(cfg(adaptive_reduction_mode="on"), self.plan(8), {"a": 20.8, "b": 20.8}, 0, 0, NOW)
        self.assertGreater(short["reduction"], long["reduction"])

    def test_predicted_cooling_counts_but_predicted_warming_does_not(self):
        rooms = {"a": 20.8, "b": 20.8}
        settings = cfg(adaptive_reduction_mode="on")
        plain = adaptive_reduction(settings, self.plan(4), rooms, 0, 0, NOW)
        # A model that believes the house heads for 23 °C must not buy a bigger reduction.
        warm = adaptive_reduction(settings, self.plan(4, {**GOOD, "beta": [0.46, -0.02, 0.04, 0.01]}), rooms, 0, 0, NOW)
        self.assertEqual(warm["reduction"], plain["reduction"])
        # One that sees the house cooling towards 20 °C reduces nothing.
        cold = adaptive_reduction(settings, self.plan(4, {**GOOD, "beta": [0.40, -0.02, 0.04, 0.01]}), rooms, 0, 0, NOW)
        self.assertEqual(cold["reduction"], 0)

    def test_the_pump_lag_postpones_but_does_not_hide_the_effect(self):
        # Six hours of lag: a two-hour reduction cools the house only after the expensive period, and still counts.
        slow = adaptive_reduction(cfg(adaptive_reduction_mode="on"), self.plan(2, {**GOOD, "lag": 6}),
                                  {"a": 21.0, "b": 21.0}, 0, 0, NOW)
        self.assertEqual((slow["lag"], slow["reduction"]), (6, 1))
        self.assertLess(slow["predicted_min"], 21.0)

    def test_shadow_reports_without_changing_control(self):
        settings = cfg(adaptive_reduction_mode="shadow", room_sensors=["sensor.living", "sensor.bedroom", "sensor.kitchen"])
        result = evaluate(settings, house(living=20.75, bedroom=20.75, kitchen=21.5), NOW, plan=self.plan())
        self.assertLess(result["adaptive_reduction"]["reduction"], 1)
        self.assertFalse(result["adaptive_reduction"]["applied"])
        self.assertEqual(result["contributions"]["price"], -1)

    def test_unsure_model_or_unknown_end_keeps_the_fixed_reduction(self):
        for plan in ({"model": {**GOOD, "status": "rough"}, "expensive_until": NOW + 3600}, {"model": GOOD},
                     None, {"model": GOOD, "expensive_until": NOW - 1}):
            result = evaluate(cfg(adaptive_reduction_mode="on"), house(living=20.75, bedroom=20.75), NOW, plan=plan)
            self.assertIsNotNone(result["adaptive_reduction"]["note"])
            self.assertEqual(result["contributions"]["price"], -1)

    def test_not_asked_when_the_price_rule_does_not_reduce(self):
        result = evaluate(cfg(adaptive_reduction_mode="on"), house(price=1), NOW, plan=self.plan())
        self.assertIsNone(result["adaptive_reduction"])

    def test_expensive_until_follows_the_price_series(self):
        prices = [[NOW - 3600 + k * 900, p] for k, p in enumerate([1] * 4 + [3] * 8 + [1] * 4)]
        self.assertEqual(expensive_until(cfg(), prices, NOW + 60), NOW + 2 * 3600)
        self.assertIsNone(expensive_until(cfg(), prices, NOW - 1800))
        self.assertIsNone(expensive_until(cfg(), [], NOW))
        # Relative mode ranks within the day.
        day = datetime(2026, 11, 17).timestamp()
        series = [[day + h * 3600, float(h)] for h in range(24)]
        relative = cfg(price_mode="relative")
        self.assertEqual(expensive_until(relative, series, day + 20 * 3600 + 60), day + 24 * 3600)
        self.assertIsNone(expensive_until(relative, series, day + 3 * 3600))


class SolarTests(unittest.TestCase):
    def samples(self, rise_per_hour):
        return [(NOW - 3600 + m * 60, 21 + rise_per_hour * (m - 60) / 60) for m in range(1, 60)]

    def test_sun_heating_lowers_the_target_when_on(self):
        settings = cfg(solar_mode="on", solar_power_entity="sensor.pv_power")
        result = evaluate(settings, house(solar=2000), NOW, indoor_samples=self.samples(0.4))
        self.assertTrue(result["solar"]["heating"])
        self.assertAlmostEqual(result["target_shift"], -SUN_HEATING_DROP)
        # No rise, or too little sun: nothing happens.
        self.assertFalse(evaluate(settings, house(solar=2000), NOW, indoor_samples=self.samples(0))["solar"]["heating"])
        self.assertFalse(evaluate(settings, house(solar=100), NOW, indoor_samples=self.samples(0.4))["solar"]["heating"])

    def test_sunny_forecast_trims_preheating_and_cheap_boost(self):
        settings = cfg(solar_mode="on", solar_power_entity="sensor.pv_power", preheat_enabled=True, preheat_degrees=1,
                       cheap_boost=1)
        result = evaluate(settings, house(solar=0, price=0.1), NOW, target_shift=1, plan={"sunny_hours": 5})
        self.assertEqual(result["target_shift"], 0.5)
        self.assertEqual(result["contributions"]["price"], 0.5)
        cloudy = evaluate(settings, house(solar=0, price=0.1), NOW, target_shift=1, plan={"sunny_hours": 1})
        self.assertEqual((cloudy["target_shift"], cloudy["contributions"]["price"]), (1, 1))

    def test_shadow_and_missing_sensor_change_nothing(self):
        shadow = cfg(solar_mode="shadow", solar_power_entity="sensor.pv_power", preheat_enabled=True, preheat_degrees=1)
        result = evaluate(shadow, house(solar=2000), NOW, target_shift=1, indoor_samples=self.samples(0.4),
                          plan={"sunny_hours": 6})
        self.assertTrue(result["solar"]["heating"] and result["solar"]["sunny_ahead"])
        self.assertEqual(result["target_shift"], 1)
        missing = evaluate(cfg(solar_mode="on", solar_power_entity="sensor.pv_power"), house(), NOW)
        self.assertIn("Solstyrningen gör inget", missing["solar"]["note"])

    def test_kilowatt_sensor_and_rate_needs_half_an_hour(self):
        rows = house()
        rows.append(entity("sensor.pv_power", 1.2, NOW, unit_of_measurement="kW"))
        result = evaluate(cfg(solar_mode="on", solar_power_entity="sensor.pv_power"), rows, NOW)
        self.assertEqual(result["solar"]["power"], 1200)
        self.assertIsNone(indoor_rate([(NOW - 600, 20), (NOW, 21)], NOW))
        self.assertAlmostEqual(indoor_rate([(NOW - 1800, 20), (NOW, 20.5)], NOW), 1.0)


class ControllerRuleTests(unittest.TestCase):
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

    def test_base_correction_fades_with_short_memory(self):
        self.controller.save(config(auto_base_enabled=True, auto_base_memory_days=1))
        self.controller.base_correction["value"] = -1.0
        result = {"error": 0.0, "control_limit": None}
        self.controller.base_step(result, NOW, True)
        for k in range(1, 145):
            report = self.controller.base_step(result, NOW + k * 600, True)
        self.assertAlmostEqual(report["value"], -math.exp(-1), places=2)
        self.assertEqual(report["memory_days"], 1)

    def test_base_correction_keeps_its_value_without_memory_setting(self):
        self.controller.save(config(auto_base_enabled=True))
        self.controller.base_correction["value"] = -1.0
        result = {"error": 0.0, "control_limit": None}
        self.controller.base_step(result, NOW, True)
        self.assertEqual(self.controller.base_step(result, NOW + 86400, True)["value"], -1.0)

    def test_base_correction_stands_still_while_the_sun_heats(self):
        self.controller.save(config(auto_base_enabled=True, solar_mode="on", solar_power_entity="sensor.pv_power"))
        warm = {"error": -1.0, "control_limit": None, "solar": {"applied": True, "power": 3000}}
        self.controller.base_step(warm, NOW, True)
        self.assertEqual(self.controller.base_step(warm, NOW + 600, True)["value"], 0)
        dark = {**warm, "solar": {"applied": True, "power": 0}}
        self.assertLess(self.controller.base_step(dark, NOW + 1200, True)["value"], 0)

    def hot_water(self, mode, level="expensive", now=None, habits=None, current="Economy"):
        self.controller.save(config(enabled=True, price_entity="sensor.price", hot_water_price_enabled=True,
                                    hot_water_mode_entity=self.MODE, hot_water_habit_mode=mode))
        now = now or datetime(2026, 11, 17, 6, 10).timestamp()
        state = entity(self.MODE, current, now, options=["Economy", "Normal"])
        return self.controller.hot_water_step([state], now, {"price_level": level, "price": 3.0}, {"kind": None}, False,
                                              habits)

    def test_habit_keeps_the_cheap_mode_before_a_usual_draw(self):
        tuesday_seven = {"profile": {"wd-7": 0.75}, "days": 28}
        report = self.hot_water("on", habits=tuesday_seven)
        self.assertEqual(report["desired"], "Normal")
        self.assertIn("kl 07", report["reason"])
        self.ha.set_select.assert_called_once_with(self.MODE, "Normal")

    def test_habit_shadow_only_reports(self):
        report = self.hot_water("shadow", habits={"profile": {"wd-7": 0.75}, "days": 28})
        self.assertEqual((report["desired"], report["habit"]["would"]), ("Economy", "Normal"))
        self.ha.set_select.assert_not_called()
        self.controller.shadow_step({"hot_water": report, "contributions": {"price": 0}})
        self.controller.shadow_step({"hot_water": report, "contributions": {"price": 0}})
        shadows = [e for e in self.controller.events.recent() if e["kind"] == "shadow"]
        self.assertEqual(len(shadows), 1)
        self.assertIn("Normal", shadows[0]["message"])

    def test_unlikely_or_missing_habits_change_nothing(self):
        self.assertEqual(self.hot_water("on", habits={"profile": {"wd-7": 0.25}, "days": 28})["desired"], "Economy")
        self.assertIn("bakgrunden", self.hot_water("on", habits=None)["habit"]["note"])
        self.assertIn("kan inte", self.hot_water("on", habits={"error": "nere"})["habit"]["note"])
        learning = self.hot_water("on", habits={"profile": {}, "days": 11, "note": "Lär sig: 11 av 14 dygn med varmvattenmätningar."})
        self.assertIn("11 av 14", learning["habit"]["note"])
        self.ha.set_select.assert_not_called()

    def test_adaptive_shadow_is_logged_once_per_decision(self):
        info = {"mode": "shadow", "note": None, "reduction": 0.5, "fixed": 1, "predicted_min": 20.71, "hours": 3,
                "floor": 20.7}
        result = {"adaptive_reduction": info, "contributions": {"price": -1}}
        self.controller.shadow_step(result)
        self.controller.shadow_step({**result, "adaptive_reduction": {**info, "predicted_min": 20.72}})
        self.controller.shadow_step({**result, "adaptive_reduction": {**info, "reduction": 1}})
        self.controller.shadow_step({**result, "adaptive_reduction": {**info, "reduction": 0}})
        messages = [e["message"] for e in self.controller.events.recent() if e["kind"] == "shadow"]
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("0,5 steg i stället för 1" in m for m in messages))


class HabitProfileTests(unittest.TestCase):
    def test_share_of_weekdays_per_hour_kept_beyond_home_assistant_history(self):
        start = datetime(2026, 10, 19)  # a Monday; local days, across the change to winter time
        at = lambda d, h, m=0: (start + timedelta(days=d, hours=h, minutes=m)).timestamp()
        store = {}
        # Read in two windows, like an add-on that sees only Home Assistant's last ten days each time.
        for first, last in ((0, 11), (11, 21)):
            points = [[at(d, h), 50] for d in range(first, last) for h in (0, 12)]
            draws = [{"start": at(d, 6, 30), "drop": 3.0} for d in range(first, last) if d % 7 in (0, 1, 2)]
            draws += [{"start": at(d, 9), "drop": 1.5} for d in range(first, last)]  # small: not a shower
            store = merge_habits(store, draws, points, at(last, 0))
        result = habit_profile(store)
        self.assertEqual(result["days"], 21)
        # Monday–Wednesday of 15 weekdays.
        self.assertAlmostEqual(result["profile"]["wd-6"], 9 / 15, places=3)
        self.assertNotIn("we-6", result["profile"])
        self.assertNotIn("wd-9", result["profile"])
        self.assertEqual(habit_due(result["profile"], datetime(2026, 11, 17, 4, 30).timestamp()), (6, 0.6))
        self.assertIsNone(habit_due(result["profile"], datetime(2026, 11, 17, 3, 30).timestamp()))
        self.assertIsNone(habit_due(result["profile"], datetime(2026, 11, 21, 4, 30).timestamp()))  # Saturday

    def test_old_days_are_dropped_and_too_little_history_learns_first(self):
        now = datetime(2026, 11, 17).timestamp()
        store = merge_habits({"days": ["2026-09-01"], "draws": [[datetime(2026, 9, 1, 7).timestamp(), 5]]},
                             [], [[now - 3600, 50]], now)
        self.assertEqual(store, {"days": ["2026-11-16"], "draws": []})
        result = habit_profile(store)
        self.assertEqual(result["profile"], {})
        self.assertIn("Lär sig", result["note"])


class SunnyForecastTests(unittest.TestCase):
    def test_weighted_hours_of_hourly_forecast(self):
        points = [{"timestamp": NOW + h * 3600, "condition": c}
                  for h, c in enumerate(["clear-night"] * 3 + ["sunny"] * 3 + ["partlycloudy"] * 2 + ["cloudy"] * 5)]
        self.assertEqual(sunny_hours(points, NOW), 4.0)
        self.assertIsNone(sunny_hours(points[:3], NOW))


if __name__ == "__main__":
    unittest.main()
