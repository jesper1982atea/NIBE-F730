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

    def test_final_failure_continues_pending_heater_recovery(self):
        controller = self.controller(KeyError("bug"))
        controller.aux_limited = True
        controller.aux_limit_step.side_effect = lambda *args: setattr(controller, "aux_limited", False) or {"error": None}
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with self.assertLogs("nibe_comfort", "ERROR"):
            run_control_loop(controller, stop, interval=0, retries=0)
        controller.aux_limit_step.assert_called_once()
        self.assertIn(unittest.mock.call(300), stop.wait.call_args_list)


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
        self.ha.integration_registry.return_value = [
            {"entity_id": "number.offset", "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
            {"entity_id": self.MODE, "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
        ]
        self.ha.states.side_effect = lambda: fixtures()
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(enabled=True, price_entity="sensor.price", hot_water_price_enabled=True, hot_water_mode_entity=self.MODE)
        self.controller.save(self.cfg)

    def step(self, current, level=None, kind=None, hold=False, now=10_000.0, options=("Economy", "Normal", "Luxury")):
        mode = entity(self.MODE, current, now, options=list(options))
        return self.controller.hot_water_step([mode], now, {"price_level": level, "price": 1.0}, {"kind": kind}, hold)

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
        self.assertEqual((report["current"], report["reason"]), ("Economy", "billig el"))
        self.assertIn("väntar", report["note"])
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

    def test_timeout_and_restart_preserve_hourly_interval(self):
        self.ha.set_select.side_effect = ControlError("timeout")
        self.step("Economy", level="cheap")
        restored = Controller(self.ha, self.temp.name)
        self.ha.set_select.reset_mock(side_effect=True)
        mode = entity(self.MODE, "Economy", 10_060, options=["Economy", "Normal"])
        report = restored.hot_water_step([mode], 10_060, {"price_level": "cheap", "price": 1},
                                         {"kind": None}, False)
        self.assertEqual(report["next_change_at"], 13_600)
        self.ha.set_select.assert_not_called()

    def test_missing_price_and_foreign_pump_do_not_write(self):
        mode = entity(self.MODE, "Economy", 10_000, options=["Economy", "Normal"])
        report = self.controller.hot_water_step([mode], 10_000, {"price_level": "cheap", "price": None},
                                                {"kind": None}, False)
        self.assertIn("Elpriset", report["note"])
        self.controller.peer_cache = (time.time(), {"number.offset": []})
        self.controller.myuplink_peers = {}
        report = self.controller.hot_water_step([mode], 10_000, {"price_level": "cheap", "price": 1},
                                                {"kind": None}, False)
        self.assertIn("samma pump", report["error"])
        self.ha.set_select.assert_not_called()


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
    CHARGE = "sensor.pump_hot_water_charging_bt6"
    TOP = "sensor.pump_hot_water_top_bt7"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.integration_registry.return_value = [
            {"entity_id": "number.offset", "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
            {"entity_id": self.ADD, "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
            {"entity_id": self.CHARGE, "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
            {"entity_id": self.TOP, "device_id": "pump", "platform": "myuplink", "config_entry_id": "entry"},
        ]
        self.ha.states.side_effect = lambda: fixtures()
        self.controller = Controller(self.ha, self.temp.name)
        self.cfg = config(enabled=True, target_temperature=21, price_entity="sensor.price",
                          outdoor_entity="sensor.outdoor", aux_limit_enabled=True, aux_limit_entity=self.ADD,
                          aux_limit_option="2.0 kW", aux_normal_option="7.1 kW")
        self.controller.save(self.cfg)

    def step(self, current, level=None, kind=None, indoor=21.0, outdoor=0.0, hold=False, now=10_000.0, options=OPTIONS,
             price=1, rank=None, charge=48.0, top=52.0):
        add = entity(self.ADD, current, now, options=list(options))
        water = [entity(key, value, now, unit_of_measurement="°C")
                 for key, value in ((self.CHARGE, charge), (self.TOP, top)) if value is not None]
        return self.controller.aux_limit_step([add, *water], now, {"price_level": level, "price": price, "price_rank": rank,
                                                           "indoor": indoor, "outdoor": outdoor},
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
        self.assertFalse(report["limited"], "A request is not a confirmed setting")
        self.assertIn("väntar", report["note"])
        self.assertTrue(self.controller.aux_limited)
        self.ha.set_select.reset_mock()
        # A cheap price lifts the limit at once.
        self.step("2.0 kW", level="cheap", now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        self.assertTrue(self.controller.aux_limited)
        self.ha.set_select.reset_mock()
        self.step("7.1 kW", level="normal", now=10_120.0)
        self.assertTrue(self.controller.aux_limited, "An accepted restore is not readback")
        self.step("7.1 kW", level="normal", now=10_181.0)
        self.assertFalse(self.controller.aux_limited)
        # Lowering again waits an hour from the last change.
        report = self.step("7.1 kW", kind="coast", now=10_240.0)
        self.ha.set_select.assert_not_called()
        self.assertEqual(report["reason"], "dyr period efter förvärmning")
        self.assertEqual(report["next_change_at"], 10_000.0 + 3600)
        # Preheating wants heat even in an expensive hour.
        self.assertEqual(self.step("7.1 kW", level="expensive", kind="preheat", now=20_000.0)["desired"], "7.1 kW")

    def test_little_hot_water_lifts_and_blocks_the_limit(self):
        # Too little hot water never starts a limit, with a margin above the guard limits.
        for kwargs, text in (({"charge": 39.0}, "BT6 39.0"), ({"top": 46.0}, "BT7 46.0"),
                             ({"charge": None}, "BT6"), ({"top": None}, "BT7")):
            with self.subTest(**kwargs):
                report = self.step("7.1 kW", level="expensive", **kwargs)
                self.assertEqual(report["desired"], "7.1 kW")
                self.assertIn(text, report["reason"])
                self.ha.set_select.assert_not_called()
        # Enough hot water: the limit is set; tank values are reported.
        report = self.step("7.1 kW", level="expensive", charge=41.0, top=48.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "2.0 kW")
        self.assertEqual(report["hot_water"], {"charge": 41.0, "top": 48.0, "error": None})
        self.ha.set_select.reset_mock()
        # Inside the margin an active limit stays; below the guard limit it is lifted at once.
        self.step("2.0 kW", level="expensive", charge=38.5, now=10_060.0)
        self.ha.set_select.assert_not_called()
        report = self.step("2.0 kW", level="expensive", charge=37.5, now=10_120.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        self.assertIn("Lite varmvatten kvar", report["reason"])

    def test_hot_water_guard_can_be_turned_off_and_allows_zero_kw(self):
        self.controller.save({**self.cfg, "aux_limit_option": "0.0 kW"})
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.assert_called_once_with(self.ADD, "0.0 kW")
        with self.assertRaises(ControlError) as caught:
            self.controller.save({**self.cfg, "aux_limit_option": "0.0 kW", "aux_hot_water_guard": False})
        self.assertIn("varmvattenvakten", str(caught.exception))
        for bad in ({"aux_hot_water_min_charge": 10}, {"aux_hot_water_min_top": 80}, {"hot_water_precharge_minutes": -1},
                    {"aux_hot_water_guard": "ja"}):
            with self.assertRaises(ControlError):
                self.controller.save({**self.cfg, **bad})

    def test_without_guard_the_tank_is_not_read(self):
        self.controller.save({**self.cfg, "aux_hot_water_guard": False})
        report = self.step("7.1 kW", level="expensive", charge=None, top=None)
        self.ha.set_select.assert_called_once_with(self.ADD, "2.0 kW")
        self.assertIsNone(report["hot_water"])

    def test_old_settings_get_the_guard_on(self):
        path = Path(self.temp.name) / "settings.json"
        saved = json.loads(path.read_text())
        for key in ("aux_hot_water_guard", "aux_hot_water_min_charge", "aux_hot_water_min_top", "hot_water_precharge_minutes"):
            saved.pop(key)
        path.write_text(json.dumps(saved))
        loaded = Controller(self.ha, self.temp.name).settings()
        self.assertTrue(loaded["aux_hot_water_guard"])
        self.assertEqual((loaded["aux_hot_water_min_charge"], loaded["aux_hot_water_min_top"],
                          loaded["hot_water_precharge_minutes"]), (38.0, 45.0, 120.0))

    def limit(self):
        self.step("7.1 kW", level="expensive")
        self.step("2.0 kW", level="expensive", now=10_060.0)
        self.assertEqual(self.controller.aux_restore["phase"], "limited")
        self.ha.set_select.reset_mock()

    def test_short_loss_of_the_select_keeps_the_limit(self):
        self.limit()
        report = self.step("unavailable", level="expensive", now=10_120.0)
        self.assertIn("högst 10 minuter", report["error"])
        self.assertEqual(self.controller.aux_restore["phase"], "limited")
        report = self.step("2.0 kW", level="expensive", now=10_420.0)
        self.assertIsNone(report["error"])
        self.assertIsNone(self.controller.aux_outage_since, "Readable again: the next gap gets a full grace")
        self.step("unavailable", level="expensive", now=10_480.0)
        self.step("unavailable", level="expensive", now=11_000.0)
        self.assertEqual(self.controller.aux_restore["phase"], "limited")
        self.ha.set_select.assert_not_called()

    def test_long_loss_of_the_select_lifts_the_limit(self):
        self.limit()
        self.step("unavailable", level="expensive", now=10_120.0)
        report = self.step("unavailable", level="expensive", now=10_720.0)
        self.assertIn("Kontrollera inställningen", report["error"])
        self.assertEqual(self.controller.aux_restore["phase"], "restoring")
        self.step("2.0 kW", level="expensive", now=10_780.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_cheap_hour_after_a_short_gap_still_restores(self):
        self.limit()
        self.step("unavailable", level="expensive", now=10_120.0)
        self.step("2.0 kW", level="cheap", now=10_300.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_outage_round_keeps_the_limit_for_ten_minutes(self):
        self.limit()
        now = 10_120.0
        add = entity(self.ADD, "unavailable", now, options=list(self.OPTIONS))
        self.assertIn("förblir begränsad", self.controller.lift_aux_limit_after_outage("pumpens värden saknas", now, [add]))
        self.assertEqual(self.controller.aux_restore["phase"], "limited")
        self.controller.lift_aux_limit_after_outage("pumpens värden saknas", now + 599, [add])
        self.assertEqual(self.controller.aux_restore["phase"], "limited")
        self.controller.lift_aux_limit_after_outage("pumpens värden saknas", now + 600, [add])
        self.assertEqual(self.controller.aux_restore["phase"], "restoring")
        self.ha.set_select.assert_not_called()
        add = entity(self.ADD, "2.0 kW", now + 660, options=list(self.OPTIONS))
        self.controller.lift_aux_limit_after_outage("pumpens värden saknas", now + 660, [add])
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_outage_after_restart_restores_at_once(self):
        self.limit()
        restored = Controller(self.ha, self.temp.name)
        self.assertTrue(restored.aux_restart_restore)
        add = entity(self.ADD, "2.0 kW", 10_120.0, options=list(self.OPTIONS))
        restored.lift_aux_limit_after_outage("pumpens värden saknas", 10_120.0, [add])
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_tick_during_pump_outage_keeps_the_limit(self):
        from controller import OUTAGE_MESSAGE
        self.controller.store_aux_limited({"limited": True, "entity_id": self.ADD, "normal": "7.1 kW",
                                           "restricted": "2.0 kW", "phase": "limited", "started_at": time.time(),
                                           "restore_attempted_at": None})
        rows = fixtures()
        next(e for e in rows if e["entity_id"] == "number.offset")["state"] = "unavailable"
        rows.append(entity(self.ADD, "unavailable", time.time(), options=list(self.OPTIONS)))
        self.ha.states.side_effect = lambda: rows
        self.controller.tick()
        self.assertEqual(self.controller.status["message"], OUTAGE_MESSAGE)
        self.assertEqual(self.controller.aux_restore["phase"], "limited")
        self.controller.aux_outage_since -= 600
        self.controller.tick()
        self.assertEqual(self.controller.aux_restore["phase"], "restoring")
        self.ha.set_select.assert_not_called()

    def test_a_limit_in_force_is_held_through_short_price_dips(self):
        self.limit()
        # A normal quarter right after the start keeps the limit instead of toggling it through the cloud.
        report = self.step("2.0 kW", level=None, now=10_900.0)
        self.ha.set_select.assert_not_called()
        self.assertIn("minst 45 minuter", report["reason"])
        self.assertEqual(report["desired"], "2.0 kW")
        # After the hold, an absolute price just under the expensive limit still keeps it ...
        report = self.step("2.0 kW", level=None, now=12_800.0, price=1.9)
        self.ha.set_select.assert_not_called()
        self.assertIn("tydligt lägre pris", report["reason"])
        # ... and a clearly lower price lifts it.
        self.step("2.0 kW", level=None, now=12_860.0, price=1.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_relative_prices_release_below_the_margin(self):
        self.controller.save({**self.cfg, "price_mode": "relative", "price_high_percent": 75})
        self.limit()
        self.step("2.0 kW", level=None, now=12_800.0, rank=0.62)
        self.ha.set_select.assert_not_called()
        self.step("2.0 kW", level=None, now=12_860.0, rank=None)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        self.ha.set_select.reset_mock()
        self.setUp()
        self.controller.save({**self.cfg, "price_mode": "relative", "price_high_percent": 75})
        self.limit()
        self.step("2.0 kW", level=None, now=12_800.0, rank=0.55)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_hold_never_overrides_cheap_prices_preheating_or_comfort(self):
        for kwargs in ({"level": "cheap"}, {"kind": "preheat"}, {"indoor": 20.0}, {"outdoor": -15.0}):
            with self.subTest(**kwargs):
                self.setUp()
                self.limit()
                self.step("2.0 kW", now=10_300.0, **kwargs)
                self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_cold_house_or_cold_weather_get_full_power(self):
        report = self.step("7.1 kW", level="expensive", indoor=20.4)
        self.assertEqual(report["desired"], "7.1 kW")
        self.assertIn("kallt inne", report["reason"])
        self.assertIn("kallt inne", self.step("7.1 kW", level="expensive", indoor=None)["reason"])
        self.assertIn("kallt ute", self.step("7.1 kW", level="expensive", outdoor=-12)["reason"])
        self.assertIn("Utetemperaturen saknas", self.step("7.1 kW", level="expensive", outdoor=None)["reason"])
        self.ha.set_select.assert_not_called()
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.reset_mock()
        # The house cooled while limited: full power again straight away, once clearly below the limit.
        self.step("2.0 kW", level="expensive", indoor=20.2, now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)

    def test_cold_house_lift_needs_a_margin_below_the_limit(self):
        # Limit 21 - 0.5 = 20.5; a limit in force is lifted only below 20.3, so one sensor step at the
        # limit does not cost a cloud write.
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.reset_mock()
        for now, indoor in ((10_060.0, 20.4), (10_120.0, 20.3)):
            report = self.step("2.0 kW", level="expensive", indoor=indoor, now=now)
            self.ha.set_select.assert_not_called()
            self.assertEqual(report["desired"], "2.0 kW")
        self.step("2.0 kW", level="expensive", indoor=20.2, now=10_180.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        sent = [e for e in self.controller.events.recent() if e.get("kind") == "aux_limit_restore_sent"]
        self.assertIn("för kallt inne", sent[0]["message"])
        self.assertNotIn("behöver återställning", sent[0]["message"])

    def test_new_limit_still_needs_the_house_at_the_limit(self):
        report = self.step("7.1 kW", level="expensive", indoor=20.4)
        self.ha.set_select.assert_not_called()
        self.assertIn("kallt inne", report["reason"])
        self.step("7.1 kW", level="expensive", indoor=20.5, now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "2.0 kW")

    def test_limit_timeout_keeps_a_persistent_restore_target(self):
        self.ha.set_select.side_effect = ControlError("timeout")
        report = self.step("7.1 kW", level="expensive")
        self.assertIn("kunde inte ändras", report["error"])
        restored = Controller(self.ha, self.temp.name)
        self.assertTrue(restored.aux_limited)
        self.assertEqual(restored.aux_restore["normal"], "7.1 kW")
        self.assertEqual(restored.aux_restore["phase"], "restoring")

    def test_hand_picked_monitoring_and_holds(self):
        self.assertIn("för hand", self.step("4.60 kW", level="expensive")["note"])
        self.ha.set_select.assert_not_called()
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.reset_mock()
        # A pause lifts our own limit but does not lower anything.
        report = self.step("2.0 kW", level="expensive", hold=True, now=10_060.0)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        self.assertEqual(report["current"], "2.0 kW")
        self.assertIn("väntar", report["error"])
        self.step("7.1 kW", level="normal", now=10_181.0)
        self.ha.set_select.reset_mock()
        self.controller.save({**self.cfg, "enabled": False})
        self.assertIn("Övervakning", self.step("7.1 kW", level="expensive", now=10_240.0)["note"])
        self.ha.set_select.assert_not_called()

    def test_limit_survives_restart_and_is_lifted_on_errors_and_setting_changes(self):
        self.step("7.1 kW", level="expensive")
        restored = Controller(self.ha, self.temp.name)
        self.assertTrue(restored.aux_limited)
        self.ha.set_select.reset_mock()
        state = entity(self.ADD, "2.0 kW", 10_060, options=list(self.OPTIONS))
        self.ha.set_select.side_effect = ControlError("timeout")
        self.assertIn("kunde inte bekräftas", restored.lift_aux_limit("test", entities=[state], now=10_060))
        self.assertTrue(restored.aux_limited)
        self.ha.set_select.side_effect = None
        result = restored.aux_limit_step([state], 10_120, {"price_level": "expensive", "indoor": 21, "outdoor": 0},
                                         {"kind": None}, False)
        self.assertIn("väntar", result["error"])
        self.assertTrue(restored.aux_limited)
        self.ha.set_select.assert_called_once()
        updated = entity(self.ADD, "7.1 kW", 10_200, options=list(self.OPTIONS))
        restored.aux_limit_step([updated], 10_200, {"price_level": "normal", "indoor": 21, "outdoor": 0},
                                {"kind": None}, False)
        self.assertFalse(restored.aux_limited)
        self.assertIsNone(restored.lift_aux_limit("nothing to do"))

    def test_restart_recovers_before_limiting_again_and_preserves_manual_changes(self):
        self.step("7.1 kW", level="expensive")
        restored = Controller(self.ha, self.temp.name)
        self.ha.set_select.reset_mock()
        manual = entity(self.ADD, "4.60 kW", 10_060, options=list(self.OPTIONS))
        report = restored.aux_limit_step([manual], 10_060, {"price_level": "expensive", "indoor": 21, "outdoor": 0},
                                         {"kind": None}, False)
        self.assertIn("kontrollera", report["error"].lower())
        self.ha.set_select.assert_not_called()
        limited = entity(self.ADD, "2.0 kW", 10_120, options=list(self.OPTIONS))
        restored.aux_limit_step([limited], 10_120, {"price_level": "expensive", "indoor": 21, "outdoor": 0},
                                {"kind": None}, False)
        self.ha.set_select.assert_called_once_with(self.ADD, "7.1 kW", timeout=60)
        self.assertTrue(restored.aux_limited)

    def test_old_unchanged_normal_reading_does_not_confirm_restore(self):
        self.step("7.1 kW", level="expensive")
        self.step("2.0 kW", level="cheap", now=10_060)
        stale = entity(self.ADD, "7.1 kW", 10_000, options=list(self.OPTIONS))
        self.controller.aux_limit_step([stale], 10_300, {"price_level": "normal", "indoor": 21, "outdoor": 0},
                                       {"kind": None}, False)
        self.assertTrue(self.controller.aux_limited)

    def test_manual_recovery_requires_wait_matching_plan_and_normal_readback(self):
        self.step("7.1 kW", level="expensive")
        self.step("2.0 kW", level="cheap", now=10_060)
        self.ha.set_select.reset_mock()
        self.ha.states.side_effect = None
        self.ha.states.return_value = [entity(self.ADD, "7.1 kW", 10_200, options=list(self.OPTIONS))]
        with patch("controller.time.time", return_value=10_100):
            with self.assertRaises(ControlError):
                self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")
        with patch("controller.time.time", return_value=10_200):
            with self.assertRaises(ControlError):
                self.controller.acknowledge_aux_recovery(self.ADD, "2.0 kW")
            self.ha.states.return_value = [entity(self.ADD, "unavailable", 10_200, options=list(self.OPTIONS))]
            with self.assertRaises(ControlError):
                self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")
            self.ha.states.return_value = [entity(self.ADD, "2.0 kW", 10_200, options=list(self.OPTIONS))]
            with self.assertRaises(ControlError):
                self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")
            self.ha.states.return_value = [entity(self.ADD, "7.1 kW", 10_200, options=list(self.OPTIONS))]
            self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")
            with self.assertRaises(ControlError):
                self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")
        self.ha.set_select.assert_not_called()
        self.assertFalse(self.controller.settings()["aux_limit_enabled"])
        restored = Controller(self.ha, self.temp.name)
        self.assertFalse(restored.aux_limited)
        self.assertFalse(restored.settings()["aux_limit_enabled"])
        self.assertEqual(restored.aux_written, 10_000)

    def test_manual_recovery_keeps_plan_if_saving_disabled_setting_fails(self):
        self.step("7.1 kW", level="expensive")
        self.step("2.0 kW", level="cheap", now=10_060)
        self.ha.states.side_effect = None
        self.ha.states.return_value = [entity(self.ADD, "7.1 kW", 10_200, options=list(self.OPTIONS))]
        with patch("controller.time.time", return_value=10_200), patch("controller.atomic_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")
        self.assertTrue(Controller(self.ha, self.temp.name).aux_limited)

    def test_recovery_remains_visible_if_device_disappears(self):
        self.step("7.1 kW", level="expensive")
        self.controller.myuplink_peers = {}
        self.step("2.0 kW", level="cheap", now=10_060)
        self.assertEqual(self.controller.snapshot()["aux_recovery"]["phase"], "restoring")
        with patch("controller.time.time", return_value=10_300):
            with self.assertRaises(ControlError):
                self.controller.acknowledge_aux_recovery(self.ADD, "7.1 kW")

    def test_unknown_select_never_starts_limiting_or_clears_recovery(self):
        self.step("7.1 kW", level="expensive")
        self.ha.set_select.reset_mock()
        report = self.step("unavailable", level="normal", now=10_060)
        self.assertIn("kan inte läsas", report["error"])
        self.assertTrue(self.controller.aux_limited)
        self.ha.set_select.assert_not_called()

    def test_control_loop_lifts_the_limit_when_a_round_fails(self):
        controller = Mock()
        controller.lock = threading.Lock()
        stop = threading.Event()
        controller.tick.side_effect = lambda: (stop.set(), (_ for _ in ()).throw(ControlError("rum saknas")))
        with self.assertLogs("nibe_comfort", "WARNING"):
            run_control_loop(controller, stop, interval=0)
        controller.lift_aux_limit_after_outage.assert_called_once_with("styrningen saknar aktuella värden")
        controller.lift_aux_limit.assert_not_called()
