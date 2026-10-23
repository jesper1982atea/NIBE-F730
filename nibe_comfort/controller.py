"""Bounded room-temperature control through Home Assistant entities."""

import copy
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULTS = {
    "enabled": False,
    "target_temperature": 21.0,
    "room_sensors": [],
    "priority_sensors": [],
    "offset_entity": "",
    "outdoor_entity": "",
    "price_entity": "",
    "forecast_entity": "",
    "base_offset": 0.0,
    "min_offset": -3.0,
    "max_offset": 3.0,
    "room_gain": 1.5,
    "deadband": 0.3,
    "weather_gain": 0.0,
    "weather_reference": 0.0,
    "price_low": 0.5,
    "price_high": 2.0,
    "cheap_boost": 0.0,
    "expensive_reduction": 0.0,
    "max_step": 1.0,
    "min_interval_minutes": 30.0,
    "max_age_minutes": 180.0,
    "preheat_enabled": False,
    "preheat_degrees": 0.5,
}
LIMITS = {
    "target_temperature": (10, 30),
    "base_offset": (-10, 10),
    "min_offset": (-10, 10),
    "max_offset": (-10, 10),
    "room_gain": (0, 5),
    "deadband": (0, 2),
    "weather_gain": (0, 0.5),
    "weather_reference": (-30, 30),
    "price_low": (-10000, 10000),
    "price_high": (-10000, 10000),
    "cheap_boost": (0, 3),
    "expensive_reduction": (0, 3),
    "max_step": (0.1, 2),
    "min_interval_minutes": (10, 1440),
    "max_age_minutes": (5, 1440),
    "preheat_degrees": (0.1, 1.5),
}


TEST_SECONDS = 600
RESTORE_CONFIRM_SECONDS = 900


class ControlError(Exception):
    pass


def number(value, label):
    if isinstance(value, bool):
        raise ControlError(f"{label}: ett ändligt tal krävs.")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ControlError(f"{label}: ett ändligt tal krävs.") from exc
    if not math.isfinite(result):
        raise ControlError(f"{label}: ett ändligt tal krävs.")
    return result


