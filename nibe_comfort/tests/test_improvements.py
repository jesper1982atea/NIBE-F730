import copy
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import (AUTO_BASE_MAX_GAP, ControlError, Controller, DEFAULTS, PAUSE_NOTIFICATION_ID, control_state,
                        evaluate, indoor_reading, price_contribution, price_rank, pump_alarm, sensor_states, series_rank,
                        validate)
from insights import planned_offsets, relative_limits
from test_controller import entity, fixtures


def config(**overrides):
    return validate({**copy.deepcopy(DEFAULTS), "room_sensors": ["sensor.living", "sensor.bedroom"],
                     "offset_entity": "number.offset", **overrides})


def stale(rows, entity_id, hours=10):
    row = next(e for e in rows if e["entity_id"] == entity_id)
    row["last_updated"] = datetime.fromtimestamp(time.time() - hours * 3600).astimezone().isoformat()
    return rows


class SilentRoomTests(unittest.TestCase):
    def test_default_still_pauses_on_a_silent_room(self):
        with self.assertRaises(ControlError):
            evaluate(config(), stale(fixtures(), "sensor.bedroom"), time.time())

    def test_silent_room_is_skipped_when_enabled(self):
        result = evaluate(config(skip_silent_rooms=True), stale(fixtures(), "sensor.bedroom"), time.time())
        self.assertEqual(result["indoor"], 20)
        self.assertEqual(result["indoor_rooms"], ["sensor.living"])
        self.assertEqual([room["entity_id"] for room in result["skipped_rooms"]], ["sensor.bedroom"])
        self.assertFalse(result["indoor_fallback"])

    def test_implausible_value_is_skipped_too(self):
        rows = fixtures()
        next(e for e in rows if e["entity_id"] == "sensor.bedroom")["state"] = "2"
        result = evaluate(config(skip_silent_rooms=True), rows, time.time())
        self.assertEqual(result["indoor"], 20)

    def test_all_silent_rooms_still_pause(self):
        rows = stale(stale(fixtures(), "sensor.bedroom"), "sensor.living")
        with self.assertRaisesRegex(ControlError, "Ingen rumsgivare"):
            evaluate(config(skip_silent_rooms=True), rows, time.time())

    def test_silent_priority_rooms_fall_back_to_the_other_rooms(self):
        cfg = config(skip_silent_rooms=True, priority_sensors=["sensor.living"])
        result = evaluate(cfg, stale(fixtures(), "sensor.living"), time.time())
        self.assertEqual(result["indoor"], 22)
        self.assertTrue(result["indoor_fallback"])

    def test_fresh_priority_room_does_not_use_the_others(self):
        cfg = config(skip_silent_rooms=True, priority_sensors=["sensor.living"])
        reading = indoor_reading(cfg, lambda key: next(e for e in fixtures() if e["entity_id"] == key))
        self.assertEqual((reading["value"], reading["fallback"]), (20, False))


