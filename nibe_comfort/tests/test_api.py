import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import handler_for
from controller import ControlError, Controller
from insights import Insights
from test_controller import config, fixtures


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.states.side_effect = lambda: fixtures()
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)
        self.token = "a" * 32
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.controller, self.token))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, method="GET", body=None, token=True, content_type="application/json"):
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.url + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_fireplace_settings_and_read_only_observation_status(self):
        cfg = config(fireplace_enabled=True, fireplace_rooms=["sensor.living"])
        code, saved = self.request("/api/settings", "PUT", cfg)
        self.assertEqual(code, 200)
        self.assertTrue(saved["fireplace_enabled"])
        self.assertFalse(saved["enabled"])
        self.controller.tick()
        code, status = self.request("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(status["fireplace"]["phase"], "learning")
        self.assertEqual(status["fireplace"]["rooms"], ["sensor.living"])
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_ingress_and_local_api_share_insights_with_controller(self):
        insights = Insights(self.ha, self.temp.name)
        self.controller.preheat_provider = None
        handler_for(self.controller, insights=insights)
        shared = handler_for(self.controller, self.token, insights)
        self.assertIs(self.controller.preheat_provider.__self__, insights)
        shared_server = ThreadingHTTPServer(("127.0.0.1", 0), shared)
        shared_server.daemon_threads = True
        thread = threading.Thread(target=shared_server.serve_forever, daemon=True)
        thread.start()
        try:
            insights.cached("registry", 300, lambda: [{"entity_id": "sensor.shared", "platform": "test"}])
            req = Request(f"http://127.0.0.1:{shared_server.server_port}/api/overview",
                          headers={"Authorization": f"Bearer {self.token}"})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.ha.integration_registry.assert_not_called()
        finally:
            shared_server.shutdown()
            shared_server.server_close()
            thread.join()

    def test_ingress_serves_static_files_including_theme(self):
        handler = handler_for(self.controller, None)
        ingress = type("IngressHandler", (handler,), {"address_string": lambda self: "172.30.32.2"})
        original_setup = handler.setup

        def setup(this):
            original_setup(this)
            this.client_address = ("172.30.32.2", this.client_address[1])

        ingress.setup = setup
        server = ThreadingHTTPServer(("127.0.0.1", 0), ingress)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/theme.js", timeout=3) as response:
                self.assertIn("text/javascript", response.headers["Content-Type"])
                self.assertIn(b"prefers-color-scheme", response.read())
            with urlopen(base + "/", timeout=3) as response:
                html = response.read()
            self.assertIn(b'src="./theme.js"', html)
            self.assertLess(html.index(b"theme.js"), html.index(b"<body>"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_authentication_required_for_reads_and_writes(self):
        self.assertEqual(self.request("/api/settings", token=False)[0], 401)
        self.assertEqual(self.request("/api/settings", method="PUT", body=config(), token=False)[0], 401)

    def test_plan_is_read_only_and_reports_missing_sources(self):
        self.ha.request.return_value = []
        status, plan = self.request("/api/plan")
        self.assertEqual(status, 200)
        self.assertIn("Ingen Tibber", plan["prices"]["error"])
        self.assertEqual(plan["offset"]["planned"], [])
        self.ha.set_offset.assert_not_called()

    def test_version_comes_from_build(self):
        with patch.dict(os.environ, {"NIBE_COMFORT_VERSION": "9.9.9"}):
            status, data = self.request("/api/version")
        self.assertEqual((status, data), (200, {"version": "9.9.9"}))

    def test_pump_test_start_stop_and_validation(self):
        self.controller.save(config())
        self.assertIsNone(self.request("/api/test")[1]["test"])
        self.assertEqual(self.request("/api/test", "PUT", {"action": "x"})[0], 400)
        self.assertEqual(self.request("/api/test", "PUT", {"action": "start"}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("/api/test", "PUT", {"action": "start"}, token=False)[0], 401)
        self.ha.set_offset.assert_not_called()
        status, data = self.request("/api/test", "PUT", {"action": "start"})
        self.assertEqual((status, data["test"]["phase"], data["test"]["value"]), (200, "running", 1))
        self.ha.set_offset.assert_called_once_with("number.offset", 1)
        self.assertEqual(self.request("/api/test", "PUT", {"action": "start"})[0], 400)
        status, data = self.request("/api/test", "PUT", {"action": "stop"})
        self.assertEqual(status, 200)
        self.assertEqual(data["test"]["result"], "unconfirmed")

    def test_learning_is_read_only_and_wires_preheat_provider(self):
        self.ha.request.return_value = []
        self.controller.save(config())
        status, data = self.request("/api/learning")
        self.assertEqual(status, 200)
        self.assertEqual(data["model"]["status"], "learning")
        self.assertFalse(data["preheat"]["enabled"])
        self.assertIsNotNone(self.controller.preheat_provider)
        self.ha.set_offset.assert_not_called()

    def test_hot_water_is_read_only_and_explains_missing_sensors(self):
        status, data = self.request("/api/hotwater")
        self.assertEqual(status, 200)
        self.assertIn("varmvattengivare", data["error"])
        self.assertIsNone(data["level"])
        self.ha.set_offset.assert_not_called()

    def test_settings_round_trip(self):
        status, original = self.request("/api/settings")
        self.assertEqual(status, 200)
        original["target_temperature"] = 22
        self.assertEqual(self.request("/api/settings", "PUT", original)[0], 200)
        self.assertEqual(self.request("/api/settings")[1]["target_temperature"], 22)

    def test_history_includes_durable_control_events_without_writes(self):
        self.controller.save(config())
        self.ha.request.return_value = []
        request = self.controller.events.add("write_attempt", "request", entity_id="number.offset",
                                             previous=2, value=3, contributions={"room": 1, "price": 0, "weather": 0})
        self.controller.events.add("write_observed", "confirmed", request_id=request)
        status, data = self.request("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(data["changes"][0]["status"], "confirmed")
        self.assertIn("series", data)
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_history_log_failure_preserves_temperature_history_with_visible_error(self):
        self.controller.save(config())
        self.ha.request.return_value = []
        with patch.object(self.controller.events, "history", side_effect=OSError("Disk failure")), self.assertLogs("nibe_comfort", level="ERROR"):
            status, data = self.request("/api/history")
        self.assertEqual(status, 200)
        self.assertIn("series", data)
        self.assertEqual(data["changes"], [])
        self.assertIn("kunde inte läsas", data["changes_error"])
    def test_bad_json_shape_and_content_type_rejected(self):
        self.assertEqual(self.request("/api/settings", "PUT", {})[0], 400)
        self.assertEqual(self.request("/api/settings", "PUT", config(), content_type="text/plain")[0], 415)

    def test_priority_settings_and_status_round_trip(self):
        cfg = config(priority_sensors=["sensor.living"], target_temperature=22)
        self.assertEqual(self.request("/api/settings", "PUT", cfg)[0], 200)
        self.controller.tick()
        status, data = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual((data["indoor"], data["house_indoor"]), (20, 21))
        self.assertEqual(self.request("/api/settings")[1]["priority_sensors"], ["sensor.living"])
        cfg["priority_sensors"] = ["sensor.missing"]
        self.assertEqual(self.request("/api/settings", "PUT", cfg)[0], 400)
        self.assertEqual(self.controller.settings()["priority_sensors"], ["sensor.living"])

    def test_activation_preflight_failure_is_not_saved(self):
        self.ha.states.side_effect = ControlError("Ingen kontakt")
        status, body = self.request("/api/settings", "PUT", config(enabled=True))
        self.assertEqual(status, 400)
        self.assertIn("Ingen kontakt", body["error"])
        self.assertFalse(self.controller.settings()["enabled"])

    def test_entities_are_filtered_and_no_token_is_returned(self):
        status, entities = self.request("/api/entities")
        self.assertEqual(status, 200)
        self.assertIn("number.offset", [item["entity_id"] for item in entities])
        self.assertNotIn("Authorization", json.dumps(entities))
        self.assertNotIn("last_updated", entities[0])

    def test_area_endpoint_maps_only_selected_rooms_and_requires_auth(self):
        cfg = config(room_sensors=["sensor.living"])
        self.controller.save(cfg)
        expected = {"sensor.living": {"area_id": "living", "area_name": "Vardagsrum"}}
        self.ha.entity_areas.return_value = expected
        self.assertEqual(self.request("/api/areas"), (200, expected))
        self.ha.entity_areas.assert_called_once_with(["sensor.living"])
        self.assertEqual(self.request("/api/areas", token=False)[0], 401)

    def test_area_endpoint_surfaces_home_assistant_lookup_failure(self):
        self.ha.entity_areas.side_effect = ControlError("Områdesuppslag misslyckades")
        status, body = self.request("/api/areas")
        self.assertEqual(status, 502)
        self.assertIn("Områdesuppslag misslyckades", body["error"])

    def test_ha_failure_has_explicit_response(self):
        self.ha.states.side_effect = ControlError("Ingen kontakt")
        status, body = self.request("/api/entities")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "Ingen kontakt")

    def test_discovery_is_read_only_and_authenticated(self):
        result = {"offset_entity": "number.pump", "outdoor_entity": "sensor.bt1", "message": "NIBE hittad"}
        self.ha.discover.return_value = result
        self.assertEqual(self.request("/api/discovery"), (200, result))
        self.assertFalse(self.controller.settings()["offset_entity"])
        self.ha.set_offset.assert_not_called()
        self.assertEqual(self.request("/api/discovery", token=False)[0], 401)

    def test_discovery_failure_is_visible(self):
        self.ha.discover.side_effect = ControlError("Upptäckten misslyckades")
        status, body = self.request("/api/discovery")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "Upptäckten misslyckades")

    def test_events_are_authenticated_and_paginated(self):
        self.controller.save(config())
        status, events = self.request("/api/events")
        self.assertEqual(status, 200)
        self.assertIn("settings_saved", [item["kind"] for item in events])
        self.assertEqual(self.request("/api/events", token=False)[0], 401)
        self.assertEqual(self.request("/api/events?before=not-an-id")[0], 400)
        self.assertEqual(self.request("/api/events?before=1"), (200, []))

    def test_climate_entitites_include_measured_temperature_for_room_picker(self):
        self.ha.states.side_effect = None
        self.ha.states.return_value = [{
            "entity_id": "climate.lk_allrum_thermostat", "state": "heat",
            "attributes": {"current_temperature": 22.2, "temperature": 29, "temperature_unit": "°C"},
        }]
        status, entities = self.request("/api/entities")
        self.assertEqual(status, 200)
        self.assertEqual(entities[0]["current_temperature"], 22.2)
        self.assertIsNone(entities[0]["temperature_error"])
        self.assertEqual(entities[0]["setpoint"], 29)
        self.assertEqual(entities[0]["temperature_unit"], "°C")

    def test_thermostat_configuration_round_trip_and_preflight(self):
        cfg = config(room_sensors=["climate.room"], nearest_thermostat="climate.room",
                     thermostat_control_enabled=True)
        self.assertEqual(self.request("/api/settings", "PUT", cfg)[0], 200)
        saved = self.request("/api/settings")[1]
        self.assertTrue(saved["thermostat_control_enabled"])
        self.assertEqual(saved["nearest_thermostat"], "climate.room")
        self.ha.set_thermostat.assert_not_called()
        cfg["enabled"] = True
        self.assertEqual(self.request("/api/settings", "PUT", cfg)[0], 400)
        self.assertFalse(self.request("/api/settings")[1]["enabled"])
        cfg.update(enabled=False, nearest_thermostat="climate.missing")
        self.assertEqual(self.request("/api/settings", "PUT", cfg)[0], 400)

    def test_local_api_does_not_serve_ui_or_arbitrary_paths(self):
        self.assertEqual(self.request("/")[0], 404)
        self.assertEqual(self.request("/../controller.py")[0], 404)
        self.assertEqual(self.request("/api/services")[0], 404)

    def test_ingress_rejects_non_supervisor_clients(self):
        self.server.RequestHandlerClass = handler_for(self.controller)
        self.assertEqual(self.request("/api/settings")[0], 403)


if __name__ == "__main__":
    unittest.main()
