import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from controller import ControlError, Controller, evaluate, fireplace_contribution
from fireplace import empty_state, intervals, update, validate_state
from insights import Insights, planned_offsets
from test_controller import config, entity, fixtures
from test_learning import synthetic_house

HOUR = 3600


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(fireplace_enabled=True, fireplace_rooms=["sensor.living"])
        self.state = empty_state()
        self.events = []

    def observe(self, minute, local=21, other=21):
        readings = [{"entity_id": key, "temperature": value, "error": None}
                    for key, value in (("sensor.living", local), ("sensor.bedroom", other))]
        self.state, result, events = update(self.state, self.cfg, readings, minute * 60)
        self.events.extend(events)
        validate_state(self.state)
        return result

    def heat(self):
        for minute in range(0, 61, 5):
            self.observe(minute)
        for minute in range(65, 126, 5):
            result = self.observe(minute, 21 + (minute - 60) / 60 * 1.2)
        self.assertEqual(result["phase"], "active")
        return result

    def test_requires_sustained_local_rise_and_never_claims_confirmed_fire(self):
        for minute in range(0, 61, 5):
            self.observe(minute)
        row = self.observe(65, 21.9)
        self.assertEqual(row["phase"], "candidate")
        self.observe(70, 21.9)
        row = self.observe(75, 21.9)
        self.assertEqual(row["phase"], "active")
        self.assertIn("sol", row["message"])
        self.assertEqual(len(self.events), 1)
        self.assertIsNotNone(row["active_since"])
        self.assertEqual(len(intervals(self.state)), 1)
        self.assertFalse(row["learned"])

    def test_common_heating_or_slow_rise_is_not_local_fire(self):
        for minute in range(0, 181, 5):
            row = self.observe(minute, 21 + minute / 60, 21 + minute / 60)
        self.assertEqual(row["phase"], "idle")
        self.assertEqual(self.events, [])
        self.state = empty_state()
        for minute in range(0, 181, 5):
            row = self.observe(minute, 21 + minute / 600, 21)
        self.assertEqual(row["phase"], "idle")
        self.assertEqual(self.events, [])

    def test_spike_without_persistence_does_not_start_episode(self):
        for minute in range(0, 61, 5):
            self.observe(minute)
        self.observe(65, 22)
        row = self.observe(70, 21)
        self.assertEqual(row["phase"], "idle")
        self.assertEqual(self.events, [])

    def test_detection_thresholds_and_confirmation_boundary(self):
        for minute in range(0, 121, 5):
            row = self.observe(minute, 21 + minute / 60 * 0.69)
        self.assertNotEqual(row["phase"], "active")
        self.assertEqual(self.events, [])
        self.state = empty_state()
        for minute in range(0, 121, 5):
            row = self.observe(minute, 21 + minute / 60, 21 + minute / 60 * 0.6)
        self.assertNotEqual(row["phase"], "active", "A rise similar to comparison rooms is not sufficient")
        self.state = empty_state()
        for minute in range(0, 61, 5):
            self.observe(minute)
        self.observe(65, 22)
        self.observe(70, 22)
        self.assertEqual(self.observe(74, 22)["phase"], "candidate")
        self.assertEqual(self.observe(75, 22)["phase"], "active")

    def test_needs_comparison_and_fresh_data(self):
        cfg = {**self.cfg, "room_sensors": ["sensor.living"]}
        _, row, _ = update(empty_state(), cfg, [], 0)
        self.assertEqual(row["phase"], "insufficient")
        self.heat()
        self.state, row, events = update(self.state, self.cfg, [], 150 * 60)
        self.assertEqual(row["phase"], "unavailable")
        self.assertIsNone(self.state["active"])
        self.assertFalse(self.state["episodes"][-1]["complete"])
        self.assertEqual(events[0]["kind"], "fireplace_end")

    def test_restart_gap_closes_incomplete_not_learned(self):
        self.heat()
        before = copy.deepcopy(self.state)
        self.state = json.loads(json.dumps(self.state))
        row = self.observe(360, 22)
        self.assertIsNone(row["active_since"])
        self.assertFalse(self.state["episodes"][-1]["complete"])
        self.assertEqual(before["samples"][-1]["at"], self.state["episodes"][-1]["end"])

    def test_same_scope_survives_short_restart(self):
        self.heat()
        start = self.state["active"]["start"]
        self.state = validate_state(json.loads(json.dumps(self.state)))
        self.assertEqual(self.observe(130, 22.2)["active_since"], start)
        self.assertEqual(len(self.events), 1)

    def test_samples_are_bounded_and_not_written_every_tick(self):
        self.observe(0)
        unchanged = copy.deepcopy(self.state)
        self.observe(1)
        self.assertEqual(self.state, unchanged)
        for minute in range(5, 3 * 1440, 5):
            self.observe(minute)
        self.assertLessEqual(len(self.state["samples"]), 289)

    def test_completed_duration_and_spread_need_three_matching_episodes(self):
        for episode in range(3):
            start = episode * 600
            for minute in range(start, start + 61, 5):
                self.observe(minute)
            for minute in range(start + 65, start + 126, 5):
                self.observe(minute, 21 + (minute - start - 60) / 60 * 1.2)
            for minute in range(start + 130, start + 241, 5):
                row = self.observe(minute, 21.2, 21.3)
        self.assertEqual(row["completed"], 3)
        self.assertTrue(row["learned"])
        self.assertGreater(row["duration_hours"], 1)
        other = next(r for r in row["room_effects"] if r["entity_id"] == "sensor.bedroom")
        self.assertAlmostEqual(other["rise"], 0.3, delta=0.01)
        self.assertIsNotNone(other["arrival_minutes"])
        self.assertEqual(len(intervals(self.state)), 3)

    def test_disabling_and_room_changes_do_not_reuse_unfinished_episode(self):
        self.heat()
        self.cfg["fireplace_enabled"] = False
        row = self.observe(130)
        self.assertEqual(row["phase"], "off")
        self.assertFalse(self.state["episodes"][-1]["complete"])
        self.assertEqual(self.state["samples"], [])
        self.cfg["fireplace_enabled"] = True
        self.cfg["fireplace_rooms"] = ["sensor.bedroom"]
        row = self.observe(135)
        self.assertEqual(row["completed"], 0)
        self.assertEqual(row["phase"], "learning")

    def test_long_unresolved_heat_is_not_a_completed_training_episode(self):
        self.heat()
        for minute in range(130, 20 * 60, 5):
            self.observe(minute, 23)
        self.assertTrue(self.state["episodes"])
        self.assertFalse(self.state["episodes"][0]["complete"])

    def test_corrupt_persistence_is_rejected(self):
        for bad in ({}, [], {"version": 99}, {**empty_state(), "samples": [{"at": 0, "values": {"a": float("nan")}}]},
                    {**empty_state(), "active": {"start": 0}},
                    {**empty_state(), "scope": [None]}):
            with self.assertRaises(ValueError):
                validate_state(bad)


class ControllerFireplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = []
        self.now = time.time()
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(fireplace_enabled=True, fireplace_rooms=["sensor.living"])
        self.controller.save(self.cfg)

    def tick(self, minute, local=21, other=21):
        now = self.now + minute * 60
        self.ha.states.return_value = [
            entity("sensor.living", local, now, unit_of_measurement="°C"),
            entity("sensor.bedroom", other, now, unit_of_measurement="°C"),
            entity("number.offset", 0, now, min=-10, max=10, step=1),
        ]
        with patch("controller.time.time", return_value=now):
            self.controller.tick()

    def test_opt_in_preserves_control_and_records_before_any_physical_writes(self):
        for minute in range(0, 126, 5):
            self.tick(minute, 21 + max(0, minute - 60) / 60)
        status = self.controller.snapshot()
        self.assertEqual(status["fireplace"]["phase"], "active")
        expected = evaluate(self.cfg, self.ha.states.return_value, self.now + 125 * 60, fireplace_active=True,
                            indoor_samples=self.controller.indoor_samples[:-1])
        self.assertEqual(status["contributions"], expected["contributions"])
        self.assertEqual(status["next_offset"], expected["next_offset"])
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()
        self.assertIn("fireplace_start", [e["kind"] for e in self.controller.events.recent()])
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.fireplace_state, self.controller.fireplace_state)
        self.assertEqual(restored.fireplace_observations(), self.controller.fireplace_observations())

    # Checks the fireplace term on top of the instantaneous room rule; smoothing has its own tests.
    @patch("controller.SMOOTHING_SECONDS", 1)
    def test_enabled_control_adds_single_bounded_reduction_to_priority_average(self):
        self.tick(0)
        self.cfg = {**self.cfg, "enabled": True, "priority_sensors": ["sensor.living"], "min_offset": -5}
        self.controller.save(self.cfg)
        for minute in range(5, 181, 5):
            self.tick(minute, 21 + max(0, minute - 60) / 60, 20)
        status = self.controller.snapshot()
        self.assertEqual(status["fireplace"]["phase"], "active")
        self.assertEqual(status["indoor"], 23)
        self.assertEqual(status["house_indoor"], 21.5)
        self.assertEqual(status["contributions"]["room"], -3)
        self.assertEqual(status["contributions"]["fireplace"], -1)
        self.assertEqual(status["desired_offset"], -4)
        attempts = [e for e in self.controller.events.recent() if e["kind"] == "write_attempt"]
        self.assertTrue(any(e["details"]["fireplace"] == "active" for e in attempts))
        self.assertTrue(all(e["details"]["contributions"]["room"] >= -3 for e in attempts))
        self.assertTrue(all(e["details"]["contributions"]["fireplace"] in (-1, 0) for e in attempts))
        ordered = sorted(e["timestamp"] for e in attempts)
        self.assertTrue(all(b - a >= 1800 for a, b in zip(ordered, ordered[1:])))
        for minute in (185, 190):
            self.tick(minute, 23, 20)
            self.assertEqual(self.controller.snapshot()["desired_offset"], -4, "Not another reduction on every tick")

    def test_cold_guard_uses_ordinary_target_and_current_scope(self):
        for indoor, expected in ((20.69, 0), (20.7, -1), (21, -1), (23, -1)):
            self.assertEqual(fireplace_contribution(self.cfg, indoor, True), expected)
        self.assertEqual(fireplace_contribution(self.cfg, 23, False), 0)
        self.assertEqual(fireplace_contribution({**self.cfg, "fireplace_enabled": False}, 23, True), 0)
        self.cfg.update(priority_sensors=["sensor.bedroom"], preheat_enabled=True, price_entity="sensor.price")
        states = fixtures(self.now)
        states[0]["state"], states[1]["state"] = "25", "20.6"
        result = evaluate(self.cfg, states, self.now, target_shift=-0.5, fireplace_active=True)
        self.assertEqual(result["indoor"], 20.6)
        self.assertEqual(result["contributions"]["fireplace"], 0, "Coasting must not weaken the ordinary cold-room guard")
        self.cfg["priority_sensors"] = []
        result = evaluate(self.cfg, states, self.now, fireplace_active=True)
        self.assertEqual(result["contributions"]["fireplace"], -1, "Without priorities the selected-room average governs")

    def test_current_missing_readings_remove_fireplace_reduction(self):
        for minute in range(0, 126, 5):
            self.tick(minute, 21 + max(0, minute - 60) / 60)
        self.assertEqual(self.controller.snapshot()["contributions"]["fireplace"], -1)
        with patch("controller.time.time", return_value=self.now + 150 * 60):
            self.controller.tick()
        status = self.controller.snapshot()
        self.assertEqual(status["fireplace"]["phase"], "unavailable")
        self.assertEqual(status["contributions"]["fireplace"], 0)

    def test_finished_observation_recomputes_normal_goal_instead_of_restoring_old_offset(self):
        for minute in range(0, 126, 5):
            self.tick(minute, 21 + max(0, minute - 60) / 60)
        self.assertEqual(self.controller.snapshot()["contributions"]["fireplace"], -1)
        for minute in range(130, 186, 5):
            self.tick(minute, 21.1)
        status = self.controller.snapshot()
        self.assertEqual(status["fireplace"]["phase"], "idle")
        self.assertEqual(status["contributions"]["fireplace"], 0)
        self.assertEqual(status["desired_offset"], 0)
        self.assertEqual(status["fireplace"]["completed"], 1)

    def test_stale_input_does_not_detect_heat_even_if_control_freshness_allows_it(self):
        self.tick(0)
        with patch("controller.time.time", return_value=self.now + HOUR):
            self.controller.tick()
        self.assertEqual(self.controller.snapshot()["fireplace"]["phase"], "unavailable")

    def test_observation_disk_failure_prevents_physical_control(self):
        self.controller.config = {**self.cfg, "enabled": True}
        with patch("controller.atomic_json", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.tick(0)
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_migration_and_invalid_selection(self):
        old = {k: v for k, v in self.cfg.items() if not k.startswith("fireplace_")}
        Path(self.temp.name, "settings.json").write_text(json.dumps(old))
        restored = Controller(self.ha, self.temp.name)
        self.assertFalse(restored.settings()["fireplace_enabled"])
        self.assertEqual(restored.settings()["fireplace_rooms"], [])
        for changes in ({"fireplace_enabled": "yes"}, {"fireplace_rooms": "sensor.living"},
                        {"fireplace_rooms": ["sensor.unknown"]}, {"fireplace_enabled": True},
                        {"fireplace_enabled": True, "fireplace_rooms": ["sensor.living", "sensor.bedroom"]}):
            with self.assertRaises(ControlError):
                config(**changes)
        Path(self.temp.name, "fireplace-state.json").write_text("{}")
        with self.assertRaises(ControlError):
            Controller(self.ha, self.temp.name)


class FireplaceLearningTests(unittest.TestCase):
    def test_planner_includes_current_fireplace_contribution_and_cold_guard(self):
        cfg = config(fireplace_enabled=True, fireplace_rooms=["sensor.living"], target_temperature=21)
        states = fixtures()
        status = evaluate(cfg, states, time.time(), fireplace_active=True)
        status["fireplace"] = {"phase": "active"}
        result = planned_offsets(cfg, status, [], [], time.time(), time.time() + 3600, False)
        self.assertEqual(result["desired"][0][1], -1)
        self.assertTrue(any("kaminbidrag" in reason for change in result["changes"] for reason in change["reasons"]))
        cfg["target_temperature"] = 23
        status = evaluate(cfg, states, time.time(), fireplace_active=True)
        status["fireplace"] = {"phase": "active"}
        result = planned_offsets(cfg, status, [], [], time.time(), time.time() + 3600, False)
        self.assertFalse(any("kaminbidrag" in reason for change in result["changes"] for reason in change["reasons"]))

    def test_excludes_suspected_heat_and_tail_without_changing_plotted_history(self):
        start, indoor, outdoor, offset = synthetic_house()
        ha = Mock()
        ha.request.return_value = []
        ha.states.return_value = fixtures()
        insights = Insights(ha)
        rows = [(eid, h, v) for eid, values in (("sensor.living", indoor), ("sensor.bedroom", indoor),
                ("sensor.outdoor", outdoor), ("number.offset", offset)) for h, v in values.items()]
        insights.store.add(rows, max(indoor))
        cfg = config(outdoor_entity="sensor.outdoor")
        episode = {"start": start + 100 * HOUR, "end": start + 105 * HOUR}
        insights.fireplace_provider = lambda: [episode]
        with patch("insights.time.time", return_value=max(indoor)):
            result = insights.house_model(cfg)
            self.assertEqual(len(result["indoor"]), len(indoor))
            self.assertEqual(result["excluded_hours"], 13)
            self.assertEqual(result["model"]["hours"], len(indoor) - 13)
            self.assertIn("undantas", result["model"]["message"])
            episode["end"] = None
            active = insights.house_model(cfg)
            self.assertLess(active["model"]["hours"], result["model"]["hours"], "New exclusions invalidate the model cache")

    def test_temperature_prediction_is_not_presented_as_reliable_during_extra_heat(self):
        insights = Insights(Mock())
        with patch.object(insights, "house_model", return_value={
                "model": {"beta": [0, -0.1, 0.2, 0.01]}, "indoor": {}, "outdoor": {}, "offset": {}, "error": None}), \
                patch.object(insights, "preheat", return_value={"windows": [], "lead_hours": 3, "lead_source": "default", "note": ""}):
            result = insights.learning(config(), {"indoor": 23, "fireplace": {"active_since": 1}})
        self.assertEqual(result["prediction"], [])
        self.assertIn("extravärme", result["prediction_note"])


if __name__ == "__main__":
    unittest.main()
