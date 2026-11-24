import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AutoAlarmResetMonitor
from compressor_diagnostics import CompressorDiagnostics


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


class AutoAlarmResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.diagnostics = CompressorDiagnostics(Path(self.temp.name) / "diagnostics.sqlite")
        self.events = Mock()
        self.events.alarm_reset_attempted.return_value = False
        self.controller = SimpleNamespace(
            compressor_diagnostics=self.diagnostics,
            events=self.events,
            settings=lambda: {"min_interval_minutes": 30},
        )
        self.current_time = [1000]
        self.active_alarms = [[]]
        self.myuplink = Mock()
        self.myuplink.report.side_effect = lambda: {
            "configured": True, "system_id": "system", "device_id": "device",
            "checked_at": self.current_time[0], "alarms": self.active_alarms[0],
        }
        self.myuplink.reset.return_value = {"message": "Begäran accepterad"}
        self.monitor = AutoAlarmResetMonitor(self.controller, self.myuplink)
        self.peers = (COMPRESSOR, RETURN, ALARM, ALARM_CODE)

    def record_sample(self, timestamp):
        self.diagnostics.record(
            [state(OFFSET, 1), state(COMPRESSOR, "off"), state(RETURN, 30, "°C"),
             state(ALARM, "off"), state(ALARM_CODE, 0)],
            self.peers, OFFSET, {"min_interval_minutes": 30}, {}, timestamp,
        )

    def tick_at(self, timestamp, alarms=None):
        self.current_time[0] = timestamp
        self.active_alarms[0] = [] if alarms is None else alarms
        self.monitor.tick()

    def alarm(self, identifier="123"):
        numeric = identifier.isascii() and identifier.isdigit()
        return {"id": identifier, "code": 229, "status": "Active", "can_reset": numeric}

    def run_clear_baseline(self, omit_samples=()):
        for timestamp in range(1000, 2801, 60):
            if timestamp not in omit_samples:
                self.record_sample(timestamp)
            self.tick_at(timestamp)

    def run_alarm_window(self, alarm_id="123", omit_samples=()):
        self.run_clear_baseline(omit_samples)
        for timestamp in range(2800, 3401, 60):
            if timestamp not in omit_samples:
                self.record_sample(timestamp)
            self.tick_at(timestamp, [self.alarm(alarm_id)])

    def test_auto_reset_waits_for_complete_window_and_sends_one_request(self):
        self.run_clear_baseline()
        self.tick_at(2800, [self.alarm()])
        self.assertEqual(self.myuplink.reset.call_count, 0)
        for timestamp in range(2860, 3401, 60):
            self.record_sample(timestamp)
            self.tick_at(timestamp, [self.alarm()])
        self.assertEqual(self.myuplink.reset.call_count, 1)
        self.assertTrue(self.myuplink.reset.call_args.kwargs["automatic"])
        self.assertIn("Ingen kort kompressorkörning", self.myuplink.reset.call_args.kwargs["analysis_summary"])
        self.tick_at(3460, [self.alarm()])
        self.assertEqual(self.myuplink.reset.call_count, 1)
        self.assertEqual(self.diagnostics.auto_reset_history()[0]["state"], "submitted")

    def test_gap_in_diagnostic_window_blocks_without_reset(self):
        self.run_alarm_window(omit_samples=(1600, 1660))
        self.assertEqual(self.myuplink.reset.call_count, 0)
        history = self.diagnostics.auto_reset_history()
        self.assertEqual(history[0]["state"], "blocked")
        self.assertIn("Datagap", history[0]["message"])

    def test_unverified_notification_id_never_resets(self):
        self.run_alarm_window(alarm_id="e822a6ef-0cf2-4c0a-a06c-a9e1d3c9b798")
        self.assertEqual(self.myuplink.reset.call_count, 0)
        self.assertEqual(self.diagnostics.auto_reset_history()[0]["state"], "blocked")

    def test_failed_reset_is_terminal_and_not_retried(self):
        self.myuplink.reset.side_effect = OSError("timeout")
        self.run_alarm_window()
        self.tick_at(3460, [self.alarm()])
        self.assertEqual(self.myuplink.reset.call_count, 1)
        self.assertEqual(self.diagnostics.auto_reset_history()[0]["state"], "failed")

    def test_recommendations_treat_offset_changes_as_correlation(self):
        samples = [
            {"timestamp": 1000, "app": {"current_offset": 1}},
            {"timestamp": 1060, "app": {"current_offset": 2}},
        ]
        cycles = [{"classification": "Kort körning enligt prover", "start": 1060}]
        recommendations = self.diagnostics._recommendations(samples, cycles, {"min_interval_minutes": 30})
        self.assertTrue(any("tidsmässigt samband, inte orsak" in item for item in recommendations))
        self.assertTrue(any("flöde" in item for item in recommendations))


if __name__ == "__main__":
    unittest.main()