def validate(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULTS):
        raise ControlError("Konfigurationen innehåller saknade eller okända fält.")
    result = copy.deepcopy(config)
    for key in ("enabled", "preheat_enabled"):
        if type(result[key]) is not bool:
            raise ControlError(f"{key} måste vara boolesk.")
    for key, (low, high) in LIMITS.items():
        result[key] = number(result[key], key)
        if not low <= result[key] <= high:
            raise ControlError(f"{key} måste vara mellan {low} och {high}.")
    rooms = result["room_sensors"]
    if not isinstance(rooms, list) or len(rooms) > 20:
        raise ControlError("Välj högst 20 rumsgivare.")
    if any(not isinstance(item, str) or not item.startswith(("sensor.", "climate.")) for item in rooms):
        raise ControlError("Rumsgivare måste vara sensor- eller climate-entiteter.")
    if len(set(rooms)) != len(rooms):
        raise ControlError("Samma rumsgivare får inte väljas flera gånger.")
    priority = result["priority_sensors"]
    if not isinstance(priority, list) or len(priority) > 2 or any(not isinstance(key, str) or key not in rooms for key in priority):
        raise ControlError("Välj högst två prioriterade givare bland dina rumsgivare.")
    if len(set(priority)) != len(priority):
        raise ControlError("Samma prioriterade givare får inte väljas flera gånger.")
    for key, prefixes in (
        ("offset_entity", ("number.",)),
        ("outdoor_entity", ("sensor.", "weather.")),
        ("price_entity", ("sensor.",)),
        ("forecast_entity", ("weather.",)),
    ):
        value = result[key]
        if not isinstance(value, str) or (value and not value.startswith(prefixes)):
            raise ControlError(f"{key}: fel entitetstyp.")
    if not result["min_offset"] <= result["base_offset"] <= result["max_offset"]:
        raise ControlError("Basförskjutningen måste ligga inom säkerhetsgränserna.")
    if result["price_low"] >= result["price_high"]:
        raise ControlError("Lågprisgränsen måste vara lägre än högprisgränsen.")
    if result["weather_gain"] and not result["outdoor_entity"]:
        raise ControlError("Välj utomhusgivare för väderpåverkan.")
    if (result["cheap_boost"] or result["expensive_reduction"]) and not result["price_entity"]:
        raise ControlError("Välj elprisgivare för elprisstyrning.")
    if result["preheat_enabled"] and not result["price_entity"]:
        raise ControlError("Välj elprisgivare (Tibber) för förvärmning.")
    if result["enabled"] and (not rooms or not result["offset_entity"]):
        raise ControlError("Välj rumsgivare och kurvförskjutning innan aktivering.")
    return result


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class HomeAssistant:
    def __init__(self, token, url="http://supervisor/core/api"):
        self.token = token
        self.url = url
        self._temperature_unit = None

    def request(self, path, data=None):
        request = Request(
            self.url + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            raise ControlError(f"Home Assistant svarade med HTTP {exc.code}. Kontrollera integration och behörighet.") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise ControlError("Kunde inte kommunicera med Home Assistant.") from exc

    def states(self):
        states = self.request("/states")
        if not isinstance(states, list) or any(
            not isinstance(entity, dict)
            or not isinstance(entity.get("entity_id"), str)
            or not isinstance(entity.get("state"), str)
            or not isinstance(entity.get("attributes"), dict)
            for entity in states
        ):
            raise ControlError("Home Assistant returnerade ett ogiltigt entitetssvar.")
        if any(entity["entity_id"].startswith("climate.") for entity in states):
            if self._temperature_unit is None:
                config = self.request("/config")
                self._temperature_unit = config.get("unit_system", {}).get("temperature")
                if self._temperature_unit not in ("°C", "°F"):
                    raise ControlError("Home Assistants temperaturenhet saknas eller stöds inte.")
            for entity in states:
                if entity["entity_id"].startswith("climate."):
                    entity["attributes"]["temperature_unit"] = self._temperature_unit
        return states

    def set_offset(self, entity_id, value):
        self.request("/services/number/set_value", {"entity_id": entity_id, "value": value})

    def integration_registry(self):
        template = (
            "{% set ns = namespace(items=[]) %}"
            "{% for platform in ['myuplink', 'tibber', 'smhi', 'met'] %}"
            "{% for eid in integration_entities(platform) %}"
            "{% set ns.items = ns.items + "
            "[{'entity_id': eid, 'device_id': device_id(eid), 'platform': platform}] %}"
            "{% endfor %}{% endfor %}"
            "{% for climate in states.climate %}"
            "{% set did = device_id(climate.entity_id) %}"
            "{% if did and did not in ns.items | map(attribute='device_id') | list %}"
            "{% for eid in device_entities(did) %}"
            "{% set ns.items = ns.items + "
            "[{'entity_id': eid, 'device_id': did, 'platform': 'climate_device'}] %}"
            "{% endfor %}{% endif %}{% endfor %}{{ ns.items | to_json }}"
        )
        registry = self.request("/template", {"template": template})
        validate_registry(registry)
        return registry

    def discover(self):
        from insights import discover_tibber
        registry = self.integration_registry()
        states = self.states()
        result = discover_nibe(states, registry)
        result.update(discover_tibber(states, registry))
        return result


def validate_registry(registry):
    if not isinstance(registry, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("entity_id"), str)
        or "device_id" not in item
        or not isinstance(item.get("device_id"), (str, type(None)))
        for item in registry
    ):
        raise ControlError("Home Assistant returnerade ogiltig myUplink-enhetsinformation.")


def discover_nibe(entities, registry):
    validate_registry(registry)
    registry = [item for item in registry if item.get("platform", "myuplink") == "myuplink"]
    states = {entity["entity_id"]: entity for entity in entities}
    offsets = [
        item for item in registry
        if item["device_id"] and item["entity_id"] in states
        and item["entity_id"].startswith("number.")
        and item["entity_id"].endswith("_heating_offset_climate_system_1")
    ]
    offsets = list({item["entity_id"]: item for item in offsets}.values())
    if len(offsets) != 1:
        return {
            "offset_entity": "", "outdoor_entity": "",
            "message": (
                "Flera NIBE-pumpar hittades. Välj pump manuellt."
                if offsets else
                "Ingen entydig NIBE-kurvförskjutning hittades. Kontrollera myUplink eller välj manuellt."
            ),
        }
    offset = offsets[0]
    outdoor = [
        item["entity_id"] for item in registry
        if item["device_id"] == offset["device_id"] and item["entity_id"] in states
        and item["entity_id"].startswith("sensor.")
        and item["entity_id"].endswith("_current_outd_temp_bt1")
    ]
    outdoor = list(set(outdoor))
    return {
        "offset_entity": offset["entity_id"],
        "outdoor_entity": outdoor[0] if len(outdoor) == 1 else "",
        "message": (
            "NIBE hittad via myUplink: klimatsystem 1 och pumpens utegivare BT1."
            if len(outdoor) == 1 else
            "NIBE hittad, men utegivaren BT1 kunde inte identifieras entydigt. Välj utegivare manuellt."
        ),
    }


