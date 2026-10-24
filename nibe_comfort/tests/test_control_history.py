import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller import EventLog
from insights import control_history
from test_controller import config


def event(id, kind, stamp=100, **details):
    return {"id": id, "kind": kind, "timestamp": stamp, "details": details}


class ControlHistoryTests(unittest.TestCase):
    def setUp(self):
        self.config = config(room_sensors=["climate.room"])
        self.attempt = event(1, "write_attempt", entity_id="number.offset", previous=2, value=3,
                             contributions={"room": 1.5, "weather": 0, "price": -0.5},
                             indoor=21, outdoor=4, price=2.5, price_unit="SEK/kWh", target_temperature=22, preheat="coast")

    def test_request_and_confirmation_are_not_duplicate_changes(self):
        result = control_history([self.attempt, event(2, "write_sent", request_id=1),
                                  event(3, "write_observed", 120, request_id=1)], self.config, 0, 200)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual(result[0]["contributions"]["weather"], 0)
        self.assertEqual(result[0]["price"], 2.5)
        self.assertEqual(result[0]["price_unit"], "SEK/kWh")
        self.assertEqual(result[0]["outcome_at"], 120)

    def test_request_without_confirmation_is_never_confirmed(self):
        for kind, expected in (("write_sent", "sent"), ("write_failed", "failed"),
                               ("write_unconfirmed", "unconfirmed")):
            with self.subTest(kind=kind):
                result = control_history([self.attempt, event(2, kind, request_id=1)],
                                         self.config, 0, 200)
                self.assertEqual(result[0]["status"], expected)
        self.assertEqual(control_history([self.attempt], self.config, 0, 200)[0]["status"], "attempt")

    def test_thermostat_scope_units_and_latest_outcome(self):
        attempt = event(4, "thermostat_attempt", entity_id="climate.room", previous=75, value=71.6,
                        unit="°F", current=23, target_temperature=22)
        other = event(5, "thermostat_attempt", entity_id="climate.other", value=22)
        result = control_history([attempt, other, event(6, "thermostat_failed", 110, request_id=4),
                                  event(7, "thermostat_observed", 120, request_id=4)], self.config, 0, 200)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual(result[0]["unit"], "°F")
        self.assertEqual(result[0]["indoor"], 23)
        self.assertIsNone(result[0]["contributions"])

    def test_wrong_pump_old_requests_and_orphan_outcomes_are_not_attributed(self):
        other = event(3, "write_attempt", entity_id="number.other", value=3)
        self.assertEqual(control_history([self.attempt, other, event(2, "write_observed", request_id=999)],
                                         self.config, 110, 200), [])
        result = control_history([self.attempt, event(2, "write_observed", 120, request_id=1)],
                                 self.config, 110, 200)
        self.assertEqual(result[0]["requested_at"], 100)
        self.assertEqual(result[0]["timestamp"], 120)

    def test_observed_changes_do_not_invent_a_cause(self):
        result = control_history([event(2, "offset_observed", entity_id="number.offset",
                                        previous=3, value=4)], self.config, 0, 200)
        self.assertEqual(result[0]["status"], "observed")
        self.assertNotIn("contributions", result[0])

    def test_time_window_is_not_limited_to_latest_hundred_and_keeps_linked_request(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory) / "events.sqlite")
            with patch("controller.time.time", return_value=90):
                request = log.add("write_attempt", "request", entity_id="number.offset", value=3)
            with patch("controller.time.time", return_value=120):
                log.add("write_observed", "confirmed", request_id=request)
                for i in range(150):
                    log.add("paused", str(i))
            rows = log.history(100, 200)
            self.assertEqual(len(rows), 152)
            self.assertEqual(rows[0]["id"], request)
            self.assertEqual(control_history(rows, self.config, 100, 200)[0]["status"], "confirmed")
