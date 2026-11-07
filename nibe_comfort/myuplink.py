"""Opt-in public myUplink alarm API; never infer reset IDs from alarm codes."""

import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from controller import ControlError


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MyUplink:
    def __init__(self, options):
        self.client_id = options.get("myuplink_client_id", "")
        self.client_secret = options.get("myuplink_client_secret", "")
        self.system_id = options.get("myuplink_system_id", "")
        self.device_id = options.get("myuplink_device_id", "")
        self.lock = threading.Lock()
        self.token = None
        self.expires = 0
        self.cached = None
        self.opener = build_opener(NoRedirects())

    def configured(self):
        values = (self.client_id, self.client_secret, self.system_id, self.device_id)
        if not any(values):
            return False
        if not all(isinstance(v, str) and v.strip() for v in values):
            raise ControlError("Fyll i myUplink client ID, client secret, system-ID och device-ID i tilläggets konfiguration.")
        try:
            self.system_id = str(UUID(self.system_id))
        except ValueError as exc:
            raise ControlError("myUplink system-ID måste vara ett UUID.") from exc
        return True

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

    def reset(self, identifier, events):
        if not isinstance(identifier, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", identifier):
            raise ControlError("Kvittering kräver ett verifierat numeriskt larm-ID; UUID och larmkod godtas inte.")
        if not self.lock.acquire(blocking=False):
            raise ControlError("Ett myUplink-anrop pågår. Ingen ytterligare kvittering skickades.")
        try:
            if not self.configured():
                raise ControlError("myUplink-anslutningen är inte konfigurerad.")
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