def entity_age(entity, now):
    stamp = entity.get("last_reported") or entity.get("last_updated")
    try:
        return now - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError) as exc:
        raise ControlError(f"{entity['entity_id']}: tidsstämpel saknas eller är ogiltig.") from exc


def fresh_entity(states, entity_id, max_age, now, peers=()):
    """Return a valid, recently reported entity.

    Some integrations (e.g. myUplink) only write a state when its value changes, so an
    unchanged setting keeps an old timestamp. ``peers`` are other entities of the same
    device: a recent report from any of them proves the integration is still delivering
    data, so the unchanged value is still current.
    """
    entity = states.get(entity_id)
    if not entity or entity.get("state") in ("unknown", "unavailable", None):
        raise ControlError(f"{entity_id or 'Entitet'} saknas eller är otillgänglig.")
    age = entity_age(entity, now)
    if age < -60:
        raise ControlError(f"{entity_id}: mätvärdet har en tidsstämpel i framtiden.")
    if age <= max_age * 60:
        return entity
    for peer_id in peers:
        peer = states.get(peer_id)
        if peer_id == entity_id or not peer or peer.get("state") in ("unknown", "unavailable", None):
            continue
        try:
            peer_age = entity_age(peer, now)
        except ControlError:
            continue
        if -60 <= peer_age <= max_age * 60:
            return entity
    if peers:
        raise ControlError(f"{entity_id}: varken värdet eller något annat värde från samma enhet har rapporterats på {max_age:g} minuter.")
    raise ControlError(f"{entity_id}: mätvärdet är för gammalt eller har fel tidsstämpel.")


def device_peers(registry):
    """Map each entity to the other entities on the same device, per integration."""
    groups = {}
    for item in registry:
        if item["device_id"]:
            groups.setdefault((item.get("platform"), item["device_id"]), set()).add(item["entity_id"])
    peers = {}
    for members in groups.values():
        for entity_id in members:
            peers.setdefault(entity_id, set()).update(members - {entity_id})
    return {entity_id: sorted(ids) for entity_id, ids in peers.items()}


def temperature(entity):
    attrs = entity.get("attributes", {})
    is_weather = entity["entity_id"].startswith("weather.")
    is_climate = entity["entity_id"].startswith("climate.")
    raw = attrs.get("current_temperature") if is_climate else attrs.get("temperature") if is_weather else entity["state"]
    value = number(raw, entity["entity_id"])
    unit = attrs.get("temperature_unit") if is_weather or is_climate else attrs.get("unit_of_measurement")
    if unit == "°F":
        value = (value - 32) * 5 / 9
    elif unit != "°C":
        raise ControlError(f"{entity['entity_id']}: temperaturen måste anges i °C eller °F.")
    if not -60 <= value <= 70:
        raise ControlError(f"{entity['entity_id']}: orimlig temperatur.")
    return value


def next_step(config, current, desired, minimum, step):
    """Closest allowed pump value towards desired: on the entity's step grid, within bounds and max_step."""
    low = max(config["min_offset"], current - config["max_step"])
    high = min(config["max_offset"], current + config["max_step"])
    first = math.ceil((low - minimum) / step - 1e-9)
    last = math.floor((high - minimum) / step + 1e-9)
    if first > last:
        raise ControlError("Inget giltigt steg finns inom säkerhetsgränserna.")
    nearest = math.floor((desired - minimum) / step + 0.5)
    return round(minimum + max(first, min(last, nearest)) * step, 6)


def price_contribution(config, price, error):
    """Offset change from a price level; never worsens an existing comfort deficit/excess."""
    adjustment = 0
    if price <= config["price_low"]:
        adjustment = config["cheap_boost"]
    elif price >= config["price_high"]:
        adjustment = -config["expensive_reduction"]
    if error > config["deadband"]:
        adjustment = max(0, adjustment)
    elif error < -config["deadband"]:
        adjustment = min(0, adjustment)
    return adjustment


