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
from insights import Insights, control_history
from energy import change_comparisons, meter_error
from myuplink import MyUplink

LOG = logging.getLogger("nibe_comfort")
STATIC = Path(__file__).parent / "static"
LOOP_RETRIES = 3
LOOP_RETRY_SECONDS = 300


def run_control_loop(controller, stop, interval=60, retries=LOOP_RETRIES, retry_seconds=LOOP_RETRY_SECONDS):
    """Run control rounds until stopped. An unexpected error is reported to Home Assistant and retried
    a few times; after that the loop stops and says so instead of leaving stale "Styr" sensors."""
    failures = 0
    while not stop.is_set():
        try:
            controller.tick()
            failures = 0
        except ControlError as exc:
            LOG.warning("%s", exc)
            failures = 0
        except Exception:
            failures += 1
            final = failures > retries
            LOG.exception("Oväntat fel i styrloopen (%s av %s)", failures, retries + 1)
            message = ("Styrloopen har stoppats efter upprepade interna fel. Starta om tillägget och granska loggen."
                       if final else f"Internt fel. Försöker igen om {round(retry_seconds / 60)} minuter "
                       f"(försök {failures} av {retries}).")
            with controller.lock:
                controller.status = {"error_message": message, "message": "Styrloopen stoppad." if final else "Styrloopen pausad.",
                                     "stopped": True}
            try:
                controller.events.add("fatal", message)
            except (OSError, sqlite3.Error):
                LOG.exception("Kunde inte spara felet i ändringsloggen")
            try:
                config = controller.settings()
                controller.notify("NIBE Comfort: internt fel", f"{message} Pumpen behåller sin nuvarande kurvförskjutning.",
                                  config, create=True)
                controller.publish(controller.snapshot(), config)
            except Exception:  # Home Assistant may be the problem; the log above already has the error
                LOG.exception("Kunde inte rapportera felet till Home Assistant")
            if final:
                return
            stop.wait(retry_seconds)
            continue
        stop.wait(interval)


def read_json(handler, limit=1024):
    """The request body as JSON, or {} when it is empty or larger than the limit."""
    length = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(length)) if 0 < length <= limit else {}


