"""Opt-in public myUplink alarm API; never infer reset IDs from alarm codes."""

import json
import hashlib
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID
from pathlib import Path

from controller import ControlError, atomic_json


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MyUplink:
    def __init__(self, options, data_dir=None):
        self.client_id = options.get("myuplink_client_id", "")
        self.client_secret = options.get("myuplink_client_secret", "")
        self.system_id = options.get("myuplink_system_id", "")
        self.device_id = options.get("myuplink_device_id", "")
        self.manual_selection = bool(self.system_id or self.device_id)
        self.selection_path = Path(data_dir) / "myuplink-selection.json" if data_dir else None
        self.identity = hashlib.sha256(json.dumps([self.client_id, self.client_secret]).encode()).hexdigest()
        if self.selection_path and self.selection_path.exists() and not self.system_id and not self.device_id:
            saved = json.loads(self.selection_path.read_text())
            if not isinstance(saved, dict):
                raise ControlError("Ogiltigt sparat myUplink-pumpval.")
            if saved.get("identity") == self.identity:
                self.system_id = saved.get("system_id", "")
                self.device_id = saved.get("device_id", "")
        self.lock = threading.Lock()
        self.token = None
        self.expires = 0
        self.cached = None
        self.discovery_cache = None
        self.opener = build_opener(NoRedirects())

    def credentials_configured(self):
        values = (self.client_id, self.client_secret)
        if not any(values):
            if self.system_id or self.device_id:
                raise ControlError("Ange myUplink client ID och client secret för sparat pumpval.")
            return False
        if not all(isinstance(v, str) and v.strip() for v in values):
            raise ControlError("Fyll i myUplink client ID och client secret i tilläggets konfiguration.")
        return True

    def configured(self):
        if not self.credentials_configured():
            return False
        if not self.system_id and not self.device_id:
            return False
        if not all(isinstance(v, str) and v.strip() for v in (self.system_id, self.device_id)):
            raise ControlError("Välj anläggning och pump i appen, eller ange både system-ID och device-ID.")
        try:
            self.system_id = str(UUID(self.system_id))
        except ValueError as exc:
            raise ControlError("myUplink system-ID måste vara ett UUID.") from exc
        return True

    def discover_devices(self):
        token = self.access_token()
        choices = []
        seen = set()
        count = 0
        for page in range(1, 11):
            result = self.request(f"/v2/systems/me?page={page}&itemsPerPage=100", token=token)
            if (not isinstance(result, dict) or type(result.get("numItems")) is not int
                    or not 0 <= result["numItems"] <= 1000):
                raise ControlError("myUplink lämnade en ogiltig anläggningslista.")
            systems = result.get("systems")
            if systems is None and result["numItems"] == 0:
                systems = []
            if not isinstance(systems, list) or len(systems) > 100:
                raise ControlError("myUplink lämnade en ogiltig anläggningslista.")
            for system in systems:
                if not isinstance(system, dict) or not isinstance(system.get("systemId"), str):
                    raise ControlError("myUplink lämnade ett ogiltigt system-ID.")
                try:
                    system_id = str(UUID(system["systemId"]))
                except ValueError as exc:
                    raise ControlError("myUplink lämnade ett ogiltigt system-ID.") from exc
                if system_id in seen:
                    raise ControlError("myUplink lämnade dubbla anläggningar.")
                seen.add(system_id)
                devices = system.get("devices")
                if devices is None:
                    details = self.request(f"/v2/systems/{system_id}", token=token)
                    if not isinstance(details, dict) or details.get("systemId") != system["systemId"]:
                        raise ControlError("myUplink lämnade fel anläggningsdetaljer.")
                    devices = details.get("devices")
                if not isinstance(devices, list) or len(devices) > 100:
                    raise ControlError("myUplink lämnade en ogiltig enhetslista.")
                ids = set()
                for device in devices:
                    if (not isinstance(device, dict) or not isinstance(device.get("id"), str)
                            or not device["id"].strip() or device["id"] in ids):
                        raise ControlError("myUplink lämnade ett ogiltigt eller dubbelt device-ID.")
                    ids.add(device["id"])
                    product = device.get("product")
                    name = product.get("name") if isinstance(product, dict) else None
                    choices.append({"system_id": system_id, "device_id": device["id"],
                                    "system_name": system.get("name") if isinstance(system.get("name"), str) else system_id,
                                    "device_name": name if isinstance(name, str) else
                                    device["deviceType"] if isinstance(device.get("deviceType"), str) and device["deviceType"] else device["id"]})
                    if len(choices) > 1000:
                        raise ControlError("För många myUplink-enheter; upptäckten stoppades.")
            count += len(systems)
            if count == result["numItems"]:
                return choices
            if not systems or count > result["numItems"]:
                raise ControlError("myUplink-anläggningslistan är ofullständig.")
        raise ControlError("myUplink-anläggningslistan är för stor.")

    def discovery(self):
        with self.lock:
            if not self.credentials_configured():
                return {"credentials_configured": False, "choices": [], "checked_at": None}
            if not self.discovery_cache or time.time() - self.discovery_cache["checked_at"] >= 300:
                self.discovery_cache = None
                choices = self.discover_devices()
                self.discovery_cache = {"credentials_configured": True, "choices": choices, "checked_at": time.time()}
            return {**self.discovery_cache, "system_id": self.system_id, "device_id": self.device_id,
                    "manual_selection": self.manual_selection}

    def select_device(self, system_id, device_id, events):
        with self.lock:
            if not self.credentials_configured():
                raise ControlError("Ange client ID och client secret först.")
            if not self.selection_path:
                raise ControlError("Beständig lagring för pumpval saknas.")
            if self.manual_selection:
                raise ControlError("Töm manuellt system-ID och device-ID i tilläggets konfiguration och starta om innan du sparar ett pumpval i appen.")
            choices = self.discover_devices()
            if not any(c["system_id"] == system_id and c["device_id"] == device_id for c in choices):
                raise ControlError("Pumpvalet finns inte i den aktuella myUplink-anläggningslistan.")
            atomic_json(self.selection_path, {"identity": self.identity, "system_id": system_id, "device_id": device_id})
            self.system_id, self.device_id = system_id, device_id
            self.cached = self.discovery_cache = None
            events.add("myuplink_device_selected", "Användaren valde molnanläggning och enhet för larmläsning.",
                       system_id=system_id, device_id=device_id)
            return {"system_id": system_id, "device_id": device_id}

    def request(self, path, data=None, token=None):
        headers = {"Accept": "application/json", "Accept-Language": "sv-SE"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if path == "/oauth/token" else "application/json"
        req = Request("https://api.myuplink.com" + path, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ControlError("myUplink-svaret är för stort.")
                return json.loads(raw)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401:
                self.token = None
            raise ControlError(f"myUplink svarade med HTTP {status}. Kontrollera anslutning och READSYSTEM/WRITESYSTEM-behörighet. Begäran upprepas inte automatiskt.") from exc
        except (URLError, OSError, ValueError) as exc:
            raise ControlError("myUplink-anropet kunde inte bekräftas. Vid kvittering kan pumpen ändå ha fått begäran; kontrollera larmstatus innan ett nytt försök.") from exc

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        body = urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                          "client_secret": self.client_secret, "scope": "READSYSTEM WRITESYSTEM"}).encode()
        result = self.request("/oauth/token", body)
        if (not isinstance(result, dict) or not isinstance(result.get("access_token"), str)
                or not result["access_token"] or not isinstance(result.get("expires_in"), (int, float))
                or isinstance(result["expires_in"], bool) or not 60 < result["expires_in"] <= 86400):
            raise ControlError("myUplink lämnade ett ogiltigt tokensvar.")
        self.token = result["access_token"]
        self.expires = time.time() + result["expires_in"] - 60
        return self.token

    def active(self):
        token = self.access_token()
        alarms = []
        for page in range(1, 11):
            result = self.request(f"/v2/systems/{self.system_id}/notifications/active?page={page}&itemsPerPage=100", token=token)
            if (not isinstance(result, dict) or type(result.get("numItems")) is not int
                    or not 0 <= result["numItems"] <= 1000):
                raise ControlError("myUplink lämnade en ogiltig larmlista.")
            notifications = result.get("notifications")
            if notifications is None and result["numItems"] == 0:
                notifications = []
            if not isinstance(notifications, list) or len(notifications) > 100:
                raise ControlError("myUplink lämnade en ogiltig larmlista.")
            for alarm in notifications:
                if (not isinstance(alarm, dict) or not isinstance(alarm.get("id"), str)
                        or not alarm["id"] or type(alarm.get("alarmNumber")) is not int
                        or not isinstance(alarm.get("deviceId"), str)
                        or not isinstance(alarm.get("status"), str)):
                    raise ControlError("myUplink lämnade ett ogiltigt larm.")
                if alarm["deviceId"] != self.device_id:
                    continue
                identifier = alarm["id"]
                numeric = re.fullmatch(r"[1-9][0-9]{0,9}", identifier) is not None and int(identifier) <= 2147483647
                can_reset = numeric and alarm["alarmNumber"] == 229 and alarm["status"] == "Active"
                reason = (None if can_reset else
                          "API:t ger UUID eller annat inkompatibelt larm-ID; reset kräver heltal. Larmkoden används aldrig som ersättning."
                          if not numeric else "Endast ett aktivt larm 229 stöds för manuell kvittering här.")
                alarms.append({"id": identifier, "code": alarm["alarmNumber"], "status": alarm["status"],
                               "title": alarm.get("header") if isinstance(alarm.get("header"), str) else "Pumplarm",
                               "can_reset": can_reset, "blocked_reason": reason})
            if page * 100 >= result["numItems"]:
                if len({a["id"] for a in alarms}) != len(alarms):
                    raise ControlError("myUplink lämnade dubbla larm-ID:n.")
                return alarms
            if not notifications:
                raise ControlError("myUplink-larmlistan är ofullständig.")
        raise ControlError("myUplink-larmlistan överstiger 1000 poster; ingen kvittering tillåts.")

    def report(self):
        with self.lock:
            if not self.configured():
                return {"configured": False, "alarms": [], "checked_at": None}
            if self.cached and time.time() - self.cached["checked_at"] < 60:
                return self.cached
            self.cached = None
            alarms = self.active()
            self.cached = {"configured": True, "system_id": self.system_id, "device_id": self.device_id,
                           "alarms": alarms, "checked_at": time.time()}
            return self.cached

    def reset(self, identifier, events, expected_system=None, expected_device=None):
        if not isinstance(identifier, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", identifier):
            raise ControlError("Kvittering kräver ett verifierat numeriskt larm-ID; UUID och larmkod godtas inte.")
        if not self.lock.acquire(blocking=False):
            raise ControlError("Ett myUplink-anrop pågår. Ingen ytterligare kvittering skickades.")
        try:
            if not self.configured():
                raise ControlError("myUplink-anslutningen är inte konfigurerad.")
            if ((expected_system is not None and expected_system != self.system_id)
                    or (expected_device is not None and expected_device != self.device_id)):
                raise ControlError("Molnpumpvalet har ändrats. Uppdatera larmvyn innan kvittering.")
            if any(e["kind"] == "alarm_reset_attempt"
                   and e["details"].get("system_id") == self.system_id
                   and e["details"].get("notification_id") == identifier
                   for e in events.history(time.time() - 120, time.time())):
                raise ControlError("En kvittering för detta larm har redan begärts de senaste två minuterna. Kontrollera pumpen innan ett nytt försök.")
            self.cached = None
            alarm = next((a for a in self.active() if a["id"] == identifier), None)
            if not alarm or not alarm["can_reset"]:
                raise ControlError("Larmet kan inte kvitteras: nytt aktivt larm 229 med verifierat ID krävs.")
            events.add("alarm_reset_attempt", "Användaren begär manuell larmkvittering via myUplink.",
                       system_id=self.system_id, device_id=self.device_id, notification_id=identifier, code=229)
            result = self.request(f"/v2/systems/{self.system_id}/notifications/{identifier}/reset",
                                  b"{}", self.access_token())
            if not isinstance(result, dict) or type(result.get("status")) is not int or result["status"] != 200:
                raise ControlError("myUplink bekräftade inte en lyckad enhetsbegäran. Kontrollera pumpen; ingen automatisk återkvittering görs.")
            events.add("alarm_reset_sent", "myUplink accepterade kvitteringsbegäran; kontrollera pumpens larmstatus.",
                       notification_id=identifier, code=229)
            return {"message": "myUplink accepterade begäran. Detta bevisar inte att pumpens larm är släckt; uppdatera larmvyn och kontrollera pumpen.",
                    "confirmed_cleared": False}
        finally:
            self.lock.release()