class PriceTests(unittest.TestCase):
    def test_absolute_mode_is_unchanged(self):
        cfg = config(price_entity="sensor.price", cheap_boost=1, expensive_reduction=1, price_low=1, price_high=2)
        self.assertEqual(price_contribution(cfg, 3, 0), -1)
        self.assertEqual(price_contribution(cfg, 0.5, 0), 1)

    def test_relative_mode_uses_rank(self):
        cfg = config(price_entity="sensor.price", cheap_boost=1, expensive_reduction=1, price_mode="relative")
        self.assertEqual(price_contribution(cfg, 9, 0, rank=0.1), 1)
        self.assertEqual(price_contribution(cfg, 0.1, 0, rank=0.9), -1)
        self.assertEqual(price_contribution(cfg, 3, 0, rank=0.5), 0)
        self.assertEqual(price_contribution(cfg, 3, 0, rank=None), 0)

    def test_relative_rule_never_worsens_a_cold_house(self):
        cfg = config(price_entity="sensor.price", cheap_boost=1, expensive_reduction=1, price_mode="relative")
        self.assertEqual(price_contribution(cfg, 5, 1.0, rank=0.95), 0)

    def test_tibber_ranking_attribute(self):
        self.assertEqual(price_rank(entity("sensor.price", 4, intraday_price_ranking=0.4), time.time()), 0.4)

    def test_nordpool_raw_today(self):
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        raw = [{"start": (start + timedelta(hours=h)).isoformat(), "value": float(h)} for h in range(24)]
        rank = price_rank(entity("sensor.price", 12.0, raw_today=raw), time.time())
        self.assertAlmostEqual(rank, 12 / 23)

    def test_missing_series_gives_no_rank(self):
        self.assertIsNone(price_rank(entity("sensor.price", 4), time.time()))

    def test_series_rank_only_counts_the_same_day(self):
        now = time.time()
        points = [(now, 1), (now + 60, 3), (now - 3 * 86400, 100)]
        self.assertEqual(series_rank(points, now, 3), 1)

    def test_price_is_shown_without_price_rule(self):
        result = evaluate(config(price_entity="sensor.price"), fixtures(), time.time())
        self.assertEqual(result["price"], 3)
        self.assertEqual(result["contributions"]["price"], 0)

    def test_broken_price_does_not_pause_without_price_rule(self):
        rows = fixtures()
        next(e for e in rows if e["entity_id"] == "sensor.price")["state"] = "unavailable"
        result = evaluate(config(price_entity="sensor.price"), rows, time.time())
        self.assertIsNone(result["price"])
        self.assertTrue(result["price_error"])

    def test_relative_limits_for_the_chart(self):
        now = datetime.now().replace(hour=12).timestamp()
        points = [(now + i, float(i)) for i in range(5)]
        limits = relative_limits(points, now, config(price_mode="relative"))
        self.assertEqual((limits["low"], limits["high"]), (1.0, 3.0))

    def test_validation(self):
        with self.assertRaises(ControlError):
            config(price_mode="cheap")
        with self.assertRaises(ControlError):
            config(price_low_percent=80, price_high_percent=20)


class BaseCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.states.side_effect = lambda: fixtures()
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)
        self.controller.config = config(enabled=True, auto_base_enabled=True, target_temperature=22)

    def result(self, error, limit=None):
        return {"error": error, "control_limit": limit}

    def test_one_degree_for_a_day_gives_one_step(self):
        now = 1_000_000.0
        self.controller.base_step(self.result(1.0), now, True)
        for i in range(1, 24 * 6 + 1):
            state = self.controller.base_step(self.result(1.0), now + i * AUTO_BASE_MAX_GAP, True)
        self.assertAlmostEqual(state["value"], 1.0, places=3)

    def test_small_deviation_and_ineligible_rounds_stand_still(self):
        self.controller.base_step(self.result(0.05), 0, True)
        self.assertEqual(self.controller.base_step(self.result(0.05), 600, True)["value"], 0)
        self.assertEqual(self.controller.base_step(self.result(2.0), 1200, False)["value"], 0)

    def test_limit_and_windup(self):
        self.controller.base_correction["value"] = 1.99
        self.controller.base_step(self.result(5), 0, True)
        self.assertEqual(self.controller.base_step(self.result(5), 600, True)["value"], 2.0)
        self.controller.base_correction["value"] = 0
        upper = {"direction": "upper", "value": 3, "requested": 4}
        self.controller.base_step(self.result(1, upper), 1200, True)
        self.assertEqual(self.controller.base_step(self.result(1, upper), 1800, True)["value"], 0)

    def test_gap_after_downtime_is_capped(self):
        self.controller.base_step(self.result(1.0), 0, True)
        value = self.controller.base_step(self.result(1.0), 86400, True)["value"]
        self.assertAlmostEqual(value, AUTO_BASE_MAX_GAP / 86400, places=2)

    def test_correction_steers_and_survives_restart(self):
        self.controller.base_correction = {"value": 1.0, "updated_at": 0}
        self.controller.base_saved_at = 0
        self.controller.base_step(self.result(0.5), 10_000, True)
        self.controller.base_step(self.result(0.5), 10_600, True)
        saved = json.loads((Path(self.temp.name) / "base-correction.json").read_text())
        self.assertGreater(saved["value"], 1.0)
        result = evaluate(self.controller.config, fixtures(), time.time(), base_correction=1.0)
        plain = evaluate(self.controller.config, fixtures(), time.time())
        self.assertAlmostEqual(result["desired_offset"] - plain["desired_offset"], 1.0)
        self.assertEqual(result["contributions"]["base_correction"], 1.0)

    def test_disabled_setting_ignores_saved_value(self):
        cfg = config(enabled=True, target_temperature=22)
        self.assertEqual(evaluate(cfg, fixtures(), time.time(), base_correction=2.0)["contributions"]["base_correction"], 0)

    def test_manual_base_change_resets(self):
        self.controller.save(config(auto_base_enabled=True, target_temperature=22))
        self.controller.base_correction = {"value": 1.2, "updated_at": 0}
        self.controller.save(config(auto_base_enabled=True, target_temperature=22, base_offset=1))
        self.assertEqual(self.controller.base_correction["value"], 0)
        self.assertTrue(any(e["kind"] == "base_correction_reset" for e in self.controller.events.recent()))

    def test_tick_reports_correction(self):
        self.controller.tick()
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertTrue(status["base_correction"]["enabled"])
        self.assertIn("base_correction", status["contributions"])

    def test_plan_includes_correction(self):
        status = evaluate(self.controller.config, fixtures(), time.time(), base_correction=1.0)
        status.update(error_message=None)
        plan = planned_offsets(self.controller.config, status, [], [], time.time(), time.time() + 3600, False)
        self.assertAlmostEqual(plan["desired"][0][1], status["desired_offset"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.states.side_effect = lambda: fixtures()
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)
        self.controller.config = config(enabled=True, target_temperature=22, pause_notify_minutes=30,
                                        notify_service="notify.mobile_app_phone")

    def calls(self, prefix):
        return [c for c in self.ha.request.call_args_list if c.args[0].startswith(prefix)]

    def test_long_pause_notifies_once_and_dismisses(self):
        self.controller.status = {"error_message": "Givare tyst.", "message": "Pausad"}
        self.controller.report(0)
        self.controller.report(29 * 60)
        self.assertFalse(self.calls("/services/persistent_notification/create"))
        self.controller.report(31 * 60)
        self.controller.report(40 * 60)
        create = self.calls("/services/persistent_notification/create")
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0].args[1]["notification_id"], PAUSE_NOTIFICATION_ID)
        self.assertEqual(len(self.calls("/services/notify/mobile_app_phone")), 1)
        self.controller.status = {"error_message": None, "message": "Ingen ändring behövs."}
        self.controller.report(41 * 60)
        self.assertEqual(len(self.calls("/services/persistent_notification/dismiss")), 1)
        self.assertFalse(self.controller.pause_notified)

    def test_zero_minutes_and_monitoring_never_notify(self):
        self.controller.config = config(target_temperature=22, pause_notify_minutes=30)
        self.controller.status = {"error_message": "Givare tyst."}
        self.controller.report(0)
        self.controller.report(3600)
        self.controller.config = config(enabled=True, target_temperature=22, pause_notify_minutes=0)
        self.controller.report(7200)
        self.assertFalse(self.ha.request.called)

    def test_failed_notification_is_retried_and_never_raises(self):
        self.ha.request.side_effect = ControlError("HA nere")
        self.controller.status = {"error_message": "Givare tyst."}
        self.controller.report(0)
        self.controller.report(3600)
        self.assertFalse(self.controller.pause_notified)
        self.ha.request.side_effect = None
        self.controller.report(3660)
        self.assertTrue(self.controller.pause_notified)

    def test_publishes_sensors_after_tick(self):
        self.controller.config = config(enabled=True, target_temperature=22, publish_sensors=True)
        self.controller.tick()
        published = {c.args[0]: c.args[1] for c in self.calls("/states/")}
        self.assertEqual(published["/states/sensor.nibe_comfort_status"]["state"], "Styr")
        self.assertEqual(published["/states/sensor.nibe_comfort_innetemp"]["state"], 21)
        self.assertEqual(published["/states/sensor.nibe_comfort_mal"]["state"], 22)
        self.assertNotIn("/states/sensor.nibe_comfort_baskorrigering", published)

    def test_publishing_failure_does_not_break_control(self):
        self.controller.config = config(enabled=True, target_temperature=22, publish_sensors=True)
        self.ha.request.side_effect = ControlError("HA nere")
        self.controller.tick()
        self.assertIsNone(self.controller.snapshot()["error_message"])

    def test_paused_sensors(self):
        cfg = config(enabled=True, auto_base_enabled=True)
        sensors = {key: (state, attrs) for key, state, attrs in sensor_states(
            {"error_message": "Tyst givare.", "indoor_smoothed": 21, "target_shift": 0}, cfg)}
        self.assertEqual(sensors["sensor.nibe_comfort_status"][0], "Pausad")
        self.assertEqual(sensors["sensor.nibe_comfort_innetemp"][0], "unknown")
        self.assertEqual(sensors["sensor.nibe_comfort_mal"][0], "unknown")
        self.assertIn("sensor.nibe_comfort_baskorrigering", sensors)

    def test_control_states(self):
        cfg = config(enabled=True)
        self.assertEqual(control_state({}, config()), "Övervakning")
        self.assertEqual(control_state({"preheat": {"kind": "preheat"}}, cfg), "Förvärmer")
        self.assertEqual(control_state({"preheat": {"kind": "coast"}}, cfg), "Sparar")
        self.assertEqual(control_state({"fireplace": {"phase": "active"}}, cfg), "Kamin")
        self.assertEqual(control_state({"vacation": {"phase": "active"}}, cfg), "Semester")
        self.assertEqual(control_state({}, cfg), "Styr")

    def test_notify_service_validation(self):
        with self.assertRaises(ControlError):
            config(notify_service="mobile_app_phone")


class WarningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = []
        rows = fixtures()
        rows.append(entity("climate.radiator", "heat", None, current_temperature=24, temperature=22,
                           supported_features=1, min_temp=5, max_temp=30, temperature_unit="°C"))
        self.ha.states.side_effect = lambda: copy.deepcopy(rows)
        self.controller = Controller(self.ha, self.temp.name)

    def test_thermostat_room_sensor_is_flagged(self):
        self.controller.config = config(room_sensors=["sensor.living", "sensor.bedroom", "climate.radiator"],
                                        priority_sensors=["sensor.living"])
        self.controller.tick()
        warnings = self.controller.snapshot()["warnings"]
        self.assertTrue(any("termostat" in w and "husets snitt" in w for w in warnings))

    def test_skipped_room_is_explained(self):
        self.ha.states.side_effect = lambda: stale(fixtures(), "sensor.bedroom")
        self.controller.config = config(skip_silent_rooms=True)
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertIsNone(status["error_message"])
        self.assertTrue(any(w.startswith("sensor.bedroom hoppas över") for w in status["warnings"]))


class AlarmDateTests(unittest.TestCase):
    def test_code_only_alarm_reports_since(self):
        now = time.time()
        code = entity("sensor.pump_alarms_45001", 184, now)
        code["last_changed"] = datetime(2026, 10, 28, 14, 36).astimezone().isoformat()
        offset = entity("number.offset", 0, now, min=-10, max=10, step=1)
        peers = {"number.offset": ("number.offset", "sensor.pump_alarms_45001")}
        alarm = pump_alarm([code, offset], config(), peers, now)
        self.assertEqual(alarm["latest_code"], 184)
        self.assertIsNotNone(alarm["since"])
        self.assertIn("oförändrad åtminstone sedan 28 okt", alarm["error"])


class MigrationTests(unittest.TestCase):
    def test_settings_saved_by_older_version_load(self):
        with tempfile.TemporaryDirectory() as path:
            old = {key: value for key, value in config().items()
                   if key not in ("skip_silent_rooms", "price_mode", "price_low_percent", "price_high_percent",
                                  "auto_base_enabled", "auto_base_limit", "publish_sensors",
                                  "pause_notify_minutes", "notify_service")}
            (Path(path) / "settings.json").write_text(json.dumps(old))
            controller = Controller(Mock(), path)
            self.assertFalse(controller.config["skip_silent_rooms"])
            self.assertEqual(controller.config["price_mode"], "absolute")

    def test_broken_correction_file_starts_from_zero(self):
        with tempfile.TemporaryDirectory() as path:
            (Path(path) / "base-correction.json").write_text("{trasig")
            self.assertEqual(Controller(Mock(), path).base_correction["value"], 0.0)


if __name__ == "__main__":
    unittest.main()