def handler_for(controller, api_token=None, insights=None, myuplink=None):
    if myuplink is None:
        myuplink = MyUplink({})
    if insights is None:
        insights = Insights(controller.ha, getattr(controller, "data_dir", None))
    insights.fireplace_provider = controller.fireplace_observations
    if getattr(controller, "preheat_provider", 1) is None:
        controller.preheat_provider = insights.control_preheat

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
                elif path == "/api/alarms":
                    self.respond(200, insights.alarms(controller.settings()))
                elif path == "/api/myuplink/alarms":
                    self.respond(200, myuplink.report())
                elif path == "/api/myuplink/discovery":
                    self.respond(200, myuplink.discovery())
                elif path == "/api/hotwater":
                    self.respond(200, insights.hot_water(controller.settings()))
                elif path == "/api/auxiliary":
                    self.respond(200, insights.auxiliary(controller.settings()))
                elif path == "/api/sources":
                    self.respond(200, insights.sources(controller.settings()))
                elif path == "/api/energy":
                    config = controller.settings()
                    report = insights.energy(config)
                    try:
                        events = controller.events.history(report["start"], report["end"])
                        report["changes"] = [
                            {**change, "timestamp": change["outcome_at"] if change["status"] == "confirmed" else change["timestamp"]}
                            for change in control_history(events, config, report["start"], report["end"])
                            if report["start"] <= (change.get("outcome_at") if change["status"] == "confirmed" else change["timestamp"]) <= report["end"]
                        ]
                        report["settings_changes"] = [
                            {"timestamp": e["timestamp"], "kind": "settings", "status": "saved",
                             "label": e["message"]}
                            for e in events if e["kind"] == "settings_saved"
                            and report["start"] <= e["timestamp"] <= report["end"]
                        ]
                        report["comparisons"] = change_comparisons(report["hours"], report["changes"] + report["settings_changes"])
                        report["changes_error"] = None
                    except (sqlite3.Error, OSError):
                        LOG.exception("Kunde inte läsa ändringsloggen för elförbrukningen")
                        report.update(changes=[], settings_changes=[], comparisons=[],
                                      changes_error="Ändringsloggen kunde inte läsas. Förbrukningen visas utan styrmarkeringar.")
                    self.respond(200, report)
                elif path == "/api/history":
                    config = controller.settings()
                    history = insights.history(config)
                    try:
                        history["changes"] = control_history(
                            controller.events.history(history["start"], history["end"]),
                            config, history["start"], history["end"])
                        history["changes_error"] = None
                    except (sqlite3.Error, OSError):
                        LOG.exception("Kunde inte läsa ändringsloggen för temperaturhistoriken")
                        history["changes"] = []
                        history["changes_error"] = "Ändringsloggen kunde inte läsas. Temperaturhistoriken visas utan styrmarkeringar."
                    self.respond(200, history)
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
                                "state_class": attrs.get("state_class", ""),
                                "energy_meter_error": meter_error(entity),
                                "min": attrs.get("min"), "max": attrs.get("max"), "step": attrs.get("step"),
                                "current_temperature": current, "temperature_error": temperature_error,
                                "setpoint": attrs.get("temperature") if entity["entity_id"].startswith("climate.") else None,
                                "temperature_unit": attrs.get("temperature_unit"),
                                "target_temp_step": attrs.get("target_temp_step"),
                            })
                    self.respond(200, entities)
                elif path == "/api/areas":
                    self.respond(200, controller.ha.entity_areas(controller.settings()["room_sensors"]))
                elif api_token is None and path in ("/", "/index.html", "/app.js", "/theme.js", "/style.css"):
                    name = "index.html" if path == "/" else path[1:]
                    mime = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
                    self.respond(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
                else:
                    self.respond(404, {"error": "Sökvägen finns inte."})
            except ControlError as exc:
                if path == "/api/myuplink/discovery":
                    LOG.warning("myUplink-upptäckt misslyckades: %s", exc)
                    self.respond(422, {"error": str(exc)})
                else:
                    self.respond(502, {"error": str(exc)})
            except (OSError, sqlite3.Error):
                LOG.exception("Kunde inte läsa tilläggets data")
                self.respond(500, {"error": "Kunde inte läsa tilläggets data. Kontrollera logg och lagringsutrymme."})
            except Exception:
                # An unexpected bug must give the page a readable error, not a dropped connection.
                LOG.exception("Oväntat fel vid %s", path)
                self.respond(500, {"error": "Oväntat fel i tillägget. Se tilläggsloggen."})

        def do_PUT(self):
            try:
                self.handle_put()
            except Exception:
                LOG.exception("Oväntat fel vid %s", urlsplit(self.path).path)
                self.respond(500, {"error": "Oväntat fel i tillägget. Se tilläggsloggen."})

        def handle_put(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            if path == "/api/fireplace":
                try:
                    body = read_json(self)
                    if body.get("action") == "start":
                        plan = controller.start_manual_fireplace(body.get("hours"))
                    elif body.get("action") == "stop":
                        plan = controller.stop_manual_fireplace()
                    else:
                        raise ControlError("action måste vara start eller stop.")
                    self.respond(200, {"manual": plan})
                except (ValueError, AttributeError, ControlError) as exc:
                    self.respond(400, {"error": str(exc) or "Ogiltig begäran."})
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte spara eldningen")
                    self.respond(500, {"error": "Eldningen kunde inte sparas på disk."})
                return
            if path == "/api/myuplink/discovery":
                try:
                    body = read_json(self)
                    if not isinstance(body, dict) or set(body) != {"system_id", "device_id"}:
                        raise ControlError("Ange system_id och device_id från upptäckten.")
                    self.respond(200, myuplink.select_device(body["system_id"], body["device_id"], controller.events))
                except (ValueError, ControlError) as exc:
                    self.respond(400, {"error": str(exc) or "Ogiltigt pumpval."})
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte spara myUplink-pumpval")
                    self.respond(500, {"error": "Pumpvalet kunde inte sparas eller loggas. Uppdatera vyn och kontrollera valet."})
                return
            if path == "/api/myuplink/alarms":
                try:
                    body = read_json(self)
                    if (not isinstance(body, dict) or set(body) != {"action", "id", "confirmed", "system_id", "device_id"}
                            or body["action"] != "reset" or body["confirmed"] is not True
                            or not isinstance(body["system_id"], str) or not body["system_id"]
                            or not isinstance(body["device_id"], str) or not body["device_id"]):
                        raise ControlError("Manuell kvittering kräver action=reset, larm-ID, system_id, device_id och confirmed=true.")
                    self.respond(200, myuplink.reset(body["id"], controller.events, body["system_id"], body["device_id"]))
                except (ValueError, ControlError) as exc:
                    self.respond(400, {"error": str(exc) or "Ogiltig begäran."})
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte logga myUplink-kvittering")
                    self.respond(500, {"error": "Kvitteringen kunde inte loggas. Begäran kan ha skickats; kontrollera pumpen innan ett nytt försök."})
                return
            if path == "/api/alarms":
                try:
                    if read_json(self) != {"action": "pause_thermostats"}:
                        raise ControlError("Endast pause_thermostats stöds. Larm återställs inte automatiskt.")
                    self.respond(200, controller.pause_alarm_thermostats())
                except (ValueError, AttributeError, ControlError) as exc:
                    self.respond(400, {"error": str(exc) or "Ogiltig begäran."})
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte spara larmåtgärden")
                    self.respond(500, {"error": "Larmåtgärden kunde inte sparas. Kontrollera loggen."})
                return
            if path == "/api/test":
                try:
                    action = read_json(self).get("action")
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
            if path == "/api/vacation":
                try:
                    body = read_json(self)
                    action = body.get("action")
                    if action == "schedule":
                        plan = controller.schedule_vacation(body.get("start"), body.get("end"))
                    elif action == "cancel":
                        plan = controller.cancel_vacation()
                    else:
                        self.respond(400, {"error": "action måste vara schedule eller cancel."})
                        return
                    self.respond(200, {"vacation": plan})
                except (ValueError, AttributeError, ControlError) as exc:
                    self.respond(400, {"error": str(exc) or "Ogiltig begäran."})
                except (OSError, sqlite3.Error):
                    LOG.exception("Kunde inte spara semesterperioden")
                    self.respond(500, {"error": "Semesterperioden kunde inte sparas på disk."})
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

    insights = Insights(controller.ha, controller.data_dir)
    myuplink = MyUplink(options, controller.data_dir)
    servers = [ThreadingHTTPServer(("0.0.0.0", 8098), handler_for(controller, insights=insights, myuplink=myuplink))]
    if local_token:
        servers.append(ThreadingHTTPServer(("0.0.0.0", 8099), handler_for(controller, local_token, insights, myuplink)))
    for server in servers:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=run_control_loop, args=(controller, stop), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    LOG.info("NIBE Comfort startad; lokalt API %s", "aktiverat" if local_token else "avstängt")
    stop.wait()
    for server in servers:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
