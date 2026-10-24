"""Ingress UI and optional bearer-authenticated local REST API."""

import hmac
import json
import logging
import os
import signal
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from controller import ControlError, Controller, HomeAssistant, temperature
from insights import Insights

LOG = logging.getLogger("nibe_comfort")
STATIC = Path(__file__).parent / "static"


def handler_for(controller, api_token=None):
    insights = Insights(controller.ha, getattr(controller, "data_dir", None))
    if getattr(controller, "preheat_provider", 1) is None:
        controller.preheat_provider = insights.preheat

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, format_string, *args):
            LOG.info("%s %s", self.address_string(), format_string % args)

        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if api_token is None and self.client_address[0] != "172.30.32.2":
                self.respond(403, {"error": "Använd Home Assistants ingress för webbgränssnittet."})
                return False
            if api_token is not None:
                supplied = self.headers.get("Authorization", "").encode()
                if not hmac.compare_digest(supplied, f"Bearer {api_token}".encode()):
                    self.respond(401, {"error": "Giltig Bearer-token krävs."})
                    return False
            # Browsers cannot submit cross-origin settings using a simple form.
            if self.command == "PUT" and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, {"error": "Content-Type application/json krävs."})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/settings":
                    self.respond(200, controller.settings())
                elif path == "/api/test":
                    self.respond(200, controller.test_status())
                elif path == "/api/version":
                    self.respond(200, {"version": os.environ.get("NIBE_COMFORT_VERSION", "dev")})
                elif path == "/api/status":
                    self.respond(200, controller.snapshot())
                elif path == "/api/discovery":
                    self.respond(200, controller.ha.discover())
                elif path == "/api/overview":
                    self.respond(200, insights.overview(controller.settings()))
                elif path == "/api/hotwater":
                    self.respond(200, insights.hot_water(controller.settings()))
                elif path == "/api/history":
                    self.respond(200, insights.history(controller.settings()))
                elif path == "/api/forecast":
                    self.respond(200, insights.forecast(controller.settings()["forecast_entity"]))
                elif path == "/api/plan":
                    self.respond(200, insights.plan(controller.settings(), controller.snapshot(), controller.events.recent()))
                elif path == "/api/learning":
                    self.respond(200, insights.learning(controller.settings(), controller.snapshot(), controller.events.recent()))
                elif path == "/api/events":
                    before = parse_qs(urlsplit(self.path).query).get("before", [None])[0]
                    if before is not None and (not before.isascii() or not before.isdigit() or len(before) > 19
                                               or not 0 < int(before) < 9223372036854775807):
                        self.respond(400, {"error": "before måste vara ett positivt logg-ID."})
                        return
                    self.respond(200, controller.events.recent(None if before is None else int(before)))
                elif path == "/api/entities":
                    entities = []
                    for entity in controller.ha.states():
                        if entity["entity_id"].startswith(("sensor.", "number.", "weather.", "climate.")):
                            attrs = entity.get("attributes", {})
                            current = None
                            temperature_error = None
                            if entity["entity_id"].startswith("climate."):
                                try:
                                    current = temperature(entity)
                                except ControlError as exc:
                                    temperature_error = str(exc)
                            entities.append({
                                "entity_id": entity["entity_id"],
                                "name": attrs.get("friendly_name", entity["entity_id"]),
                                "state": entity.get("state"),
                                "unit": attrs.get("unit_of_measurement", ""),
                                "device_class": attrs.get("device_class", ""),
                                "min": attrs.get("min"), "max": attrs.get("max"), "step": attrs.get("step"),
                                "current_temperature": current, "temperature_error": temperature_error,
                                "setpoint": attrs.get("temperature") if entity["entity_id"].startswith("climate.") else None,
                                "temperature_unit": attrs.get("temperature_unit"),
                                "target_temp_step": attrs.get("target_temp_step"),
                            })
                    self.respond(200, entities)
                elif api_token is None and path in ("/", "/index.html", "/app.js", "/theme.js", "/style.css"):
                    name = "index.html" if path == "/" else path[1:]
                    mime = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
                    self.respond(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
                else:
                    self.respond(404, {"error": "Sökvägen finns inte."})
            except ControlError as exc:
                self.respond(502, {"error": str(exc)})
            except (OSError, sqlite3.Error):
                LOG.exception("Kunde inte läsa tilläggets data")
                self.respond(500, {"error": "Kunde inte läsa tilläggets data. Kontrollera logg och lagringsutrymme."})

        def do_PUT(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            if path == "/api/test":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    action = json.loads(self.rfile.read(length)).get("action") if 0 < length <= 1024 else None
                    if action == "start":
                        controller.start_test()
                    elif action == "stop":
                        controller.stop_test()
                    else:
                        self.respond(400, {"error": "action måste vara start eller stop."})
                        return
                    self.respond(200, controller.test_status())
                except (ValueError, AttributeError, ControlError) as exc:
                    self.respond(400, {"error": str(exc) or "Ogiltig begäran."})
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte spara testläget")
                    self.respond(500, {"error": "Testläget kunde inte sparas på disk."})
                return
            if path != "/api/settings":
                self.respond(404, {"error": "Sökvägen finns inte."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    self.respond(413, {"error": "Konfigurationen måste vara 1–16384 byte."})
                    return
                config = json.loads(self.rfile.read(length))
                controller.save(config)
                self.respond(200, controller.settings())
            except (ValueError, ControlError) as exc:
                self.respond(400, {"error": str(exc)})
            except (OSError, sqlite3.Error):
                LOG.exception("Kunde inte spara konfigurationen")
                self.respond(500, {"error": "Konfigurationen kunde inte sparas på disk."})

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN saknas. Kör som ett Home Assistant-tillägg.")
    options = json.loads(Path("/data/options.json").read_text())
    local_token = options.get("api_token", "")
    if local_token and len(local_token) < 32:
        raise RuntimeError("api_token måste vara minst 32 tecken.")
    controller = Controller(HomeAssistant(token), "/data")
    stop = threading.Event()

    def control_loop():
        while not stop.is_set():
            try:
                controller.tick()
            except ControlError as exc:
                LOG.warning("%s", exc)
            except Exception:
                # Unexpected failures are visible and stop control, never silently retried.
                LOG.exception("Oväntat fel; styrloopen har stoppats")
                with controller.lock:
                    controller.status = {"error_message": "Internt fel. Starta om tillägget och granska loggen.", "message": "Styrloopen stoppad."}
                try:
                    controller.events.add("fatal", "Styrloopen stoppad på grund av internt fel. Kontrollera tilläggsloggen.")
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte spara felet i ändringsloggen")
                return
            stop.wait(60)

    servers = [ThreadingHTTPServer(("0.0.0.0", 8098), handler_for(controller))]
    if local_token:
        servers.append(ThreadingHTTPServer(("0.0.0.0", 8099), handler_for(controller, local_token)))
    for server in servers:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=control_loop, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    LOG.info("NIBE Comfort startad; lokalt API %s", "aktiverat" if local_token else "avstängt")
    stop.wait()
    for server in servers:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
