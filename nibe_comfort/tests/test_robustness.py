import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import handler_for, run_control_loop
from controller import ControlError, Controller, HomeAssistant, control_state, evaluate
from test_controller import config, entity, fixtures


class ManualFireplaceTests(unittest.TestCase):
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
        return self.controller.snapshot()

    def press(self, minute):
        with patch("controller.time.time", return_value=self.now + minute * 60):
            return self.controller.start_manual_fireplace()

    def run_minutes(self, start, stop, local=21, other=21):
        status = None
        for minute in range(start, stop + 1, 5):
            status = self.tick(minute, local, other)
        return status

    def test_one_press_is_active_at_once_and_counts_as_control(self):
        self.run_minutes(0, 60)
        self.assertNotEqual(self.tick(61)["fireplace"]["phase"], "active")
        plan = self.press(61)
        self.assertFalse(plan["already_active"])
        status = self.tick(62)
        self.assertEqual(status["fireplace"]["phase"], "active")
        self.assertTrue(status["fireplace"]["manual"])
        self.assertIn("eldar", status["fireplace"]["message"])
        self.assertEqual(control_state(status, {**self.cfg, "enabled": True}), "Kamin")
        expected = evaluate(self.cfg, self.ha.states.return_value, self.now + 62 * 60, fireplace_active=True,
                            indoor_samples=self.controller.indoor_samples[:-1])
        self.assertEqual(status["contributions"]["fireplace"], expected["contributions"]["fireplace"])
        # The observation took over the press; nothing is left pending.
        self.assertIsNone(self.controller.manual_fireplace)
        self.assertTrue(self.controller.fireplace_state["active"]["manual"])
        self.assertEqual(self.controller.start_manual_fireplace()["already_active"], True)
        self.assertEqual([e["kind"] for e in self.controller.events.recent()][0], "fireplace_manual_start")

    def test_long_warm_day_stays_active_until_room_is_no_warmer(self):
        self.run_minutes(0, 60)
        self.press(62)
        self.run_minutes(65, 90)
        # Fire out after a couple of hours, but the fireplace room stays warmer all day.
        status = self.run_minutes(95, 10 * 60, local=22.5)
        self.assertEqual(status["fireplace"]["phase"], "active")
        self.assertIn("inte längre är varmare", status["fireplace"]["message"])
        status = self.run_minutes(10 * 60 + 5, 10 * 60 + 25, local=21)
        self.assertEqual(status["fireplace"]["phase"], "active")
        status = self.run_minutes(10 * 60 + 30, 11 * 60)
        self.assertNotEqual(status["fireplace"]["phase"], "active")
        episode = self.controller.fireplace_state["episodes"][-1]
        self.assertTrue(episode["complete"] and episode["manual"] and episode["warmed"])
        self.assertGreater(episode["end"] - episode["start"], 9 * 3600)

    def test_press_without_extra_heat_ends_after_three_hours(self):
        self.run_minutes(0, 60)
        self.press(62)
        self.assertEqual(self.run_minutes(65, 60 + 175)["fireplace"]["phase"], "active")
        self.assertNotEqual(self.run_minutes(60 + 180, 60 + 190)["fireplace"]["phase"], "active")
        episode = self.controller.fireplace_state["episodes"][-1]
        self.assertFalse(episode["complete"])
        self.assertFalse(episode["warmed"])

    def test_pending_press_is_excluded_from_learning_and_survives_restart(self):
        plan = self.press(0)
        self.assertIn({"start": plan["requested"], "end": None}, self.controller.fireplace_observations())
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.manual_fireplace, plan["requested"])
        # The finished episode keeps the press out of pump learning as well.
        self.tick(1)
        self.assertIn(None, [row["end"] for row in self.controller.fireplace_observations()])

    def test_press_requires_fireplace_setup_and_is_dropped_when_disabled(self):
        self.controller.save(config())
        with self.assertRaises(ControlError):
            self.controller.start_manual_fireplace()
        self.controller.save(self.cfg)
        self.press(0)
        self.controller.save({**self.cfg, "fireplace_enabled": False})
        self.assertEqual(self.tick(1)["fireplace"]["phase"], "off")
        self.assertIsNone(self.controller.manual_fireplace)

    def test_press_without_room_values_waits_then_is_dropped(self):
        self.press(0)
        entities = [entity("number.offset", 0, self.now, min=-10, max=10, step=1)]
        for minute in (1, 20):
            status = self.controller.fireplace_step(entities, self.now + minute * 60, {})
            self.assertEqual(status["phase"], "active")
            self.assertIn("Väntar på aktuella rumsvärden", status["message"])
        status = self.controller.fireplace_step(entities, self.now + 31 * 60, {})
        self.assertNotEqual(status["phase"], "active")
        self.assertIsNone(self.controller.manual_fireplace)
        self.assertEqual(self.controller.events.recent()[0]["kind"], "fireplace_manual_dropped")

    def test_old_timed_file_is_ignored(self):
        Path(self.temp.name, "fireplace-manual.json").write_text(json.dumps({"start": 1, "until": 2}))
        self.assertIsNone(Controller(self.ha, self.temp.name).manual_fireplace)


