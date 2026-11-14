import json
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError, Controller, device_peers, pump_alarm
from insights import Insights, overview
from test_controller import config, entity, fixtures


ALARM = "binary_sensor.pump_alarm"
CODE = "sensor.pump_alarms"
REGISTRY = [{"entity_id": key, "device_id": "pump", "platform": "myuplink"}
            for key in ("number.offset", ALARM, CODE)]


class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.states = fixtures(self.now) + [entity(ALARM, "on", self.now), entity(CODE, 229, self.now)]
        self.cfg = config(enabled=True, target_temperature=23)
        self.peers = device_peers(REGISTRY)
        self.ha = Mock()
        self.ha.states.return_value = self.states
        self.ha.integration_registry.return_value = REGISTRY
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Controller(self.ha, self.temp.name)
        self.controller.config = self.cfg

    def test_code_and_active_status_require_selected_device_and_fresh_values(self):
        alarm = pump_alarm(self.states, self.cfg, self.peers, self.now)
        self.assertTrue(alarm["active"])
        self.assertEqual(alarm["code"], 229)
        self.assertIsNone(alarm["error"])
        self.assertIsNotNone(pump_alarm(self.states, config(offset_entity="number.other"), self.peers, self.now)["error"])
        for invalid in ("unknown", "unavailable", "invalid"):
            self.states[-1]["state"] = invalid
            self.assertIsNotNone(pump_alarm(self.states, self.cfg, self.peers, self.now)["error"])
        stale = pump_alarm(self.states, self.cfg, self.peers, self.now + 86400)
        self.assertIsNotNone(stale["error"])

    def test_unknown_code_is_not_229_and_cleared_alarm_does_not_keep_old_code(self):
        self.states[-1]["state"] = "999"
        self.assertEqual(pump_alarm(self.states, self.cfg, self.peers, self.now)["code"], 999)
        self.states[-2]["state"] = "off"
        alarm = pump_alarm(self.states, self.cfg, self.peers, self.now)
        self.assertFalse(alarm["active"])
        self.assertIsNone(alarm["code"])

    def test_limiter_pause_persists_only_flag_and_never_resets_or_opens_valves(self):
        self.controller.config = {**self.cfg, "thermostat_control_enabled": True}
        before = self.controller.config.copy()
        saved = self.controller.pause_alarm_thermostats()
        self.assertEqual(saved, {**before, "thermostat_control_enabled": False})
        self.assertEqual(json.loads(self.controller.path.read_text()), saved)
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()
        self.ha.set_select.assert_not_called()
        self.assertEqual(self.controller.pause_alarm_thermostats(), saved)

    def test_invalid_alarm_cannot_apply_action(self):
        for state in ("999", "unknown", "unavailable"):
            self.states[-1]["state"] = state
            with self.assertRaises(ControlError):
                self.controller.pause_alarm_thermostats()
        self.assertFalse(self.controller.path.exists())

    def test_foreign_devices_entries_and_ambiguous_codes_cannot_authorize_pause(self):
        for extra in ({"device_id": "other"}, {"config_entry_id": "other"}):
            registry = [r if r["entity_id"] != CODE else {**r, **extra} for r in REGISTRY]
            alarm = pump_alarm(self.states, self.cfg, device_peers(registry), self.now)
            self.assertIsNotNone(alarm["error"])
        registry = [*REGISTRY, {"entity_id": "sensor.duplicate_alarms", "device_id": "pump", "platform": "myuplink"}]
        self.assertIsNotNone(pump_alarm(self.states, self.cfg, device_peers(registry), self.now)["error"])

    def test_failed_persistence_does_not_claim_pause(self):
        self.controller.config = {**self.cfg, "thermostat_control_enabled": True}
        with patch("controller.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.controller.pause_alarm_thermostats()
        self.assertTrue(self.controller.config["thermostat_control_enabled"])

    def test_alarm_holds_automatic_writes_and_blocks_manual_test(self):
        self.controller.tick()
        self.assertEqual(self.controller.snapshot()["next_offset"], 0)
        self.assertIn("Larm 229", self.controller.snapshot()["message"])
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()
        with self.assertRaisesRegex(ControlError, "aktivt larm"):
            self.controller.start_test()
        self.states[-2]["state"] = "off"
        self.controller.tick()
        self.ha.set_offset.assert_called_once()

    def test_hold_really_stops_enabled_limiter_and_preserves_pending_error_message(self):
        self.states.append(entity("climate.living", "heat", self.now, current_temperature=23,
                                  temperature=25, min_temp=5, max_temp=30, target_temp_step=0.5,
                                  supported_features=1, temperature_unit="°C"))
        self.controller.config = config(enabled=True, room_sensors=["climate.living"],
                                        thermostat_control_enabled=True, nearest_thermostat="climate.living")
        self.controller.pending = {"request_id": 1, "timestamp": self.now, "entity_id": "number.offset",
                                   "value": 1, "error": "Previous timeout"}
        self.controller.tick()
        self.assertIn("Larm 229", self.controller.snapshot()["message"])
        self.assertIn("larm 229", self.controller.snapshot()["thermostat_control"]["rooms"][0]["message"])
        self.ha.set_thermostat.assert_not_called()
        self.states[-3]["state"] = "off"
        self.controller.tick()
        self.ha.set_thermostat.assert_called_once_with("climate.living", self.controller.config["target_temperature"])

    def test_existing_test_can_restore_despite_new_alarm(self):
        self.states[-2]["state"] = "off"
        self.controller.start_test()
        self.states[-2]["state"] = "on"
        next(s for s in self.states if s["entity_id"] == "number.offset")["state"] = "1"
        self.controller.test["restore_at"] = 0
        self.controller.tick()
        self.assertEqual(self.ha.set_offset.call_args_list, [call("number.offset", 1), call("number.offset", 0)])
        self.assertEqual(self.controller.test["phase"], "restoring")

    def test_report_has_no_reset_or_asserted_valve_position(self):
        report = Insights(self.ha).alarms(self.cfg)
        self.assertEqual(report["code"], 229)
        self.assertFalse(report["reset_supported"])
        self.assertFalse(report["can_pause"])
        self.assertIn("inte en fastställd diagnos", report["explanation"])
        self.assertEqual(len(report["rooms"]), 2)


class CodeOnlyAlarmTests(unittest.TestCase):
    """Models like F370 have no alarm status entity, only the latest alarm number, which stays after the alarm clears."""
    CODE = "sensor.pump_alarms_45001"
    MODULE = "sensor.pump_alarms_eb100_ep14_44137"

    def setUp(self):
        self.now = time.time()
        self.registry = [{"entity_id": key, "device_id": "pump", "platform": "myuplink"}
                         for key in ("number.offset", self.CODE, self.MODULE)]
        self.states = fixtures(self.now) + [entity(self.CODE, "184", self.now), entity(self.MODULE, "51", self.now)]
        self.cfg = config(enabled=True)

    def alarm(self, registry=None):
        return pump_alarm(self.states, self.cfg, device_peers(registry or self.registry), self.now)

    def test_latest_code_is_shown_but_never_treated_as_active(self):
        alarm = self.alarm()
        self.assertIsNone(alarm["active"])
        self.assertIsNone(alarm["code"])
        self.assertEqual(alarm["latest_code"], 184)
        self.assertIn("184 Filterlarm", alarm["error"])
        self.assertEqual(alarm["info"]["name"], "Filterlarm")
        self.states[-2]["state"] = "229"
        self.assertIsNone(self.alarm()["active"])

    def test_code_only_alarm_never_holds_control_or_blocks_test(self):
        self.states[-2]["state"] = "229"
        ha = Mock()
        ha.states.return_value = self.states
        ha.integration_registry.return_value = self.registry
        with tempfile.TemporaryDirectory() as path:
            controller = Controller(ha, path)
            controller.config = self.cfg
            controller.tick()
            self.assertNotIn("Larm 229", controller.snapshot()["message"])
            controller.start_test()

    def test_sub_module_and_ambiguous_codes(self):
        self.assertEqual(self.alarm()["latest_code"], 184)
        duplicate = [*self.registry, {"entity_id": "sensor.pump_alarms_45002", "device_id": "pump", "platform": "myuplink"}]
        alarm = self.alarm(duplicate)
        self.assertIsNone(alarm["latest_code"])
        self.assertIn("entydig", alarm["error"])

    def test_status_entity_still_decides(self):
        registry = [*self.registry, {"entity_id": ALARM, "device_id": "pump", "platform": "myuplink"}]
        self.states += [entity(ALARM, "off", self.now)]
        alarm = self.alarm(registry)
        self.assertFalse(alarm["active"])
        self.assertIsNone(alarm["error"])

    def test_overview_shows_latest_code_without_active_state(self):
        item = next(i for i in overview(self.states, self.registry, self.cfg, self.now)["pump"] if i["key"] == "alarm")
        self.assertRegex(item["value"], r"^Senaste larmkod 184 Filterlarm \(åtminstone sedan \d+ \w+\) · aktiv status saknas$")
        self.assertEqual((item["active"], item["error"]), (None, None))
        # The UI shows the bare code as the tile value and the rest as detail text.
        self.assertEqual((item["code"], type(item["since"])), (184, float))
        self.assertEqual(item["code_name"], "Filterlarm")
        self.states[-2]["state"] = "0"
        item = next(i for i in overview(self.states, self.registry, self.cfg, self.now)["pump"] if i["key"] == "alarm")
        self.assertEqual((item["value"], item["active"]), ("Senaste larmkod 0 · aktiv status saknas", None))


class AlarmTextTests(unittest.TestCase):
    def test_known_codes_have_plain_text_and_unknown_ones_stay_numbers(self):
        from alarm_codes import ALARMS, alarm_info, alarm_label
        self.assertEqual(alarm_label(184), "184 Filterlarm")
        self.assertIn("rengör", alarm_info(184)["action"].lower())
        self.assertEqual(alarm_label(99999), "99999")
        self.assertIsNone(alarm_info(None))
        self.assertIsNone(alarm_info("184"))
        for code, (name, meaning, action, severity) in ALARMS.items():
            self.assertTrue(name and meaning.endswith(".") and action.endswith("."), code)
            self.assertIn(severity, ("låg", "medel", "hög"), code)

    def test_explanation_names_the_latest_code_and_what_to_do(self):
        from insights import alarm_explanation
        text = alarm_explanation(184, None)
        self.assertIn("Senaste larmkod 184 är Filterlarm", text)
        self.assertIn("Föreslagen åtgärd: Rengör frånluftsfiltret", text)
        self.assertIn("inte verifierad för din modell", text)
        self.assertIn("display", text)
        self.assertIn("Larm 63 Lågt luftflöde:", alarm_explanation(63, True))
        self.assertIn("finns inte i Comforts lista", alarm_explanation(4711, None))
        self.assertIn("Inget aktuellt larm 229", alarm_explanation(None, None))


class AcknowledgedAlarmTests(CodeOnlyAlarmTests):
    """A generic notification counter cannot prove the status of the latest alarm."""
    NOTES = "sensor.pump_notifications"

    def with_notes(self, value):
        self.registry = [*self.registry, {"entity_id": self.NOTES, "device_id": "pump", "platform": "myuplink"}]
        self.states = [*self.states, entity(self.NOTES, value, self.now)]

    def test_no_open_notifications_does_not_confirm_acknowledgement(self):
        self.with_notes("0")
        alarm = self.alarm()
        self.assertIsNone(alarm["active"])
        self.assertIsNotNone(alarm["error"])
        self.assertEqual(alarm["latest_code"], 184)
        self.assertEqual(alarm["info"]["name"], "Filterlarm")
        item = next(i for i in overview(self.states, self.registry, self.cfg, self.now)["pump"] if i["key"] == "alarm")
        self.assertIsNone(item["active"])
        self.assertIn("aktiv status saknas", item["value"])

    def test_open_or_unreadable_notifications_keep_the_status_unknown(self):
        for value in ("2", "unknown", "1.5"):
            with self.subTest(value=value):
                self.setUp()
                self.with_notes(value)
                alarm = self.alarm()
                self.assertIsNone(alarm["active"])
                self.assertIn("184 Filterlarm", alarm["error"])
        # A zero notification counter does not establish the status of code 229.
        self.setUp()
        self.with_notes("0")
        self.states[-3]["state"] = "229"
        self.assertEqual((self.alarm()["active"], self.alarm()["code"]), (None, None))


class CloudAlarmTests(CodeOnlyAlarmTests):
    """myUplink's own active alarm list decides for pumps without a status entity, for the same device only."""
    DEVICE = "emmy-r-1"

    def cloud(self, codes=(), device=None, age=10, configured=True):
        return {"configured": configured, "system_id": "sys", "device_id": device or self.DEVICE,
                "alarms": [{"id": str(i + 1), "code": c, "status": "Active"} for i, c in enumerate(codes)],
                "checked_at": self.now - age}

    def insights(self, identifiers=None):
        ha = Mock()
        ha.states.return_value = self.states
        ha.integration_registry.return_value = self.registry
        ha.device_identifiers.return_value = [["myuplink", self.DEVICE]] if identifiers is None else identifiers
        return Insights(ha)

    def tile(self, cloud, **kwargs):
        return next(i for i in self.insights(**kwargs).overview(self.cfg, cloud)["pump"] if i["key"] == "alarm")

    def test_empty_cloud_list_means_no_active_alarm(self):
        alarm = self.insights().alarms(self.cfg, self.cloud())
        self.assertEqual((alarm["active"], alarm["code"], alarm["latest_code"], alarm["error"]), (False, None, 184, None))
        self.assertEqual(alarm["source"], "myUplink")
        self.assertIn("Inget aktivt larm enligt myUplink", alarm["explanation"])
        item = self.tile(self.cloud())
        self.assertEqual((item["active"], item["code"], item["value"], item["source"]), (False, 184, "Inget aktivt larm", "myUplink"))

    def test_listed_code_is_active_but_never_allows_pause(self):
        self.states[-2]["state"] = "229"
        self.cfg["thermostat_control_enabled"] = True
        alarm = self.insights().alarms(self.cfg, self.cloud([229]))
        self.assertEqual((alarm["active"], alarm["code"], alarm["error"]), (True, 229, None))
        self.assertFalse(alarm["can_pause"])
        item = self.tile(self.cloud([229]))
        self.assertEqual((item["active"], item["code"]), (True, 229))
        self.assertIn("229", item["value"])

    def test_other_active_code_is_shown_instead_of_the_stale_latest_code(self):
        alarm = self.insights().alarms(self.cfg, self.cloud([229]))
        self.assertEqual((alarm["active"], alarm["code"]), (True, 229))

    def test_unproven_cloud_lists_are_ignored(self):
        cases = {
            "other device": (self.cloud(device="emmy-r-2"), None),
            "stale": (self.cloud(age=301), None),
            "future": (self.cloud(age=-120), None),
            "not configured": (self.cloud(configured=False), None),
            "no cloud": (None, None),
            "no identifiers": (self.cloud(), []),
            "other integration": (self.cloud(), [["zha", self.DEVICE]]),
        }
        for name, (cloud, identifiers) in cases.items():
            with self.subTest(name):
                alarm = self.insights(identifiers).alarms(self.cfg, cloud)
                self.assertIsNone(alarm["active"])
                self.assertIn("184 Filterlarm", alarm["error"])
                self.assertNotIn("source", alarm)
                self.assertIn("aktiv status saknas", self.tile(cloud, identifiers=identifiers)["value"])

    def test_identity_failure_keeps_the_ha_view(self):
        insights = self.insights()
        insights.ha.device_identifiers.side_effect = ControlError("nere")
        self.assertIsNone(insights.alarms(self.cfg, self.cloud())["active"])

    def test_ha_status_entity_still_decides(self):
        registry = [*self.registry, {"entity_id": ALARM, "device_id": "pump", "platform": "myuplink"}]
        self.registry = registry
        self.states += [entity(ALARM, "on", self.now)]
        alarm = self.insights().alarms(self.cfg, self.cloud())
        self.assertTrue(alarm["active"])
        self.assertNotIn("source", alarm)

    def test_control_never_uses_the_cloud_list(self):
        # pump_alarm (used by the control loop) is unchanged: only HA entities decide holds.
        self.assertIsNone(self.alarm()["active"])


class DeviceIdentifierTests(unittest.TestCase):
    def test_identifiers_are_validated_and_entity_ids_never_reach_the_template_unchecked(self):
        from controller import HomeAssistant
        ha = HomeAssistant("token")
        ha.request = Mock(return_value=[["myuplink", "emmy-r-1"]])
        self.assertEqual(ha.device_identifiers("number.f370_offset_47011"), [["myuplink", "emmy-r-1"]])
        self.assertIn("device_id('number.f370_offset_47011')", ha.request.call_args.args[1]["template"])
        for bad in ("number.x') }}{{ 1", "", None, "Number.X"):
            with self.subTest(bad=bad), self.assertRaises(ControlError):
                ha.device_identifiers(bad)
        for invalid in ({}, [["myuplink"]], [[1, 2]], "x"):
            ha.request.return_value = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ControlError):
                ha.device_identifiers("number.offset")
