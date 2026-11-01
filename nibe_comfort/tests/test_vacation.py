import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError, Controller, VACATION_RETRY_SECONDS, validate_vacation
from test_controller import config, entity, fixtures

MODE = "select.pump_smart_home_mode"
OPTIONS = ["default", "normal", "away", "vacation", "home"]
REGISTRY = [
    {"entity_id": "number.offset", "device_id": "pump", "platform": "myuplink"},
    {"entity_id": MODE, "device_id": "pump", "platform": "myuplink"},
]


class VacationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mode = "normal"
        self.available = True
        self.ha = Mock()
        self.ha.states.side_effect = self.states
        self.ha.integration_registry.return_value = REGISTRY
        self.ha.set_select.side_effect = lambda _entity, option: setattr(self, "mode", option)
        self.controller = self.make()
        self.controller.config = config(enabled=True)
        self.now = time.time()

    def make(self):
        return Controller(self.ha, self.temp.name)

    def states(self):
        state = self.mode if self.available else "unavailable"
        attributes = {"options": OPTIONS} if self.available else {}
        return fixtures() + [entity(MODE, state, **attributes)]

    def step(self, at):
        self.controller.vacation_step(self.states(), at)

    def kinds(self):
        return [event["kind"] for event in self.controller.events.recent()]

    def schedule(self, start=None, hours=1):
        self.controller.schedule_vacation(start, self.now + hours * 3600)

    def run_active(self):
        self.schedule()
        self.step(self.now + 60)
        self.step(self.now + 120)
        self.assertTrue(self.controller.vacation_paused())

    def test_future_period_waits_then_sets_vacation_and_restores_previous_mode(self):
        self.mode = "home"
        self.schedule(self.now + 3600, hours=2)
        self.step(self.now + 60)
        self.ha.set_select.assert_not_called()
        self.step(self.now + 3601)
        self.ha.set_select.assert_called_once_with(MODE, "vacation")
        self.assertFalse(self.controller.vacation_paused())
        self.step(self.now + 3660)
        self.assertTrue(self.controller.vacation_paused())
        self.step(self.now + 7201)
        self.assertFalse(self.controller.vacation_paused())
        self.assertEqual(self.mode, "home")
        self.step(self.now + 7260)
        self.assertIsNone(self.controller.vacation)
        self.assertIn("vacation_done", self.kinds())

    def test_paused_vacation_stops_offset_writes_and_control_resumes_at_end(self):
        self.controller.config = config(enabled=True, target_temperature=23)
        self.run_active()
        with patch("controller.time.time", return_value=self.now + 180):
            self.controller.tick()
        self.ha.set_offset.assert_not_called()
        self.assertIn("Semesterläge", self.controller.status["message"])
        self.assertEqual(self.controller.status["next_offset"], self.controller.status["current_offset"])
        with patch("controller.time.time", return_value=self.now + 3601):
            self.controller.tick()
        self.ha.set_offset.assert_called_once()
        self.assertEqual(self.mode, "normal")

    def test_restore_runs_even_when_room_sensors_pause_control(self):
        self.run_active()
        self.controller.config = config(enabled=True, room_sensors=["sensor.gone"])
        with patch("controller.time.time", return_value=self.now + 3601):
            with self.assertRaises(ControlError):
                self.controller.tick()
        self.assertEqual(self.mode, "normal")

    def test_control_continues_until_pump_confirms_and_write_is_retried(self):
        self.ha.set_select.side_effect = ControlError("timeout")
        self.schedule()
        self.step(self.now + 60)
        self.assertEqual(self.ha.set_select.call_count, 1)
        self.assertFalse(self.controller.vacation_paused())
        self.assertEqual(self.controller.vacation["error"], "timeout")
        self.step(self.now + 120)
        self.assertEqual(self.ha.set_select.call_count, 1)
        self.step(self.now + 60 + VACATION_RETRY_SECONDS)
        self.assertEqual(self.ha.set_select.call_count, 2)
        self.assertEqual(self.kinds().count("vacation_failed"), 1)

    def test_timeout_where_write_landed_is_confirmed_by_readback(self):
        def lands(_entity, option):
            self.mode = option
            raise ControlError("timeout")
        self.ha.set_select.side_effect = lands
        self.schedule()
        self.step(self.now + 60)
        self.step(self.now + 120)
        self.assertTrue(self.controller.vacation_paused())
        self.assertIsNone(self.controller.vacation["error"])

    def test_silent_write_is_reported_when_it_must_be_retried(self):
        self.ha.set_select.side_effect = None
        self.schedule()
        self.step(self.now + 60)
        self.assertIsNone(self.controller.vacation["error"])
        self.step(self.now + 60 + VACATION_RETRY_SECONDS)
        self.assertIn("inte bekräftat", self.controller.vacation["error"])

    def test_restore_retries_while_control_has_resumed(self):
        self.run_active()
        self.ha.set_select.side_effect = ControlError("timeout")
        self.step(self.now + 3601)
        self.assertEqual(self.controller.vacation["phase"], "restoring")
        self.assertFalse(self.controller.vacation_paused())
        self.ha.set_select.side_effect = lambda _entity, option: setattr(self, "mode", option)
        self.step(self.now + 3601 + VACATION_RETRY_SECONDS)
        self.step(self.now + 3700 + VACATION_RETRY_SECONDS)
        self.assertEqual(self.mode, "normal")
        self.assertIsNone(self.controller.vacation)

    def test_unavailable_selector_delays_restore_without_ending_the_plan(self):
        self.run_active()
        self.available = False
        self.step(self.now + 3601)
        self.assertEqual(self.controller.vacation["phase"], "restoring")
        self.available = True
        self.step(self.now + 3700)
        self.assertEqual(self.mode, "normal")

    def test_mode_changed_by_someone_else_is_left_alone(self):
        self.run_active()
        self.mode = "away"
        self.step(self.now + 1800)
        self.step(self.now + 3601)
        self.assertEqual(self.mode, "away")
        self.assertIsNone(self.controller.vacation)
        self.assertEqual(self.ha.set_select.call_count, 1)

    def test_pump_already_in_vacation_is_not_touched(self):
        self.mode = "vacation"
        self.schedule()
        self.step(self.now + 60)
        self.assertTrue(self.controller.vacation_paused())
        self.step(self.now + 3601)
        self.ha.set_select.assert_not_called()
        self.assertEqual(self.mode, "vacation")
        self.assertIsNone(self.controller.vacation)

    def test_plan_survives_restart_and_restores_after_downtime(self):
        self.run_active()
        restarted = self.make()
        restarted.config = config(enabled=True)
        self.assertTrue(restarted.vacation_paused())
        restarted.vacation_step(self.states(), self.now + 86400)
        self.assertEqual(self.mode, "normal")

    def test_period_missed_entirely_never_touches_the_pump(self):
        self.schedule(self.now + 60, hours=1)
        self.step(self.now + 7200)
        self.ha.set_select.assert_not_called()
        self.assertIsNone(self.controller.vacation)
        self.assertIn("vacation_missed", self.kinds())

    def test_cancel(self):
        self.schedule(self.now + 3600, hours=2)
        self.assertIsNone(self.controller.cancel_vacation())
        self.assertIsNone(self.controller.vacation)
        self.run_active()
        self.assertIsNotNone(self.controller.cancel_vacation())
        self.step(time.time() + 1)
        self.assertEqual(self.mode, "normal")
        with self.assertRaisesRegex(ControlError, "Ingen semesterperiod"):
            self.controller.cancel_vacation()

    def test_end_of_running_vacation_can_move_but_not_start(self):
        self.run_active()
        start = self.controller.vacation["start"]
        self.controller.schedule_vacation(self.now + 90000, self.now + 7200)
        self.assertEqual(self.controller.vacation["start"], start)
        self.assertEqual(self.controller.vacation["end"], self.now + 7200)
        self.step(self.now + 3601)
        self.assertEqual(self.mode, "vacation")

    def test_rejects_bad_periods_and_missing_selector(self):
        for start, end in ((None, self.now - 60), (self.now + 7200, self.now + 3600),
                           (None, self.now + 400 * 86400), (None, None), ("x", self.now + 60)):
            with self.assertRaises(ControlError):
                self.controller.schedule_vacation(start, end)
        self.ha.integration_registry.return_value = REGISTRY[:1]
        self.controller.peer_cache = (0, {})
        with self.assertRaisesRegex(ControlError, "Smart Home Mode"):
            self.schedule()
        self.assertIsNone(self.controller.vacation)

    def test_selector_on_another_device_is_not_used(self):
        self.ha.integration_registry.return_value = [REGISTRY[0], {**REGISTRY[1], "device_id": "other"}]
        with self.assertRaisesRegex(ControlError, "Smart Home Mode"):
            self.schedule()

    def test_snapshot_exposes_plan_and_saved_file_is_validated(self):
        self.assertIsNone(self.controller.snapshot()["vacation"])
        self.schedule()
        self.assertEqual(self.controller.snapshot()["vacation"]["phase"], "scheduled")
        saved = json.loads((Path(self.temp.name) / "vacation-state.json").read_text())
        self.assertEqual(validate_vacation(saved), saved)
        with self.assertRaises(ControlError):
            validate_vacation({"phase": "okänd"})
        self.assertIsNone(validate_vacation({}))


if __name__ == "__main__":
    unittest.main()
