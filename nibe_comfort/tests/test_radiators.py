import json
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import ControlError, Controller, validate, DEFAULTS
from radiators import MIN_WRITE_SECONDS, CONFIRM_SECONDS
from test_controller import config, entity, fixtures

T = "climate.kok"
S = "sensor.kok_temp"
PAIR = [{"thermostat": T, "sensor": S}]


class RadiatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_800_000_000.0
        self.room_value = 21.0
        self.setpoint = 22.0
        self.mode = "heat"
        self.ha = Mock()
        self.ha.set_thermostat.side_effect = self.write
        self.writes = []
        self.apply_writes = True
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(enabled=True, target_temperature=21.7, radiator_mode="on", radiator_pairs=PAIR)
        self.controller.config = validate(self.cfg)

    def write(self, entity_id, value, timeout=None):
        self.writes.append((entity_id, value))
        if self.apply_writes:
            self.setpoint = value

    def states(self, at):
        return [
            *fixtures(at),
            entity(S, self.room_value, at, unit_of_measurement="°C"),
            entity(T, self.mode, at, current_temperature=19.4, temperature=self.setpoint, min_temp=5, max_temp=35,
                   temperature_unit="°C", hvac_action="heating", supported_features=401),
        ]

    def step(self, minutes, alarm_hold=False):
        at = self.now + minutes * 60
        return self.controller.radiator_step(self.states(at), at, alarm_hold)["rows"]

    def warm_close(self, start=0):
        self.room_value = 22.6
        self.step(start)
        rows = self.step(start + 15)
        self.assertEqual(self.writes[-1], (T, 16.0))
        self.step(start + 16)  # readback
        return rows

    def kinds(self):
        return [e["kind"] for e in self.controller.events.recent()]

    def test_defaults_off_and_validation(self):
        self.assertEqual(DEFAULTS["radiator_mode"], "off")
        with self.assertRaises(ControlError):
            config(radiator_mode="on")  # no pairs
        with self.assertRaises(ControlError):
            config(radiator_pairs=[{"thermostat": "sensor.x", "sensor": S}])
        with self.assertRaises(ControlError):
            config(radiator_pairs=[{"thermostat": T, "sensor": "climate.y"}])
        with self.assertRaises(ControlError):
            config(radiator_pairs=PAIR * 2)
        with self.assertRaises(ControlError):
            config(radiator_open_above=0.8, radiator_close_above=0.8)
        with self.assertRaises(ControlError):
            config(radiator_mode="on", radiator_pairs=PAIR, target_temperature=16)
        with self.assertRaises(ControlError):
            config(room_sensors=[T], thermostat_control_enabled=True, radiator_pairs=PAIR)

    def test_old_settings_file_gets_defaults(self):
        saved = {k: v for k, v in config().items() if not k.startswith("radiator_")}
        with tempfile.TemporaryDirectory() as directory:
            with open(f"{directory}/settings.json", "w") as handle:
                json.dump(saved, handle)
            controller = Controller(Mock(), directory)
            self.assertEqual(controller.config["radiator_mode"], "off")
            self.assertEqual(controller.config["radiator_pairs"], [])

    def test_off_never_writes(self):
        self.controller.config = validate({**self.cfg, "radiator_mode": "off"})
        self.room_value = 26
        for minute in range(0, 120, 5):
            self.assertEqual(self.step(minute), [])
        self.assertEqual(self.writes, [])

    def test_closes_only_after_sustained_warmth_and_restores_previous(self):
        self.room_value = 22.6
        self.assertIn("Stryper om 15 min", self.step(0)[0]["message"])
        self.room_value = 22.4  # dips below: timer resets
        self.step(10)
        self.room_value = 22.6
        self.step(11)
        self.step(25)
        self.assertEqual(self.writes, [])
        self.step(26)
        self.assertEqual(self.writes, [(T, 16.0)])
        self.step(27)
        self.assertIn("radiator_observed", self.kinds())
        self.room_value = 22.0  # between open (21.9) and close (22.5): stays throttled
        self.assertIn("Stryper", self.step(60)[0]["message"])
        self.room_value = 21.9
        self.step(61)
        self.assertEqual(self.writes[-1], (T, 22.0))
        self.step(62)
        self.assertEqual(self.controller.radiators[T]["phase"], "open")
        self.assertNotIn("restore", self.controller.radiators[T])

    def test_cooling_waits_for_min_interval(self):
        self.warm_close()
        self.room_value = 21.5
        rows = self.step(15 + MIN_WRITE_SECONDS / 60 - 2)
        self.assertIn("Öppnar om", rows[0]["message"])
        self.assertEqual(len(self.writes), 1)
        self.step(15 + MIN_WRITE_SECONDS / 60)
        self.assertEqual(self.writes[-1], (T, 22.0))

    def test_safety_reasons_open_at_once(self):
        for reason in ("sensor", "alarm", "disabled", "off", "removed"):
            with self.subTest(reason=reason):
                self.setUp()
                self.warm_close()
                if reason == "sensor":
                    self.room_value = "unavailable"
                    self.step(17)
                elif reason == "alarm":
                    self.step(17, alarm_hold=True)
                elif reason == "disabled":
                    self.controller.config = validate({**self.cfg, "enabled": False})
                    self.step(17)
                elif reason == "off":
                    self.controller.config = validate({**self.cfg, "radiator_mode": "off"})
                    self.step(17)
                    self.step(18)
                    self.assertNotIn(T, self.controller.radiators)
                else:
                    self.controller.config = validate({**self.cfg, "radiator_mode": "off", "radiator_pairs": []})
                    self.step(17)
                self.assertEqual(self.writes[-1], (T, 22.0))

    def test_manual_change_while_throttled_is_left_alone(self):
        self.warm_close()
        self.setpoint = 20.0
        self.step(30)
        self.assertIn("radiator_manual", self.kinds())
        self.room_value = 21.0
        self.step(60)
        self.assertEqual(self.writes, [(T, 16.0)])

    def test_lost_close_is_forgotten_and_lost_open_is_retried(self):
        self.apply_writes = False
        self.room_value = 22.6
        self.step(0)
        self.step(15)
        self.step(15 + CONFIRM_SECONDS / 60)
        self.assertIn("radiator_unconfirmed", self.kinds())
        self.assertEqual(self.controller.radiators[T]["phase"], "open")
        # Now the open write gets lost.
        self.setUp()
        self.warm_close()
        self.apply_writes = False
        self.room_value = 21.0
        self.step(40)
        self.assertEqual(self.writes[-1], (T, 22.0))
        self.step(39 + CONFIRM_SECONDS / 60)
        self.assertEqual(self.controller.radiators[T]["phase"], "closed")
        self.apply_writes = True
        self.step(40 + CONFIRM_SECONDS / 60)  # unconfirmed, still throttled: written again at once
        self.assertEqual(self.writes.count((T, 22.0)), 2)
        self.step(41 + CONFIRM_SECONDS / 60)
        self.assertEqual(self.setpoint, 22.0)
        self.assertEqual(self.controller.radiators[T]["phase"], "open")

    def test_failed_call_is_reported_and_readback_decides(self):
        def fail(entity_id, value, timeout=None):
            self.writes.append((entity_id, value))
            raise ControlError("timeout")
        self.ha.set_thermostat.side_effect = fail
        self.room_value = 22.6
        self.step(0)
        rows = self.step(15)
        self.assertEqual(rows[0]["error"], "timeout")
        self.assertIn("radiator_failed", self.kinds())
        self.step(15 + CONFIRM_SECONDS / 60)
        self.assertEqual(self.controller.radiators[T]["phase"], "open")

    def test_not_heat_or_already_low_is_untouched(self):
        self.mode = "off"
        self.room_value = 23
        self.step(0)
        self.assertIn("värmeläge", self.step(20)[0]["error"])
        self.mode, self.setpoint = "heat", 15.0
        self.assertIn("redan lågt", self.step(40)[0]["message"])
        self.assertEqual(self.writes, [])

    def test_shadow_logs_but_never_writes(self):
        self.controller.config = validate({**self.cfg, "radiator_mode": "shadow"})
        self.room_value = 23
        self.step(0)
        self.assertIn("hade sänkt", self.step(20)[0]["message"])
        self.room_value = 21
        self.assertIn("hade öppnat", self.step(40)[0]["message"])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.kinds().count("radiator_shadow"), 2)

    def test_switch_on_to_shadow_restores_first(self):
        self.warm_close()
        self.controller.config = validate({**self.cfg, "radiator_mode": "shadow"})
        self.step(20)
        self.assertEqual(self.writes[-1], (T, 22.0))
        self.step(21)
        self.assertTrue(self.controller.radiators[T]["virtual"])
        self.assertEqual(len(self.writes), 2)

    def test_restart_keeps_throttle_and_stop_restores(self):
        self.warm_close()
        restarted = Controller(self.ha, self.temp.name)
        self.assertEqual(restarted.radiators[T]["phase"], "closed")
        self.assertEqual(restarted.restore_radiators("tillägget stoppas"), [])
        self.assertEqual(self.writes[-1], (T, 22.0))

    def test_unavailable_thermostat_waits(self):
        self.warm_close()
        self.room_value = 21
        self.mode = "unavailable"
        rows = self.step(40)
        self.assertTrue(rows[0]["error"])
        self.assertEqual(len(self.writes), 1)
        self.mode = "heat"
        self.step(41)
        self.assertEqual(self.writes[-1], (T, 22.0))

    def test_step_and_min_temp_respected(self):
        self.controller.config = validate({**self.cfg, "radiator_closed_setpoint": 15.2})
        self.room_value = 23
        at = self.now
        states = self.states(at)
        states[-1]["attributes"].update(target_temp_step=0.5, min_temp=15.5)
        self.controller.radiator_step(states, at)
        self.controller.radiator_step(states, at + 900)
        self.assertEqual(self.writes, [(T, 15.5)])

    def test_tick_runs_without_pump_values(self):
        self.ha.states.return_value = [entity(S, 23, time.time(), unit_of_measurement="°C"),
                                       entity(T, "heat", time.time(), temperature=22, temperature_unit="°C")]
        self.ha.integration_registry.return_value = []
        self.controller.tick_entities = None
        with self.assertRaises(ControlError):  # the pump's values are missing
            self.controller.tick()
        self.assertEqual(self.controller.snapshot()["radiators"]["mode"], "on")
        self.assertIsNotNone(self.controller.radiators.get(T, {}).get("warm_since"))


if __name__ == "__main__":
    unittest.main()
