import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from controller import ControlError, Controller, HomeAssistant, atomic_json
from test_controller import config, entity, fixtures


class ThermostatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()
        self.room = entity("climate.living", "heat", self.now, current_temperature=22.5,
                           temperature=25, min_temp=5, max_temp=30, target_temp_step=0.5,
                           supported_features=1, temperature_unit="°C")
        self.ha = Mock()
        self.ha.states.side_effect = lambda: [*fixtures(), self.room]
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(enabled=True, target_temperature=22, room_sensors=["climate.living"],
                          thermostat_control_enabled=True, nearest_thermostat="climate.living")
        self.controller.save(self.cfg)

    def step(self, offset=0, testing=False):
        return self.controller.thermostat_step(self.ha.states(), self.now + offset, {}, testing)["rooms"][0]

    def test_exact_threshold_one_write_and_readback(self):
        self.room["attributes"]["current_temperature"] = 22.49
        self.step()
        self.ha.set_thermostat.assert_not_called()
        self.room["attributes"]["current_temperature"] = 22.5
        self.step()
        self.ha.set_thermostat.assert_called_once_with("climate.living", 22)
        self.assertIn("återläsning", self.step(60)["message"])
        self.room["attributes"]["temperature"] = 22
        self.step(120)
        kinds = [e["kind"] for e in self.controller.events.recent()]
        for kind in ("thermostat_attempt", "thermostat_sent", "thermostat_observed"):
            self.assertIn(kind, kinds)
        self.ha.set_thermostat.assert_called_once()
        self.room["attributes"]["temperature"] = 25
        self.step(3600)
        self.ha.set_thermostat.assert_called_once()

    def test_cooldown_and_rearm_only_after_cooling(self):
        self.step()
        self.room["attributes"]["temperature"] = 22
        self.step(60)
        self.room["attributes"]["current_temperature"] = 22.2
        self.step(1801)
        self.room["attributes"].update(current_temperature=22.6, temperature=25)
        self.step(1860)
        self.assertEqual(self.ha.set_thermostat.call_count, 2)

    def test_disabled_master_and_test_do_not_write(self):
        self.controller.config["enabled"] = False
        self.step()
        self.controller.config["enabled"] = True
        self.step(testing=True)
        self.controller.config["thermostat_control_enabled"] = False
        self.assertEqual(self.controller.thermostat_step(self.ha.states(), self.now, {}, False)["rooms"], [])
        self.ha.set_thermostat.assert_not_called()

    def test_never_raises_or_switches_mode(self):
        for value in (20, 22):
            self.room["attributes"]["temperature"] = value
            self.step()
        self.room["attributes"]["temperature"] = 25
        for mode in ("off", "auto", "cool", "heat_cool", "unavailable"):
            self.room["state"] = mode
            self.assertIsNotNone(self.step()["error"])
        self.ha.set_thermostat.assert_not_called()

    def test_invalid_stale_and_unrepresentable_values_fail_closed(self):
        for attrs in ({"supported_features": 0}, {"target_temp_step": 0},
                      {"current_temperature": None}, {"temperature": None},
                      {"min_temp": 24}, {"target_temp_step": 2}, {"temperature_unit": "K"}):
            with self.subTest(attrs=attrs):
                previous = self.room["attributes"].copy()
                self.room["attributes"].update(attrs)
                self.assertIsNotNone(self.step()["error"])
                self.room["attributes"] = previous
        self.room["last_updated"] = entity("climate.living", "heat", self.now - 86400)["last_updated"]
        self.assertIsNotNone(self.step()["error"])
        self.ha.set_thermostat.assert_not_called()

    def test_fahrenheit_conversion_for_service(self):
        self.room["attributes"].update(temperature_unit="°F", current_temperature=73,
                                       temperature=80, min_temp=41, max_temp=95, target_temp_step=0.1)
        self.step()
        self.ha.set_thermostat.assert_called_once_with("climate.living", 71.6)

    def test_optional_step_sends_exact_goal_and_confirms_readback(self):
        del self.room["attributes"]["target_temp_step"]
        self.room["attributes"]["supported_features"] = 401
        self.controller.save(self.cfg)
        row = self.step()
        self.assertIn("utan avrundning", row["warning"])
        self.ha.set_thermostat.assert_called_once_with("climate.living", 22)
        self.room["attributes"]["temperature"] = 22
        self.step(60)
        self.assertEqual(self.controller.thermostats["climate.living"]["outcome"], "confirmed")

    def test_missing_step_does_not_allow_invalid_range_or_assume_confirmation(self):
        self.room["attributes"]["target_temp_step"] = None
        self.room["attributes"]["max_temp"] = 21
        self.assertIsNotNone(self.step()["error"])
        self.ha.set_thermostat.assert_not_called()
        self.room["attributes"]["max_temp"] = 30
        self.step()
        self.room["attributes"]["temperature"] = 22.5
        self.assertIsNotNone(self.step(900)["error"])
        self.assertEqual(self.controller.thermostats["climate.living"]["outcome"], "unconfirmed")
        self.ha.set_thermostat.assert_called_once()

    def test_failed_ambiguous_write_is_not_repeated_and_survives_restart(self):
        self.ha.set_thermostat.side_effect = ControlError("Timeout")
        self.assertIn("Timeout", self.step()["error"])
        restored = Controller(self.ha, self.temp.name)
        restored.thermostat_step(self.ha.states(), self.now + 901, {}, False)
        self.ha.set_thermostat.assert_called_once()
        self.assertFalse(restored.thermostats["climate.living"]["pending"])
        self.assertTrue(restored.thermostats["climate.living"]["latched"])

    def test_accepted_but_unconfirmed_write_does_not_repeat(self):
        self.step()
        self.assertIsNotNone(self.step(900)["error"])
        self.assertIn("thermostat_unconfirmed", [e["kind"] for e in self.controller.events.recent()])
        self.step(3600)
        self.ha.set_thermostat.assert_called_once()

    def test_persistence_before_service_and_disk_failure(self):
        def check(key, target):
            saved = json.loads(self.controller.thermostat_path.read_text())
            self.assertTrue(saved[key]["pending"])
            self.assertEqual(saved[key]["value"], target)
        self.ha.set_thermostat.side_effect = check
        self.step()
        self.ha.set_thermostat.reset_mock()
        self.controller.thermostats = {}
        with patch("controller.atomic_json", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.step()
        self.ha.set_thermostat.assert_not_called()

    def test_preflight_rejects_and_does_not_save_invalid_settings(self):
        self.room["state"] = "off"
        with self.assertRaises(ControlError):
            self.controller.save({**self.cfg, "target_temperature": 23})
        self.assertEqual(self.controller.settings()["target_temperature"], 22)

    def test_master_stop_always_works_without_ha(self):
        self.ha.states.side_effect = ControlError("Ingen kontakt")
        self.controller.save({**self.cfg, "enabled": False})
        self.assertFalse(self.controller.settings()["enabled"])
        self.assertTrue(self.controller.settings()["thermostat_control_enabled"])
        self.ha.set_thermostat.assert_not_called()

    def test_one_room_failure_is_visible_without_hiding_other_rooms(self):
        other = entity("climate.other", "heat", self.now, **self.room["attributes"])
        self.controller.config["room_sensors"].append("climate.other")
        self.ha.states.side_effect = lambda: [*fixtures(), self.room, other]
        self.room["state"] = "off"
        rows = self.controller.thermostat_step(self.ha.states(), self.now, {}, False)["rooms"]
        self.assertIsNotNone(rows[0]["error"])
        self.assertIn("skickad", rows[1]["message"])
        self.ha.set_thermostat.assert_called_once_with("climate.other", 22)

    def test_non_priority_and_nearest_thermostats_are_both_controlled(self):
        other = entity("climate.other", "heat", self.now, **self.room["attributes"])
        self.controller.config.update(room_sensors=["climate.living", "climate.other"],
                                      priority_sensors=["climate.living"])
        rows = self.controller.thermostat_step([self.room, other], self.now, {}, False)["rooms"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.ha.set_thermostat.call_count, 2)

    def test_startup_does_not_bypass_thirty_minute_interval(self):
        self.step()
        restored = Controller(self.ha, self.temp.name)
        self.room["attributes"].update(temperature=22, current_temperature=22)
        restored.thermostat_step(self.ha.states(), self.now + 120, {}, False)
        self.room["attributes"].update(temperature=25, current_temperature=22.6)
        restored.thermostat_step(self.ha.states(), self.now + 180, {}, False)
        self.ha.set_thermostat.assert_called_once()

    def test_restart_without_prior_attempt_can_write_after_valid_reading(self):
        restored = Controller(self.ha, self.temp.name)
        restored.thermostat_step(self.ha.states(), self.now, {}, False)
        self.ha.set_thermostat.assert_called_once_with("climate.living", 22)

    def test_restart_does_not_reset_remaining_interval_after_rearming(self):
        self.controller.thermostats["climate.living"] = {
            "attempted_at": self.now - 1700, "latched": False, "pending": False,
        }
        atomic_json(self.controller.thermostat_path, self.controller.thermostats)
        restored = Controller(self.ha, self.temp.name)
        row = restored.thermostat_step(self.ha.states(), self.now, {}, False)["rooms"][0]
        self.assertIn("Väntar 2 min", row["message"])
        self.ha.set_thermostat.assert_not_called()
        restored = Controller(self.ha, self.temp.name)
        restored.thermostat_step(self.ha.states(), self.now + 99, {}, False)
        self.ha.set_thermostat.assert_not_called()
        restored.thermostat_step(self.ha.states(), self.now + 100, {}, False)
        self.ha.set_thermostat.assert_called_once_with("climate.living", 22)

    def test_confirmed_readback_after_restart_keeps_warm_period_latch(self):
        self.step()
        restored = Controller(self.ha, self.temp.name)
        self.room["attributes"]["temperature"] = 22
        restored.thermostat_step(self.ha.states(), self.now + 60, {}, False)
        self.assertEqual(restored.thermostats["climate.living"]["outcome"], "confirmed")
        self.assertEqual(restored.thermostats["climate.living"]["attempted_at"], self.now)
        self.room["attributes"]["temperature"] = 25
        row = restored.thermostat_step(self.ha.states(), self.now + 3600, {}, False)["rooms"][0]
        self.assertIn("Redan begränsad", row["message"])
        self.ha.set_thermostat.assert_called_once()

    def test_invalid_saved_attempt_stops_startup(self):
        for timestamp in (-1, "NaN", "Infinity"):
            with self.subTest(timestamp=timestamp):
                atomic_json(self.controller.thermostat_path, {"climate.living": {"attempted_at": timestamp}})
                with self.assertRaises(ControlError):
                    Controller(self.ha, self.temp.name)
        self.ha.set_thermostat.assert_not_called()

    def test_migration_and_validation(self):
        for overrides in ({"nearest_thermostat": "climate.other"},
                          {"room_sensors": ["sensor.living"], "thermostat_control_enabled": True},
                          {"thermostat_control_enabled": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(ControlError):
                config(**overrides)
        old = config()
        del old["nearest_thermostat"]
        del old["thermostat_control_enabled"]
        self.controller.path.write_text(json.dumps(old))
        restored = Controller(self.ha, self.temp.name)
        self.assertFalse(restored.settings()["thermostat_control_enabled"])
        self.assertEqual(restored.settings()["nearest_thermostat"], "")

    def test_validation_identifies_separate_sensor_in_mixed_selection(self):
        with self.assertRaisesRegex(ControlError, "sensor.living"):
            config(room_sensors=["climate.living", "sensor.living"],
                   thermostat_control_enabled=True)
        with self.assertRaisesRegex(ControlError, "minst en"):
            config(room_sensors=[], thermostat_control_enabled=True)
        valid = config(room_sensors=["climate.living", "climate.other"],
                       thermostat_control_enabled=True)
        self.assertTrue(valid["thermostat_control_enabled"])

    def test_tick_integrates_information_and_control(self):
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertTrue(status["rooms"][0]["nearest"])
        self.assertTrue(status["thermostat_control"]["enabled"])
        self.ha.set_thermostat.assert_called_once_with("climate.living", 22)
        self.ha.set_offset.assert_called_once_with("number.offset", -1)

    def test_service_payload(self):
        ha = HomeAssistant("test-token")
        ha.request = Mock()
        ha.set_thermostat("climate.living", 22)
        ha.request.assert_called_once_with("/services/climate/set_temperature",
                                           {"entity_id": "climate.living", "temperature": 22})


if __name__ == "__main__":
    unittest.main()