class ControlLoopTests(unittest.TestCase):
    def controller(self, tick):
        controller = Mock()
        controller.lock = threading.Lock()
        controller.tick.side_effect = tick
        controller.settings.return_value = {"publish_sensors": True}
        controller.snapshot.side_effect = lambda: controller.status
        return controller

    def test_unexpected_error_notifies_publishes_and_retries(self):
        calls = []

        def tick():
            calls.append(1)
            if len(calls) == 1:
                raise KeyError("bug")
            if len(calls) == 3:
                stop.set()

        stop = threading.Event()
        controller = self.controller(tick)
        with self.assertLogs("nibe_comfort", "ERROR"):
            run_control_loop(controller, stop, interval=0, retries=3, retry_seconds=0)
        self.assertEqual(len(calls), 3)
        title, message = controller.notify.call_args.args[:2]
        self.assertIn("internt fel", title)
        self.assertIn("försök 1 av 3", message)
        published = controller.publish.call_args.args[0]
        self.assertTrue(published["stopped"])
        self.assertEqual(control_state(published, {"enabled": True}), "Stoppad")
        controller.events.add.assert_called_once()

    def test_repeated_errors_stop_the_loop_with_a_final_message(self):
        stop = threading.Event()
        controller = self.controller(KeyError("bug"))
        with self.assertLogs("nibe_comfort", "ERROR"):
            run_control_loop(controller, stop, interval=0, retries=2, retry_seconds=0)
        self.assertEqual(controller.tick.call_count, 3)
        self.assertIn("stoppats efter upprepade", controller.status["error_message"])
        self.assertFalse(stop.is_set())

    def test_failing_home_assistant_does_not_break_the_retry(self):
        stop = threading.Event()
        steps = [KeyError("bug"), None, ControlError("paus")]

        def tick():
            step = steps.pop(0)
            if not steps:
                stop.set()
            if step:
                raise step

        controller = self.controller(tick)
        controller.notify.side_effect = OSError("HA nere")
        with self.assertLogs("nibe_comfort", "ERROR"):
            run_control_loop(controller, stop, interval=0, retries=3, retry_seconds=0)
        self.assertEqual(controller.tick.call_count, 3)


class UnexpectedApiErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.controller, "a" * 32))
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))

    def request(self, path, method="GET", body=None):
        request = Request(f"http://127.0.0.1:{self.server.server_port}{path}", method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_bug_in_a_get_handler_gives_a_readable_500(self):
        with patch.object(self.controller, "snapshot", side_effect=KeyError("bug")), self.assertLogs("nibe_comfort", "ERROR"):
            code, body = self.request("/api/status")
        self.assertEqual(code, 500)
        self.assertIn("Oväntat fel", body["error"])

    def test_bug_in_a_put_handler_gives_a_readable_500(self):
        with patch.object(self.controller, "start_manual_fireplace", side_effect=KeyError("bug")), \
                self.assertLogs("nibe_comfort", "ERROR"):
            code, body = self.request("/api/fireplace", "PUT", {"action": "start"})
        self.assertEqual(code, 500)
        self.assertIn("Oväntat fel", body["error"])

    def test_fireplace_endpoint_validates_actions(self):
        self.assertEqual(self.request("/api/fireplace", "PUT", {"action": "x"})[0], 400)
        self.assertEqual(self.request("/api/fireplace", "PUT", {"action": "stop"})[0], 400)
        self.assertEqual(self.request("/api/fireplace", "PUT", {"action": "start"})[0], 400)
        self.controller.save(config(fireplace_enabled=True, fireplace_rooms=["sensor.living"]))
        code, body = self.request("/api/fireplace", "PUT", {"action": "start"})
        self.assertEqual(code, 200)
        self.assertIsNotNone(body["manual"]["requested"])


if __name__ == "__main__":
    unittest.main()


class HotWaterPriceTests(unittest.TestCase):
    MODE = "select.pump_comfort_mode"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = []
        self.ha.states.side_effect = lambda: fixtures()
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(enabled=True, hot_water_price_enabled=True, hot_water_mode_entity=self.MODE)
        self.controller.save(self.cfg)

    def step(self, current, level=None, kind=None, hold=False, now=10_000.0, options=("Economy", "Normal", "Luxury")):
        mode = {"entity_id": self.MODE, "state": current, "attributes": {"options": list(options)}}
        return self.controller.hot_water_step([mode], now, {"price_level": level}, {"kind": kind}, hold)

    def test_off_by_default_and_validated(self):
        controller = Controller(self.ha, tempfile.mkdtemp(dir=self.temp.name))
        self.assertEqual(controller.hot_water_step([], 0, {}, {}, False), {"enabled": False})
        for bad in ({"hot_water_mode_entity": ""}, {"hot_water_mode_entity": "sensor.x"},
                    {"hot_water_normal_option": "Normal"}, {"hot_water_cheap_option": " "}):
            with self.assertRaises(ControlError):
                self.controller.save({**self.cfg, **bad})

    def test_cheap_price_and_preheat_select_the_cheap_mode(self):
        report = self.step("Economy", level="cheap")
        self.ha.set_select.assert_called_once_with(self.MODE, "Normal")
        self.assertEqual((report["current"], report["reason"]), ("Normal", "billig el"))
        self.assertIn("hot_water_mode", [e["kind"] for e in self.controller.events.recent()])
        self.ha.set_select.reset_mock()
        # At most one change an hour, so a charge can finish.
        report = self.step("Normal", level="expensive", now=10_000.0 + 1800)
        self.ha.set_select.assert_not_called()
        self.assertEqual(report["next_change_at"], 10_000.0 + 3600)
        self.step("Normal", level="expensive", now=10_000.0 + 3600)
        self.ha.set_select.assert_called_once_with(self.MODE, "Economy")
        self.assertEqual(self.step("Normal", kind="preheat", now=99_000.0)["reason"], "förvärmer före dyr el")

    def test_hand_picked_modes_monitoring_and_holds_are_left_alone(self):
        self.assertIn("för hand", self.step("Luxury", level="cheap")["note"])
        self.assertIn("paus", self.step("Economy", level="cheap", hold=True)["note"])
        self.controller.save({**self.cfg, "enabled": False})
        self.assertIn("Övervakning", self.step("Economy", level="cheap")["note"])
        self.ha.set_select.assert_not_called()

    def test_unreadable_or_unknown_modes_are_errors_without_writes(self):
        self.assertIsNotNone(self.step("unavailable", level="cheap")["error"])
        self.assertIn("finns inte", self.step("Economy", level="cheap", options=("Economy", "Luxury"))["error"])
        self.ha.set_select.assert_not_called()
        self.ha.set_select.side_effect = ControlError("timeout")
        self.assertIn("kunde inte ändras", self.step("Economy", level="cheap")["error"])


class NotifyServiceTests(unittest.TestCase):
    SERVICES = [
        {"domain": "light", "services": {"turn_on": {}}},
        {"domain": "notify", "services": {"send_message": {}, "persistent_notification": {}, "notify": {},
                                          "mobile_app_peters_iphone": {}, "mobile_app_ipad": {}, "Bad-Name": {}}},
    ]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = HomeAssistant("token")
        self.calls = []

        def request(path, data=None, timeout=15):
            self.calls.append((path, data))
            return self.SERVICES if path == "/services" else {}

        self.ha.request = request

    def test_lists_pickable_services_with_readable_names_phones_first(self):
        self.assertEqual(self.ha.notify_services(), [
            {"service": "notify.mobile_app_ipad", "name": "Mobilappen · Ipad"},
            {"service": "notify.mobile_app_peters_iphone", "name": "Mobilappen · Peters iphone"},
            {"service": "notify.notify", "name": "Notify"},
        ])

    def test_no_notify_domain_gives_an_empty_list_and_bad_replies_are_errors(self):
        self.ha.request = lambda path, data=None, timeout=15: [{"domain": "light", "services": {}}]
        self.assertEqual(self.ha.notify_services(), [])
        self.ha.request = lambda path, data=None, timeout=15: {"x": 1}
        with self.assertRaises(ControlError):
            self.ha.notify_services()

    def test_test_message_goes_only_to_a_listed_service(self):
        controller = Controller(self.ha, self.temp.name)
        self.assertEqual(controller.test_notify("notify.mobile_app_ipad"), {"sent": "notify.mobile_app_ipad"})
        path, data = self.calls[-1]
        self.assertEqual(path, "/services/notify/mobile_app_ipad")
        self.assertEqual(data["title"], "NIBE Comfort")
        for bad in ("", None, "notify.mobile_app_gone", "light.turn_on", "notify.x/../y"):
            with self.assertRaises(ControlError):
                controller.test_notify(bad)
        self.assertEqual(self.calls[-1][0], "/services")

    def test_api_lists_and_tests_services(self):
        controller = Controller(self.ha, self.temp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(controller, "a" * 32))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))

        def call(path, method="GET", body=None):
            request = Request(f"http://127.0.0.1:{server.server_port}{path}", method=method,
                              data=None if body is None else json.dumps(body).encode(),
                              headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                with exc:
                    return exc.code, json.load(exc)

        code, body = call("/api/notify")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["services"]), 3)
        self.assertEqual(call("/api/notify", "PUT", {"service": "notify.mobile_app_ipad"}), (200, {"sent": "notify.mobile_app_ipad"}))
        self.assertEqual(call("/api/notify", "PUT", {"service": "notify.gone"})[0], 400)
        self.assertEqual(call("/api/notify", "PUT", {"x": 1})[0], 400)