def control_rooms(config):
    return config["priority_sensors"] or config["room_sensors"]


def indoor_temperature(config, get):
    selected = control_rooms(config)
    if not selected:
        raise ControlError("Välj minst en rumsgivare.")
    rooms = [temperature(get(key)) for key in selected]
    if any(not 5 <= value <= 40 for value in rooms):
        raise ControlError("En rumsgivare visar en orimlig inomhustemperatur (utanför 5–40 °C).")
    return sum(rooms) / len(rooms)


def current_readings(config, entities, now, peers):
    """Read dashboard values independently; control validates its own selected inputs."""
    states = {entity["entity_id"]: entity for entity in entities}
    get = lambda key: fresh_entity(states, key, config["max_age_minutes"], now, peers.get(key, ()))
    readings = {"indoor": None, "outdoor": None, "current_offset": None, "reading_errors": {}}
    rooms = []
    for key in config["room_sensors"]:
        room = {"entity_id": key, "name": states.get(key, {}).get("attributes", {}).get("friendly_name", key),
                "temperature": None, "priority": key in config["priority_sensors"], "error": None}
        try:
            value = temperature(get(key))
            if not 5 <= value <= 40:
                raise ControlError(f"{key}: orimlig inomhustemperatur.")
            room["temperature"] = round(value, 2)
        except ControlError as exc:
            room["error"] = str(exc)
        rooms.append(room)
    readings["rooms"] = rooms
    readings["house_indoor"] = round(sum(r["temperature"] for r in rooms) / len(rooms), 2) if rooms and all(r["temperature"] is not None for r in rooms) else None
    sources = {
        "indoor": lambda: round(indoor_temperature(config, get), 2),
        "outdoor": lambda: temperature(get(config["outdoor_entity"])),
        "current_offset": lambda: number(get(config["offset_entity"])["state"], "Kurvförskjutning"),
    }
    for key, read in sources.items():
        if (key == "outdoor" and not config["outdoor_entity"]) or (key == "current_offset" and not config["offset_entity"]):
            continue
        try:
            readings[key] = read()
        except ControlError as exc:
            readings["reading_errors"][key] = str(exc)
    return readings


def evaluate(config, entities, now, peers=None, target_shift=0.0):
    """target_shift: temporary change of the wanted temperature (pre-heating), applied through the room rule."""
    states = {entity["entity_id"]: entity for entity in entities}
    peers = peers or {}
    get = lambda key: fresh_entity(states, key, config["max_age_minutes"], now, peers.get(key, ()))
    indoor = indoor_temperature(config, get)
    target_shift = max(-config["preheat_degrees"], min(config["preheat_degrees"], target_shift)) if config["preheat_enabled"] else 0.0
    error = config["target_temperature"] + target_shift - indoor
    room_adjustment = 0 if abs(error) <= config["deadband"] else error * config["room_gain"]
    outdoor = None
    weather_adjustment = 0
    if config["weather_gain"]:
        outdoor = temperature(get(config["outdoor_entity"]))
        weather_adjustment = (config["weather_reference"] - outdoor) * config["weather_gain"]
    price = None
    price_unit = ""
    price_adjustment = 0
    if config["cheap_boost"] or config["expensive_reduction"]:
        price_entity = get(config["price_entity"])
        price = number(price_entity["state"], "Elpris")
        price_unit = price_entity.get("attributes", {}).get("unit_of_measurement", "")
        price_adjustment = price_contribution(config, price, error)
    desired = config["base_offset"] + room_adjustment + weather_adjustment + price_adjustment
    desired = max(config["min_offset"], min(config["max_offset"], desired))
    result = {
        "indoor": round(indoor, 2), "outdoor": outdoor, "price": price,
        "price_unit": price_unit, "error": round(error, 2), "target_shift": round(target_shift, 2),
        "contributions": {"room": round(room_adjustment, 2), "weather": round(weather_adjustment, 2), "price": price_adjustment},
        "desired_offset": round(desired, 3), "current_offset": None,
        "next_offset": None, "writable": False,
    }
    if not config["offset_entity"]:
        return result
    entity = get(config["offset_entity"])
    current = number(entity["state"], "Kurvförskjutning")
    attrs = entity.get("attributes", {})
    minimum = number(attrs.get("min"), "Entitetens minimum")
    maximum = number(attrs.get("max"), "Entitetens maximum")
    step = number(attrs.get("step"), "Entitetens steglängd")
    if step <= 0 or minimum >= maximum or not minimum <= current <= maximum:
        raise ControlError("Kurvförskjutningens intervall eller steglängd är ogiltig.")
    if config["max_step"] < step:
        raise ControlError("Tillåten ändring per steg är mindre än entitetens steglängd.")
    if not minimum <= config["min_offset"] <= config["max_offset"] <= maximum:
        raise ControlError("Säkerhetsgränserna måste ligga inom entitetens tillåtna intervall.")
    if not config["min_offset"] <= current <= config["max_offset"]:
        raise ControlError("Aktuell förskjutning ligger utanför säkerhetsgränserna. Kontrollera pumpen manuellt.")
    next_value = next_step(config, current, desired, minimum, step)
    result.update(current_offset=current, next_offset=next_value, writable=True,
                  offset_limits={"min": minimum, "max": maximum, "step": step})
    return result


