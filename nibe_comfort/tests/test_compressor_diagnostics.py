import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compressor_diagnostics import CompressorDiagnostics
from controller import Controller


OFFSET = "number.pump_offset"
COMPRESSOR = "sensor.pump_status_compressor_1001"
RETURN = "sensor.pump_return_line_bt3_1002"
ALARM = "binary_sensor.pump_alarm"
ALARM_CODE = "sensor.pump_alarms_1003"


def state(entity_id, value, unit=None):
    attributes = {"friendly_name": entity_id}
    if unit:
        attributes["unit_of_measurement"] = unit
    return {"entity_id": entity_id, "state": str(value), "attributes": attributes}


class CompressorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = CompressorDiagnostics(Path(self.temp.name) / "diagnostics.sqlite")
        self.config = {"offset_entity": OFFSET, "target_temperature": 21, "price_mode": "tibber"}
        self.peers = (COMPRESSOR, RETURN, ALARM, ALARM_CODE)

    def sample(self, timestamp, running, return_temp=30, alarm="off", alarm_code="0"):
        return self.log.record(
            [state(OFFSET, 1), state(COMPRESSOR, running), state(RETURN, return_temp, "°C"),
             state(ALARM, alarm), state(ALARM_CODE, alarm_code)],
            self.peers, OFFSET, self.config,
            {"price_level": "expensive", "target_shift": -0.5, "current_offset": 1},
            timestamp,
        )

    def test_persists_temperature_series_short_run_and_alarm_rising_edge(self):
        self.sample(1000, "off")
        self.sample(1060, "on", 29)
        self.sample(1120, "on", 28)
        self.sample(1240, "off", 27)
        self.sample(1300, "off", 27, alarm="on", alarm_code="229")
        self.sample(1360, "off", 27, alarm="on", alarm_code="229")

        report = self.log.report(6, now=1400)
        self.assertEqual(report["sample_count"], 6)
        self.assertEqual(report["temperatures"][0]["points"], [[1000, 30.0], [1060, 29.0], [1120, 28.0],
                                                                 [1240, 27.0], [1300, 27.0], [1360, 27.0]])
        self.assertEqual(len(report["cycles"]), 1)
        self.assertEqual(report["cycles"][0]["classification"], "Kort körning enligt prover")
        self.assertEqual(report["cycles"][0]["minimum_seconds"], 60)
        self.assertEqual(report["cycles"][0]["maximum_seconds"], 240)
        self.assertEqual(len(report["alarms"]), 1)
        self.assertEqual(report["alarms"][0]["code"], 229)
        self.assertEqual(report["alarms"][0]["sample"]["app"]["price_level"], "expensive")

    def test_unknown_compressor_state_does_not_invent_a_run(self):
        self.sample(1000, "off")
        self.sample(1060, "heating")
        self.sample(1120, "off")
        self.assertEqual(self.log.report(6, now=1200)["cycles"], [])

    def test_unknown_states_and_long_sampling_gaps_break_duration_estimates(self):
        self.sample(1000, "off")
        self.sample(1060, "on")
        self.sample(1120, "unknown")
        self.sample(2000, "on")
        self.sample(2060, "on")
        self.sample(2120, "off")
        cycles = self.log.report(6, now=2200)["cycles"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["classification"], "Osäkert intervall runt fem minuter")
        self.assertIsNone(cycles[0]["maximum_seconds"])

    def test_duplicate_fast_samples_are_skipped_and_period_is_validated(self):
        self.assertTrue(self.sample(1000, "off"))
        self.assertFalse(self.sample(1010, "on"))
        self.assertEqual(self.log.report(6, now=1100)["sample_count"], 1)
        with self.assertRaises(ValueError):
            self.log.report(12, now=1100)

    def test_controller_records_the_selected_pump_after_each_tick(self):
        controller = Controller(Mock(), self.temp.name)
        controller.config["offset_entity"] = OFFSET
        states = [state(OFFSET, 1), state(COMPRESSOR, "on")]
        controller.control_tick = Mock(side_effect=lambda: (
            setattr(controller, "tick_entities", states),
            setattr(controller, "tick_pump_peers", self.peers),
        ))
        controller.radiator_tick = Mock()
        controller.report = Mock()
        diagnostics = Mock()
        controller.compressor_diagnostics = diagnostics

        controller.tick()

        diagnostics.record.assert_called_once_with(
            states, self.peers, OFFSET, controller.settings(), controller.snapshot(),
        )


if __name__ == "__main__":
    unittest.main()