class AuxLimitTests(unittest.TestCase):
    ADD = "select.pump_max_electrical_add"
    OPTIONS = ("0.0 kW", "2.0 kW", "4.60 kW", "7.1 kW")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = []
        self.ha.states.side_effect = lambda: fixtures()
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(enabled=True, target_temperature=21, aux_limit_enabled=True, aux_limit_entity=self.ADD,
                          aux_limit_option="2.0 kW", aux_normal_option="7.1 kW")
        self.controller.save(self.cfg)

    def step(self, current, level=None, kind=None, indoor=21.0, outdoor=0.0, hold=False, now=10_000.0, options=OPTIONS):
        add = {"entity_id": self.ADD, "state": current, "attributes": {"options": list(options)}}
        return self.controller.aux_limit_step([add], now, {"price_level": level, "indoor": indoor, "outdoor": outdoor},
                                              {"kind": kind}, hold)

    def test_off_by_default_and_validated(self):
        controller = Controller(self.ha, tempfile.mkdtemp(dir=self.temp.name))
        self.assertEqual(controller.aux_limit_step([], 0, {}, {}, False), {"enabled": False})
        for bad in ({"aux_limit_entity": ""}, {"aux_limit_entity": "number.x"}, {"aux_limit_option": ""},
                    {"aux_normal_option": "2.0 kW"}, {"aux_limit_max_drop": 0}, {"aux_limit_min_outdoor": 20}):
            with self.assertRaises(ControlError):
                self.controller.save({**self.cfg, **bad})
        # Disabled, the options may stay empty.
        self.controller.save({**self.cfg, "aux_limit_enabled": False, "aux_limit_entity": "", "aux_limit_option": ""})

    def test_expensive_hours_and_coast_limit_cheap_hours_restore_at_once(self):
        report = self.step("7.1 kW", level="expensive")
        self.ha.set_select.assert_called_once_with(self.ADD, "2.0 kW")
        self.assertTrue(report["limited"])
        self.assertTrue(self.controller.aux_limited)
        self.ha.set_select.reset_mock()
        # Lifting the limit is never delayed.
        self.step("2.0 kW", level="normal", now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW")
        self.assertFalse(self.controller.aux_limited)
        self.ha.set_select.reset_mock()
        # Lowering again waits an hour from the last change.
        report = self.step("7.1 kW", kind="coast", now=10_120.0)
        self.ha.set_select.assert_not_called()
        self.assertEqual(report["reason"], "dyr period efter förvärmning")
        self.assertEqual(report["next_change_at"], 10_060.0 + 3600)
        # Preheating wants heat even in an expensive hour.
        self.assertEqual(self.step("7.1 kW", level="expensive", kind="preheat", now=20_000.0)["desired"], "7.1 kW")

    def test_cold_house_or_cold_weather_get_full_power(self):
        report = self.step("7.1 kW", level="expensive", indoor=20.4)
        self.assertEqual(report["desired"], "7.1 kW")
        self.assertIn("kallt inne", report["reason"])
        self.assertIn("kallt inne", self.step("7.1 kW", level="expensive", indoor=None)["reason"])
        self.assertIn("kallt ute", self.step("7.1 kW", level="expensive", outdoor=-12)["reason"])
        self.ha.set_select.assert_not_called()
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.reset_mock()
        # The house cooled while limited: full power again straight away.
        self.step("2.0 kW", level="expensive", indoor=20.4, now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW")

    def test_hand_picked_monitoring_and_holds(self):
        self.assertIn("för hand", self.step("4.60 kW", level="expensive")["note"])
        self.ha.set_select.assert_not_called()
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.reset_mock()
        # A pause lifts our own limit but does not lower anything.
        report = self.step("2.0 kW", level="expensive", hold=True, now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        self.assertEqual(report["current"], "7.1 kW")
        self.ha.set_select.reset_mock()
        self.controller.save({**self.cfg, "enabled": False})
        self.assertIn("Övervakning", self.step("7.1 kW", level="expensive")["note"])
        self.ha.set_select.assert_not_called()

    def test_limit_survives_restart_and_is_lifted_on_errors_and_setting_changes(self):
        self.step("7.1 kW", level="expensive")
        restored = Controller(self.ha, self.temp.name)
        self.assertTrue(restored.aux_limited)
        self.ha.set_select.reset_mock()
        self.ha.set_select.side_effect = ControlError("timeout")
        self.assertIn("kunde inte återställas", restored.lift_aux_limit("test"))
        self.assertTrue(restored.aux_limited)
        self.ha.set_select.side_effect = None
        restored.save({**self.cfg, "aux_limit_enabled": False})
        self.ha.set_select.assert_called_with(self.ADD, "7.1 kW", timeout=60)
        self.assertFalse(restored.aux_limited)
        self.assertIsNone(restored.lift_aux_limit("nothing to do"))

    def test_control_loop_lifts_the_limit_when_a_round_fails(self):
        controller = Mock()
        controller.lock = threading.Lock()
        stop = threading.Event()
        controller.tick.side_effect = lambda: (stop.set(), (_ for _ in ()).throw(ControlError("rum saknas")))
        with self.assertLogs("nibe_comfort", "WARNING"):
            run_control_loop(controller, stop, interval=0)
        controller.lift_aux_limit.assert_called_once_with("styrningen saknar aktuella värden")