class EventLog:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, kind TEXT NOT NULL, "
                "message TEXT NOT NULL, details TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def add(self, kind, message, **details):
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(timestamp,kind,message,details) VALUES (?,?,?,?)",
                (time.time(), kind, message, json.dumps(details, ensure_ascii=False, allow_nan=False)),
            )
            return cursor.lastrowid

    def recent(self, before=None):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,timestamp,kind,message,details FROM events WHERE id < ? ORDER BY id DESC LIMIT 100",
                (before if before is not None else 9223372036854775807,),
            ).fetchall()
        return [
            dict(id=row[0], timestamp=row[1], kind=row[2], message=row[3], details=json.loads(row[4]))
            for row in rows
        ]


class Controller:
    def __init__(self, ha, data_dir):
        self.ha = ha
        self.path = Path(data_dir) / "settings.json"
        self.lock = threading.RLock()
        self.config = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            if isinstance(saved, dict):
                saved.setdefault("forecast_entity", "")
                saved.setdefault("preheat_enabled", False)
                saved.setdefault("preheat_degrees", 0.5)
                saved.setdefault("priority_sensors", [])
            self.config = validate(saved)
        self.events = EventLog(Path(data_dir) / "events.sqlite")
        self.pending = None
        self.observed_offset = None
        self.last_error = None
        self.state_path = Path(data_dir) / "control-state.json"
        # A restart or ambiguous write must never allow a rapid retry.
        self.last_write = time.time()
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.last_write = max(self.last_write, number(state["last_attempt"], "Senaste skrivförsök"))
            self.pending = state.get("pending")
        self.test_path = Path(data_dir) / "test-state.json"
        self.test = None
        if self.test_path.exists():
            saved_test = json.loads(self.test_path.read_text())
            self.test = saved_test if isinstance(saved_test, dict) and saved_test.get("phase") else None
        self.events.add("startup", "Tillägget startat. Minsta ändringsintervall gäller efter omstart.", settings=self.config)
        self.status = {"message": "Väntar på första avläsningen.", "error_message": None}
        self.peer_cache = (0, {})
        self.data_dir = str(data_dir)
        # Set by the web layer: callable(config) -> {"windows": [...]} for price-based pre-heating.
        self.preheat_provider = None
        self.preheat_kind = None

    def peers(self):
        """Same-device entities per entity, refreshed every 5 minutes."""
        loaded_at, peers = self.peer_cache
        if time.time() - loaded_at >= 300:
            peers = device_peers(self.ha.integration_registry())
            self.peer_cache = (time.time(), peers)
        return peers

    def settings(self):
        with self.lock:
            return copy.deepcopy(self.config)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.status)

    def save(self, config):
        config = validate(config)
        with self.lock:
            if config["enabled"]:
                result = evaluate(config, self.ha.states(), time.time(), self.peers())
                if not result["writable"]:
                    raise ControlError("Ingen skrivbar kurvförskjutning är konfigurerad.")
            changes = {key: {"from": self.config[key], "to": value} for key, value in config.items() if value != self.config[key]}
            event_id = self.events.add("settings_attempt", "Begäran att spara inställningar.", changes=changes)
            atomic_json(self.path, config)
            self.config = config
            self.status = {"message": "Inställningar sparade. Väntar på ny avläsning.", "error_message": None,
                           "enabled": config["enabled"]}
            self.events.add("settings_saved", "Inställningar sparade.", request_id=event_id, changes=changes)

    def observe(self, entities, now, peers=None):
        if not self.config["offset_entity"]:
            return
        states = {entity["entity_id"]: entity for entity in entities}
        offset_id = self.config["offset_entity"]
        entity = fresh_entity(states, offset_id, self.config["max_age_minutes"], now, (peers or {}).get(offset_id, ()))
        current = number(entity["state"], "Kurvförskjutning")
        observed = (entity["entity_id"], current)
        if self.observed_offset and observed != self.observed_offset:
            self.events.add(
                "offset_observed", "Ändrad kurvförskjutning återläst från Home Assistant.",
                entity_id=entity["entity_id"], previous=self.observed_offset[1], value=current,
                note="Kan även bero på manuell ändring eller annan automation.",
            )
        self.observed_offset = observed
        if self.pending:
            pending = self.pending
            matched = pending["entity_id"] == entity["entity_id"] and abs(current - pending["value"]) < 1e-6
            expired = now - pending["timestamp"] >= self.config["min_interval_minutes"] * 60
            if matched or expired or pending["entity_id"] != entity["entity_id"]:
                self.events.add(
                    "write_observed" if matched else "write_unconfirmed",
                    "Begärt värde återläst från Home Assistant." if matched else "Begärt värde kunde inte bekräftas genom återläsning.",
                    request_id=pending["request_id"], requested=pending["value"], observed=current,
                    entity_id=pending["entity_id"],
                )
                atomic_json(self.state_path, {"last_attempt": self.last_write, "pending": None})
                self.pending = None

    def preheat_schedule(self):
        """Fetch pre-heating windows outside the lock (may read history). Returns windows or an error text."""
        with self.lock:
            config = copy.deepcopy(self.config)
        if not config["preheat_enabled"] or not self.preheat_provider:
            return None
        try:
            return {"windows": list(self.preheat_provider(config)["windows"])}
        except Exception as exc:  # optional optimisation: fall back to plain comfort control
            return {"error": str(exc) or type(exc).__name__}

    def preheat(self, now, schedule):
        """Active pre-heating window. Any problem means no shift: normal comfort control continues."""
        result = {"shift": 0.0, "kind": None, "until": None, "note": None}
        if not self.config["preheat_enabled"]:
            windows = []
        elif not schedule:
            result["note"] = "Förvärmning är inte tillgänglig ännu."
            windows = []
        elif "error" in schedule:
            result["note"] = f"Förvärmning pausad: {schedule['error']}"
            windows = []
        else:
            windows = schedule["windows"]
        for window in windows:
            if window["start"] <= now < window["end"]:
                result.update(shift=window["shift"], kind=window["kind"], until=window["end"])
                break
        if result["kind"] != self.preheat_kind:
            labels = {"preheat": "Förvärmning startad före dyrt elpris.", "coast": "Dyrt elpris: huset får bli lite svalare.",
                      None: "Förvärmning/sparläge avslutat; vanlig måltemperatur gäller."}
            self.events.add("preheat", labels[result["kind"]], window=result["kind"], shift=result["shift"], until=result["until"])
            self.preheat_kind = result["kind"]
        return result

    def save_test(self):
        atomic_json(self.test_path, self.test or {})

    def test_status(self):
        with self.lock:
            return {"now": time.time(), "duration_seconds": TEST_SECONDS, "test": copy.deepcopy(self.test)}

    def offset_state(self):
        """Fresh offset entity, its value and limits, for a manual test write."""
        if not self.config["offset_entity"]:
            raise ControlError("Ingen kurvförskjutning är vald. Välj din pump under Inställningar först.")
        now = time.time()
        entities = self.ha.states()
        peers = self.peers()
        states = {entity["entity_id"]: entity for entity in entities}
        offset_id = self.config["offset_entity"]
        entity = fresh_entity(states, offset_id, self.config["max_age_minutes"], now, peers.get(offset_id, ()))
        attrs = entity.get("attributes", {})
        current = number(entity["state"], "Kurvförskjutning")
        minimum = number(attrs.get("min"), "Entitetens minimum")
        maximum = number(attrs.get("max"), "Entitetens maximum")
        step = number(attrs.get("step"), "Entitetens steglängd")
        if step <= 0 or minimum >= maximum or not minimum <= current <= maximum:
            raise ControlError("Kurvförskjutningens intervall eller steglängd är ogiltig.")
        return now, offset_id, current, max(minimum, self.config["min_offset"]), min(maximum, self.config["max_offset"]), step

    def start_test(self):
        """Move the offset one step for TEST_SECONDS so the user can see the pump react, then restore it."""
        with self.lock:
            if self.test and self.test["phase"] in ("running", "restoring"):
                raise ControlError("Ett test pågår redan.")
            if self.pending:
                raise ControlError("En ändring från automatiken väntar fortfarande på återläsning. Försök igen om en stund.")
            now, offset_id, current, low, high, step = self.offset_state()
            value = round(current + step, 6) if current + step <= high + 1e-6 else round(current - step, 6)
            if not low - 1e-6 <= value <= high + 1e-6:
                raise ControlError("Säkerhetsgränserna lämnar inget utrymme för ett teststeg. Vidga min/max under Säkerhet och finjustering.")
            request_id = self.events.add(
                "test_write", "Test: kurvförskjutningen ändras tillfälligt ett steg.",
                entity_id=offset_id, previous=current, value=value, restore_after_minutes=TEST_SECONDS // 60,
            )
            self.test = {
                "phase": "running", "entity_id": offset_id, "original": current, "value": value,
                "started_at": now, "restore_at": now + TEST_SECONDS, "observed_at": None,
                "restored_at": None, "finished_at": None, "result": None, "request_id": request_id,
            }
            self.save_test()
            self.last_write = now
            atomic_json(self.state_path, {"last_attempt": now, "pending": None})
            try:
                self.ha.set_offset(offset_id, value)
            except ControlError as exc:
                self.test.update(phase="done", finished_at=now, result="failed", error=str(exc))
                self.save_test()
                self.events.add("test_failed", "Test: Home Assistant tog inte emot ändringen.", request_id=request_id, error=str(exc))
                raise
            self.events.add("test_sent", "Test: Home Assistant accepterade ändringen; inväntar återläsning.", request_id=request_id)
            return copy.deepcopy(self.test)

    def stop_test(self):
        with self.lock:
            if not self.test or self.test["phase"] != "running":
                raise ControlError("Inget test pågår.")
            now, offset_id, current, *_ = self.offset_state()
            self.restore_test(current if offset_id == self.test["entity_id"] else None, now, "Du avbröt testet.")
            return copy.deepcopy(self.test)

    def restore_test(self, current, now, reason):
        test = self.test
        test["restored_at"] = now
        if current is None or (abs(current - test["value"]) > 1e-6 and abs(current - test["original"]) > 1e-6):
            test.update(phase="done", finished_at=now, result="changed")
            self.events.add("test_done", "Test: värdet ändrades av något annat under testet och lämnades orört.",
                            request_id=test["request_id"], observed=current)
        elif abs(current - test["original"]) <= 1e-6:
            test.update(phase="done", finished_at=now, result="confirmed" if test["observed_at"] else "unconfirmed")
            self.events.add("test_done", "Test: förskjutningen är redan tillbaka på ursprungsvärdet.", request_id=test["request_id"])
        else:
            test["phase"] = "restoring"
            self.events.add("test_restore", f"Test: återställer kurvförskjutningen. {reason}",
                            request_id=test["request_id"], value=test["original"])
            self.last_write = now
            atomic_json(self.state_path, {"last_attempt": now, "pending": None})
            try:
                self.ha.set_offset(test["entity_id"], test["original"])
            except ControlError as exc:
                test["phase"] = "running"
                self.save_test()
                self.events.add("test_failed", "Test: återställningen misslyckades; nytt försök vid nästa avläsning.",
                                request_id=test["request_id"], error=str(exc))
                raise
        self.save_test()

    def test_step(self, entities, now):
        """Follow an active test: confirm readback, restore when due and confirm the restore."""
        test = self.test
        if not test or test["phase"] not in ("running", "restoring"):
            return
        entity = next((e for e in entities if e["entity_id"] == test["entity_id"]), None)
        try:
            current = number(entity["state"], "Kurvförskjutning") if entity else None
        except ControlError:
            current = None
        if test["phase"] == "running":
            if current is not None and not test["observed_at"] and abs(current - test["value"]) <= 1e-6:
                test["observed_at"] = now
                self.events.add("test_observed", "Test: Home Assistant läser tillbaka det nya värdet från pumpen.",
                                request_id=test["request_id"], observed=current)
                self.save_test()
            if now >= test["restore_at"]:
                self.restore_test(current, now, "Testtiden är slut.")
        elif current is not None and abs(current - test["original"]) <= 1e-6:
            test.update(phase="done", finished_at=now, result="confirmed" if test["observed_at"] else "unconfirmed")
            self.events.add("test_done", "Test: ursprungsvärdet återläst. Testet är klart.", request_id=test["request_id"])
            self.save_test()
        elif now - test["restored_at"] >= RESTORE_CONFIRM_SECONDS:
            test.update(phase="done", finished_at=now, result="restore_unconfirmed")
            self.events.add("test_done", "Test: återställningen kunde inte bekräftas. Kontrollera förskjutningen på pumpen.",
                            request_id=test["request_id"], observed=current)
            self.save_test()

    def tick(self):
        schedule = self.preheat_schedule()
        with self.lock:
            now = time.time()
            readings = {}
            try:
                entities = self.ha.states()
                peers = self.peers()
                readings = current_readings(self.config, entities, now, peers)
                self.test_step(entities, now)
                self.observe(entities, now, peers)
                preheat = self.preheat(now, schedule)
                result = evaluate(self.config, entities, now, peers, preheat["shift"])
                result["preheat"] = preheat
                result.update(readings)
                if self.last_error:
                    self.events.add("resumed", "Giltiga mätvärden igen; styrningen kan fortsätta.")
                    self.last_error = None
                remaining = max(0, self.config["min_interval_minutes"] * 60 - (now - self.last_write))
                result.update(
                    error_message=None, checked_at=now, enabled=self.config["enabled"],
                    wait_seconds=math.ceil(remaining),
                    message="Övervakning: inga ändringar skickas.",
                )
                testing = bool(self.test and self.test["phase"] in ("running", "restoring"))
                if testing:
                    result["message"] = "Test av styrningen pågår: automatiken väntar tills testet är klart."
                elif self.config["enabled"]:
                    result["message"] = "Ingen ändring behövs."
                    if abs(result["next_offset"] - result["current_offset"]) > 1e-6:
                        result["message"] = "Väntar på nästa tillåtna ändring."
                        if remaining == 0:
                            request_id = self.events.add(
                                "write_attempt", "Automatiken begär ändrad kurvförskjutning.",
                                entity_id=self.config["offset_entity"], previous=result["current_offset"],
                                value=result["next_offset"], target_temperature=self.config["target_temperature"],
                                indoor=result["indoor"], outdoor=result["outdoor"], price=result["price"],
                                contributions=result["contributions"], desired_offset=result["desired_offset"],
                                target_shift=result["target_shift"], preheat=preheat.get("kind"),
                            )
                            pending = dict(
                                request_id=request_id, timestamp=now,
                                entity_id=self.config["offset_entity"], value=result["next_offset"],
                            )
                            atomic_json(self.state_path, {"last_attempt": now, "pending": pending})
                            self.last_write = now
                            self.pending = pending
                            try:
                                self.ha.set_offset(self.config["offset_entity"], result["next_offset"])
                            except ControlError as exc:
                                self.events.add("write_failed", "Skrivförsöket misslyckades; pumpen kan ändå ha tagit emot det.", request_id=request_id, error=str(exc))
                                raise
                            self.events.add("write_sent", "Home Assistant accepterade serviceanropet; inväntar återläsning.", request_id=request_id)
                            result["message"] = "Ändring skickad till Home Assistant; inväntar återläsning."
                            result["wait_seconds"] = math.ceil(self.config["min_interval_minutes"] * 60)
                self.status = result
            except ControlError as exc:
                if self.last_error != str(exc):
                    self.events.add("paused", "Styrningen pausad på grund av fel.", error=str(exc))
                    self.last_error = str(exc)
                self.status = {
                    **readings,
                    "error_message": str(exc), "checked_at": now,
                    "enabled": self.config["enabled"],
                    "message": "Styrningen pausad: inga nya ändringar skickas.",
                }
                raise
