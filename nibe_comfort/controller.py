"""Bounded room-temperature control through Home Assistant entities."""

import copy
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from alarm_codes import alarm_info, alarm_label
from fireplace import empty_state, intervals as fireplace_intervals, update as update_fireplace, validate_state as validate_fireplace_state


DEFAULTS = {
    "enabled": False,
    "target_temperature": 21.0,
    "room_sensors": [],
    "priority_sensors": [],
    "nearest_thermostat": "",
    "thermostat_control_enabled": False,
    "fireplace_enabled": False,
    "fireplace_rooms": [],
    "fireplace_max_age_minutes": 15.0,
    "offset_entity": "",
    "outdoor_entity": "",
    "price_entity": "",
    "price_provider": "tibber",
    "energy_entity": "",
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
    "skip_silent_rooms": False,
    "price_mode": "absolute",
    "price_low_percent": 25.0,
    "price_high_percent": 75.0,
    "auto_base_enabled": False,
    "auto_base_limit": 2.0,
    "publish_sensors": False,
    "pause_notify_minutes": 60.0,
    "hot_water_price_enabled": False,
    "hot_water_mode_entity": "",
    "hot_water_cheap_option": "Normal",
    "hot_water_normal_option": "Economy",
    "aux_limit_enabled": False,
    "aux_limit_entity": "",
    "aux_limit_option": "",
    "aux_normal_option": "",
    "aux_limit_max_drop": 0.5,
    "aux_limit_min_outdoor": -10.0,
    "notify_service": "",
    "grid_import_entity": "",
    "grid_export_entity": "",
    "solar_entity": "",
    "battery_discharge_entity": "",
    "battery_charge_entity": "",
}
SOURCE_ENTITIES = ("grid_import_entity", "grid_export_entity", "solar_entity", "battery_discharge_entity", "battery_charge_entity")
AUX_LIMIT_KEYS = ("aux_limit_enabled", "aux_limit_entity", "aux_limit_option", "aux_normal_option",
                  "aux_limit_max_drop", "aux_limit_min_outdoor")
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
    "fireplace_max_age_minutes": (5, 1440),
    "preheat_degrees": (0.1, 1.5),
    "price_low_percent": (0, 100),
    "price_high_percent": (0, 100),
    "auto_base_limit": (0.5, 5),
    "pause_notify_minutes": (0, 1440),
    "aux_limit_max_drop": (0.1, 2),
    "aux_limit_min_outdoor": (-30, 15),
}


TEST_SECONDS = 600
RESTORE_CONFIRM_SECONDS = 900
# Service calls wait for the integration's cloud write (myUplink often needs more than 15 s).
SERVICE_TIMEOUT_SECONDS = 60
OUTAGE_GRACE_SECONDS = 1800
OUTAGE_MESSAGE = "Pumpens värden är tillfälligt borta från myUplink. Kurv- och termostatstyrningen väntar på aktuella värden."
# Keep the current pump value unless the wanted offset is clearly closer to another step:
# room sensors with 1 °C resolution otherwise make the offset hop every interval.
HYSTERESIS_STEPS = 0.25
# The room rule uses the mean of the last hour's per-minute control averages. Sensors that
# report whole degrees and jump 1–2 °C between reports otherwise move the offset every interval.
SMOOTHING_SECONDS = 3600
VACATION_MODE = "vacation"
VACATION_MODE_SUFFIX = "_smart_home_mode"
VACATION_RETRY_SECONDS = 300
VACATION_MAX_SECONDS = 366 * 86400
# A "Jag eldar nu" press waits this long for fresh room values before it is dropped.
MANUAL_FIREPLACE_PENDING_SECONDS = 1800
# The pump needs time to finish a hot water charge; never flip the mode more often than this.
HOT_WATER_MIN_SECONDS = 3600
# The electric heater limit uses the same pace; lifting the limit is never delayed.
AUX_LIMIT_MIN_SECONDS = 3600
# Automatic base correction: one curve step per degree of lasting deviation and day. Small
# deviations are ignored so sensor noise does not walk the correction.
AUTO_BASE_RATE = 1 / 86400
AUTO_BASE_NOISE = 0.1
AUTO_BASE_MAX_GAP = 600
AUTO_BASE_SAVE_SECONDS = 600
PAUSE_NOTIFICATION_ID = "nibe_comfort_paus"
SENSOR_PREFIX = "sensor.nibe_comfort_"
MONTHS = ("jan", "feb", "mars", "apr", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")


LOG = logging.getLogger("nibe_comfort")


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
    if result["price_provider"] not in ("tibber", "nordpool"):
        raise ControlError("Välj Tibber eller Nord Pool som elpriskälla.")
    if result["price_mode"] not in ("absolute", "relative"):
        raise ControlError("Prisnivån måste följa fasta gränser eller dygnets egna priser.")
    for key in ("enabled", "preheat_enabled", "thermostat_control_enabled", "fireplace_enabled",
                "auto_base_enabled", "publish_sensors", "skip_silent_rooms", "hot_water_price_enabled",
                "aux_limit_enabled"):
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
    fireplace_rooms = result["fireplace_rooms"]
    if not isinstance(fireplace_rooms, list) or any(not isinstance(key, str) or key not in rooms for key in fireplace_rooms):
        raise ControlError("Kaminrum måste väljas bland dina rumsgivare.")
    if len(set(fireplace_rooms)) != len(fireplace_rooms):
        raise ControlError("Samma kaminrum får inte väljas flera gånger.")
    if result["fireplace_enabled"] and not fireplace_rooms:
        raise ControlError("Välj minst en temperaturkälla i rummet med braskamin.")
    if result["fireplace_enabled"] and not set(rooms) - set(fireplace_rooms):
        raise ControlError("Välj även minst ett rum utan kamin för temperaturjämförelse.")
    nearest = result["nearest_thermostat"]
    if not isinstance(nearest, str) or (nearest and (nearest not in rooms or not nearest.startswith("climate."))):
        raise ControlError("Termostaten närmast pumpen måste vara en av dina valda termostater.")
    if result["thermostat_control_enabled"]:
        if not rooms:
            raise ControlError("Välj minst en climate-termostat för automatisk termostatbegränsning.")
        sensors = [key for key in rooms if not key.startswith("climate.")]
        if sensors:
            raise ControlError("Dessa valda rum är temperaturgivare, inte styrbara climate-termostater: "
                               + ", ".join(sensors) + ". Avmarkera dem under Rumsgivare och välj termostatens climate-entitet.")
    for key, prefixes in (
        ("offset_entity", ("number.",)),
        ("outdoor_entity", ("sensor.", "weather.")),
        ("price_entity", ("sensor.",)),
        ("energy_entity", ("sensor.",)),
        ("forecast_entity", ("weather.",)),
        ("notify_service", ("notify.",)),
        ("hot_water_mode_entity", ("select.",)),
        ("aux_limit_entity", ("select.",)),
        *((key, ("sensor.",)) for key in SOURCE_ENTITIES),
    ):
        value = result[key]
        if not isinstance(value, str) or (value and not value.startswith(prefixes)):
            raise ControlError(f"{key}: fel entitetstyp.")
    for key in ("hot_water_cheap_option", "hot_water_normal_option"):
        if not isinstance(result[key], str) or not result[key].strip() or len(result[key]) > 64:
            raise ControlError(f"{key}: ange pumpens namn på läget.")
        result[key] = result[key].strip()
    if result["hot_water_price_enabled"]:
        if not result["hot_water_mode_entity"]:
            raise ControlError("Välj pumpens varmvattenläge (select) för varmvatten på billig el.")
        if result["hot_water_cheap_option"] == result["hot_water_normal_option"]:
            raise ControlError("Läget vid billig el och vanligt läge måste vara olika.")
    for key in ("aux_limit_option", "aux_normal_option"):
        if not isinstance(result[key], str) or len(result[key]) > 64:
            raise ControlError(f"{key}: ange pumpens namn på läget.")
        result[key] = result[key].strip()
    if result["aux_limit_enabled"]:
        if not result["aux_limit_entity"]:
            raise ControlError("Välj pumpens inställning för max eltillsats (select) för att begränsa elpatronen.")
        if not result["aux_limit_option"] or not result["aux_normal_option"]:
            raise ControlError("Välj både begränsad och vanlig max eltillsats.")
        if result["aux_limit_option"] == result["aux_normal_option"]:
            raise ControlError("Begränsad och vanlig max eltillsats måste vara olika.")
    if not result["min_offset"] <= result["base_offset"] <= result["max_offset"]:
        raise ControlError("Basförskjutningen måste ligga inom säkerhetsgränserna.")
    if result["price_low"] >= result["price_high"]:
        raise ControlError("Lågprisgränsen måste vara lägre än högprisgränsen.")
    if result["price_low_percent"] >= result["price_high_percent"]:
        raise ControlError("Andelen billiga timmar måste vara lägre än gränsen för dyra.")
    if result["weather_gain"] and not result["outdoor_entity"]:
        raise ControlError("Välj utomhusgivare för väderpåverkan.")
    if (result["cheap_boost"] or result["expensive_reduction"]) and not result["price_entity"]:
        raise ControlError("Välj elprisgivare för elprisstyrning.")
    sources = [result[key] for key in SOURCE_ENTITIES if result[key]]
    if sources and not result["grid_import_entity"]:
        raise ControlError("Välj givaren för köpt el från elnätet; utan den kan husets förbrukning inte räknas.")
    if len(set(sources)) != len(sources):
        raise ControlError("Samma givare kan inte användas för flera av husets elflöden.")
    if result["preheat_enabled"] and not result["price_entity"]:
        raise ControlError("Välj elprisgivare för förvärmning.")
    if result["enabled"] and (not rooms or not result["offset_entity"]):
        raise ControlError("Välj rumsgivare och kurvförskjutning innan aktivering.")
    return result


def validate_vacation(plan):
    """Saved vacation plan, or None when no vacation is planned."""
    if plan is None or plan == {}:
        return None
    if (not isinstance(plan, dict) or plan.get("phase") not in ("scheduled", "active", "restoring")
            or not isinstance(plan.get("entity_id"), str) or not plan["entity_id"].startswith("select.")
            or not plan["entity_id"].endswith(VACATION_MODE_SUFFIX)
            or not isinstance(plan.get("confirmed"), bool)
            or "previous" not in plan or (plan["previous"] is not None and not isinstance(plan["previous"], str))
            or "error" not in plan or (plan["error"] is not None and not isinstance(plan["error"], str))):
        raise ControlError("Ogiltig sparad semesterperiod.")
    for key in ("start", "end", "attempted_at"):
        if not isinstance(plan.get(key), (int, float)) or number(plan[key], "Semesterperiod") < 0:
            raise ControlError("Ogiltig sparad semesterperiod.")
    if plan["end"] < plan["start"]:
        raise ControlError("Ogiltig sparad semesterperiod.")
    return plan


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

    def request(self, path, data=None, timeout=15):
        request = Request(
            self.url + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if path.startswith("/history/period/"):
                    limit = 8 * 1024 * 1024
                    payload = response.read(limit + 1)
                    if len(payload) > limit:
                        raise ControlError("Historiksvaret överstiger 8 MiB. Hämtningen stoppades för att skydda minnet.")
                    return json.loads(payload)
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            raise ControlError(f"Home Assistant svarade med HTTP {exc.code}. Kontrollera integration och behörighet.") from exc
        except TimeoutError as exc:
            raise ControlError(f"Home Assistant svarade inte inom {timeout} s.") from exc
        except (URLError, OSError, ValueError) as exc:
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
        self.request("/services/number/set_value", {"entity_id": entity_id, "value": value}, timeout=SERVICE_TIMEOUT_SECONDS)

    def set_thermostat(self, entity_id, value):
        self.request("/services/climate/set_temperature", {"entity_id": entity_id, "temperature": value}, timeout=SERVICE_TIMEOUT_SECONDS)

    def set_select(self, entity_id, option, timeout=SERVICE_TIMEOUT_SECONDS):
        self.request("/services/select/select_option", {"entity_id": entity_id, "option": option}, timeout=timeout)

    def notify_services(self):
        """Notify services a person can pick, e.g. the mobile app on a phone, with a readable name."""
        domains = self.request("/services")
        if not isinstance(domains, list):
            raise ControlError("Home Assistant returnerade ett ogiltigt tjänstesvar.")
        names = next((d.get("services") for d in domains if isinstance(d, dict) and d.get("domain") == "notify"), None) or {}
        if not isinstance(names, dict):
            raise ControlError("Home Assistant returnerade ett ogiltigt tjänstesvar.")
        services = []
        # send_message needs a notify entity, and persistent_notification is always used anyway.
        for name in names:
            if name in ("send_message", "persistent_notification") or not re.fullmatch(r"[a-z0-9_]+", name):
                continue
            label = name.removeprefix("mobile_app_").replace("_", " ").strip().capitalize() or name
            services.append({"service": f"notify.{name}",
                             "name": f"Mobilappen · {label}" if name.startswith("mobile_app_") else label})
        return sorted(services, key=lambda s: (not s["service"].startswith("notify.mobile_app_"), s["name"]))

    def integration_registry(self):
        template = (
            "{% set ns = namespace(items=[]) %}"
            "{% for platform in ['myuplink', 'tibber', 'nordpool', 'smhi', 'met'] %}"
            "{% for eid in integration_entities(platform) %}"
            "{% set ns.items = ns.items + "
            "[{'entity_id': eid, 'device_id': device_id(eid), 'platform': platform, "
            "'config_entry_id': config_entry_id(eid)}] %}"
            "{% endfor %}{% endfor %}"
            "{% for climate in states.climate %}"
            "{% set did = device_id(climate.entity_id) %}"
            "{% if did and did not in ns.items | map(attribute='device_id') | list %}"
            "{% for eid in device_entities(did) %}"
            "{% set ns.items = ns.items + "
            "[{'entity_id': eid, 'device_id': did, 'platform': 'climate_device'}] %}"
            "{% endfor %}{% endif %}{% endfor %}"
            "{% for s in states.sensor | selectattr('attributes.device_class', 'defined')"
            " | selectattr('attributes.device_class', 'eq', 'temperature') %}"
            "{% set did = device_id(s.entity_id) %}"
            "{% set entry = config_entry_id(s.entity_id) %}"
            "{% if did and entry and did not in ns.items | map(attribute='device_id') | list %}"
            "{% for eid in device_entities(did) if eid.split('.')[0] in ['sensor', 'binary_sensor']"
            " and config_entry_id(eid) == entry %}"
            "{% set ns.items = ns.items + "
            "[{'entity_id': eid, 'device_id': did, 'platform': 'room_device', 'config_entry_id': entry}] %}"
            "{% endfor %}{% endif %}{% endfor %}{{ ns.items | to_json }}"
        )
        registry = self.request("/template", {"template": template})
        validate_registry(registry)
        return registry

    def entity_areas(self, entity_ids):
        """Resolve Home Assistant area assignments for the supplied entity IDs."""
        if (not isinstance(entity_ids, list) or len(entity_ids) > 100
                or any(not isinstance(eid, str) for eid in entity_ids)):
            raise ControlError("Ogiltig lista med entiteter för rumsindelning.")
        if not entity_ids:
            return {}
        template = (
            "{% set ns = namespace(items=[]) %}"
            "{% for eid in " + json.dumps(entity_ids, ensure_ascii=True) + " %}"
            "{% set aid = area_id(eid) %}"
            "{% set ns.items = ns.items + [{'entity_id': eid, 'area_id': aid, "
            "'area_name': area_name(aid) if aid else none}] %}"
            "{% endfor %}{{ ns.items | to_json }}"
        )
        rows = self.request("/template", {"template": template})
        if not isinstance(rows, list) or len(rows) != len(entity_ids) or any(
            not isinstance(row, dict)
            or row.get("entity_id") not in entity_ids
            or (row.get("area_id") is not None and not isinstance(row["area_id"], str))
            or (row.get("area_name") is not None and not isinstance(row["area_name"], str))
            for row in rows
        ) or len({row["entity_id"] for row in rows}) != len(entity_ids):
            raise ControlError("Home Assistant returnerade ogiltiga rumsuppgifter.")
        return {row["entity_id"]: {"area_id": row["area_id"], "area_name": row["area_name"]} for row in rows}

    def discover(self):
        from insights import discover_prices
        registry = self.integration_registry()
        states = self.states()
        result = discover_nibe(states, registry)
        sources = {provider: discover_prices(states, registry, provider) for provider in ("tibber", "nordpool")}
        result.update(sources["tibber"])
        result["price_sources"] = sources
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


def text_status(entity):
    """True for a textual operating-status sensor (Heating, Hot water, ...).

    Several NIBE models also expose a numeric counter whose entity id ends in `_status`
    (F370: \"status: (50095)\" in minutes). A counter with a unit or a duration class is not a mode."""
    attrs = entity.get("attributes", {})
    if attrs.get("unit_of_measurement") or attrs.get("device_class") in ("duration", "timestamp"):
        return False
    try:
        float(entity.get("state"))
    except (TypeError, ValueError):
        return True
    return False


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
            groups.setdefault((item.get("platform"), item["device_id"], item.get("config_entry_id")), set()).add(item["entity_id"])
    peers = {}
    for members in groups.values():
        for entity_id in members:
            peers.setdefault(entity_id, set()).update(members - {entity_id})
    return {entity_id: sorted(ids) for entity_id, ids in peers.items()}


def entity_base(entity_id):
    """Entity id without a myUplink parameter id suffix (sensor.x_alarms_45001 -> sensor.x_alarms)."""
    return re.sub(r"_\d+$", "", entity_id)


def alarm_sources(ids):
    """Alarm status and main alarm code entities among one pump's entities.

    Sub-module codes such as sensor.x_alarms_eb100_ep14_44137 are not the pump's own alarm code.
    """
    ids = list(ids)
    return (sorted(key for key in ids if key.startswith("binary_sensor.") and entity_base(key).endswith("_alarm")),
            sorted(key for key in ids if key.startswith("sensor.") and entity_base(key).endswith("_alarms")))


def short_date(stamp):
    moment = datetime.fromtimestamp(stamp)
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def changed_at(entity):
    """When the value last changed as a timestamp. Home Assistant resets this at restart, so it is a lower bound."""
    try:
        return datetime.fromisoformat(entity.get("last_changed") or entity.get("last_updated")).timestamp()
    except (TypeError, ValueError):
        return None


def alarm_code(entity):
    code = number(entity["state"], "Larmkod")
    if code != int(code) or not 0 <= code <= 99999:
        raise ControlError("Pumpens larmkod kan inte tolkas.")
    return int(code)


def pump_alarm(entities, config, peers, now):
    """Only read alarm entities associated with the configured pump."""
    states = {e["entity_id"]: e for e in entities}
    ids = set(peers.get(config["offset_entity"], ()))
    result = {"active": None, "code": None, "latest_code": None, "info": None, "since": None, "error": None, "reported_at": None}
    alarms, codes = alarm_sources(ids)
    try:
        if len(alarms) == 1:
            alarm = fresh_entity(states, alarms[0], config["max_age_minutes"], now, ids)
            if alarm["state"] not in ("on", "off"):
                raise ControlError("Pumpens larmstatus kan inte tolkas.")
            result.update(active=alarm["state"] == "on",
                          reported_at=alarm.get("last_reported") or alarm.get("last_updated"))
            if len(codes) == 1:
                code = alarm_code(fresh_entity(states, codes[0], config["max_age_minutes"], now, ids))
                result["code"] = code if result["active"] else None
                result["info"] = alarm_info(result["code"])
            elif result["active"]:
                raise ControlError("Aktivt larm, men ingen entydig kodgivare hittades.")
        elif not alarms and len(codes) == 1:
            # Models without an alarm status entity (e.g. F370) keep the latest alarm number even after it
            # has cleared, so the number alone cannot prove an active alarm.
            entity = fresh_entity(states, codes[0], config["max_age_minutes"], now, ids)
            code = alarm_code(entity)
            since = changed_at(entity) if code else None
            result.update(latest_code=code or None, since=since, info=alarm_info(code) if code else None,
                          reported_at=entity.get("last_reported") or entity.get("last_updated"))
            raise ControlError("Pumpen har ingen larmstatus i myUplink, bara senaste larmkod"
                               + (f" ({alarm_label(code)}{f', oförändrad åtminstone sedan {short_date(since)}' if since else ''})" if code else "")
                               + ". Se pumpens display för om larmet är aktivt.")
        else:
            raise ControlError("Ingen entydig larmgivare hittades på vald pump.")
    except ControlError as exc:
        result["error"] = str(exc)
    return result


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


def series_rank(points, stamp, price):
    """Place of a price among the same day's prices: 0 = cheapest, 1 = most expensive."""
    day = datetime.fromtimestamp(stamp).date()
    values = [value for start, value in points if datetime.fromtimestamp(start).date() == day]
    if len(values) < 2:
        return None
    return sum(value < price for value in values) / (len(values) - 1)


def price_rank(entity, now):
    """Today's rank of the current price from the price sensor itself (Tibber or Nord Pool attributes)."""
    attrs = entity.get("attributes", {})
    price = number(entity["state"], "Elpris")
    ranking = attrs.get("intraday_price_ranking")
    if isinstance(ranking, (int, float)) and not isinstance(ranking, bool) and 0 <= ranking <= 1:
        return float(ranking)
    points = []
    for row in attrs.get("raw_today") or []:
        try:
            start = datetime.fromisoformat(str(row["start"])).timestamp()
            points.append((start, number(row["value"], "Elpris")))
        except (KeyError, TypeError, ValueError, ControlError):
            continue
    if not points:
        values = [value for value in attrs.get("today") or [] if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if len(values) >= 2:
            return sum(value < price for value in values) / (len(values) - 1)
        return None
    return series_rank(points, now, price)


def price_level(config, price, rank=None):
    """'cheap', 'expensive' or None. Relative mode compares with the day's own prices."""
    if config["price_mode"] == "relative":
        if rank is None:
            return None
        return "cheap" if rank * 100 <= config["price_low_percent"] + 1e-9 else \
            "expensive" if rank * 100 >= config["price_high_percent"] - 1e-9 else None
    return "cheap" if price <= config["price_low"] else "expensive" if price >= config["price_high"] else None


def price_contribution(config, price, error, rank=None):
    """Offset change from a price level; never worsens an existing comfort deficit/excess."""
    adjustment = 0
    level = price_level(config, price, rank)
    if level == "cheap":
        adjustment = config["cheap_boost"]
    elif level == "expensive":
        adjustment = -config["expensive_reduction"]
    if error > config["deadband"]:
        adjustment = max(0, adjustment)
    elif error < -config["deadband"]:
        adjustment = min(0, adjustment)
    return adjustment


def fireplace_contribution(config, indoor, active):
    """No extra reduction while the ordinary control-room average is cold."""
    return -1.0 if config["fireplace_enabled"] and active \
        and indoor >= config["target_temperature"] - config["deadband"] - 1e-6 else 0.0


def control_rooms(config):
    return config["priority_sensors"] or config["room_sensors"]


def thermostat_state(config, states, key, now, peers):
    entity = fresh_entity(states, key, config["max_age_minutes"], now, peers.get(key, ()))
    attrs = entity["attributes"]
    if entity["state"] != "heat":
        raise ControlError(f"{key}: termostaten måste vara i värmeläge (heat); läget ändras inte automatiskt.")
    features = number(attrs.get("supported_features"), key)
    if features < 0 or features != int(features) or int(features) & 1 == 0:
        raise ControlError(f"{key}: stöd för ett enskilt skrivbart temperaturbörvärde saknas.")
    current = temperature(entity)
    if not 5 <= current <= 40:
        raise ControlError(f"{key}: orimlig inomhustemperatur.")
    unit = attrs.get("temperature_unit")
    target = config["target_temperature"] if unit == "°C" else config["target_temperature"] * 9 / 5 + 32
    minimum = number(attrs.get("min_temp"), f"{key} minimum")
    maximum = number(attrs.get("max_temp"), f"{key} maximum")
    raw_step = attrs.get("target_temp_step")
    step = number(raw_step, f"{key} steglängd") if raw_step is not None else None
    setpoint = number(attrs.get("temperature"), f"{key} börvärde")
    if (step is not None and step <= 0) or minimum >= maximum or not minimum <= setpoint <= maximum:
        raise ControlError(f"{key}: ogiltiga temperaturgränser eller steglängd.")
    if not minimum <= target <= maximum or (step is not None and abs((target - minimum) / step - round((target - minimum) / step)) > 1e-6):
        raise ControlError(f"{key}: ditt mål kan inte anges exakt inom termostatens intervall och steglängd.")
    return current, setpoint, round(target, 6)


def read_rooms(keys, get):
    values, skipped = {}, []
    for key in keys:
        try:
            value = temperature(get(key))
            if not 5 <= value <= 40:
                raise ControlError(f"{key}: orimlig inomhustemperatur (utanför 5–40 °C).")
            values[key] = value
        except ControlError as exc:
            skipped.append({"entity_id": key, "error": str(exc)})
    return values, skipped


def indoor_reading(config, get):
    """Mean of the controlling rooms.

    By default every controlling room must report, so a missing cold or warm room never skews
    the mean. With skip_silent_rooms, battery sensors that stay silent for hours are skipped
    while another controlling room still reports; when every prioritised room is silent, the
    other selected rooms are used instead. Control then pauses only when no selected room reports.
    """
    selected = control_rooms(config)
    if not selected:
        raise ControlError("Välj minst en rumsgivare.")
    if not config["skip_silent_rooms"]:
        rooms = [temperature(get(key)) for key in selected]
        if any(not 5 <= value <= 40 for value in rooms):
            raise ControlError("En rumsgivare visar en orimlig inomhustemperatur (utanför 5–40 °C).")
        return {"value": sum(rooms) / len(rooms), "rooms": list(selected), "skipped": [], "fallback": False}
    values, skipped = read_rooms(selected, get)
    fallback = False
    if not values and config["priority_sensors"]:
        others = [key for key in config["room_sensors"] if key not in selected]
        values, more = read_rooms(others, get)
        skipped += more
        fallback = bool(values)
    if not values:
        if len(skipped) == 1:
            raise ControlError(skipped[0]["error"])
        raise ControlError("Ingen rumsgivare har ett aktuellt värde: " + " ".join(room["error"] for room in skipped))
    return {"value": sum(values.values()) / len(values), "rooms": list(values), "skipped": skipped, "fallback": fallback}


def indoor_temperature(config, get):
    return indoor_reading(config, get)["value"]


def current_readings(config, entities, now, peers):
    """Read dashboard values independently; control validates its own selected inputs."""
    states = {entity["entity_id"]: entity for entity in entities}
    get = lambda key: fresh_entity(states, key, config["max_age_minutes"], now, peers.get(key, ()))
    readings = {"indoor": None, "outdoor": None, "current_offset": None, "reading_errors": {}}
    rooms = []
    for key in config["room_sensors"]:
        room = {"entity_id": key, "name": states.get(key, {}).get("attributes", {}).get("friendly_name", key),
                "temperature": None, "priority": key in config["priority_sensors"],
                "nearest": key == config["nearest_thermostat"], "error": None}
        try:
            value = temperature(get(key))
            if not 5 <= value <= 40:
                raise ControlError(f"{key}: orimlig inomhustemperatur.")
            room["temperature"] = round(value, 2)
        except ControlError as exc:
            room["error"] = str(exc)
        room.update(thermostat=None, setpoint=None, thermostat_mode=None, thermostat_error=None)
        candidates = [key] if key.startswith("climate.") else [
            eid for eid in peers.get(key, ()) if eid.startswith("climate.")
        ]
        if len(candidates) == 1:
            room["thermostat"] = candidates[0]
            try:
                thermostat = get(candidates[0])
                room["thermostat_mode"] = thermostat["state"]
                attrs = thermostat["attributes"]
                if attrs.get("temperature") is not None:
                    room["setpoint"] = round(temperature({
                        **thermostat, "attributes": {**attrs, "current_temperature": attrs["temperature"]},
                    }), 2)
            except ControlError as exc:
                room["thermostat_error"] = str(exc)
        elif candidates:
            room["thermostat_error"] = "Flera termostater på samma enhet; ingen väljs automatiskt."
        rooms.append(room)
    readings["rooms"] = rooms
    readings["house_indoor"] = round(sum(r["temperature"] for r in rooms) / len(rooms), 2) if rooms and all(r["temperature"] is not None for r in rooms) else None
    readings["warm_room_limit"] = config["target_temperature"] + 2
    readings["warm_rooms"] = [
        room["entity_id"] for room in rooms
        if room["entity_id"] not in control_rooms(config)
        and room["temperature"] is not None
        and room["temperature"] >= readings["warm_room_limit"]
    ]
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


def smoothed_indoor(samples, now):
    """Mean of the per-minute control averages within the window; samples are (time, value) pairs."""
    values = [value for stamp, value in samples if now - SMOOTHING_SECONDS < stamp <= now]
    return sum(values) / len(values) if values else None


def evaluate(config, entities, now, peers=None, target_shift=0.0, fireplace_active=False, indoor_samples=(),
             base_correction=0.0):
    """target_shift: temporary change of the wanted temperature (pre-heating), applied through the room rule.

    indoor_samples: earlier (time, value) control averages; the room rule then uses their mean
    together with the current reading instead of the current reading alone.
    base_correction: learned long-term addition to the base offset (automatic base correction).
    """
    states = {entity["entity_id"]: entity for entity in entities}
    peers = peers or {}
    get = lambda key: fresh_entity(states, key, config["max_age_minutes"], now, peers.get(key, ()))
    reading = indoor_reading(config, get)
    indoor_now = reading["value"]
    indoor = smoothed_indoor([*indoor_samples, (now, indoor_now)], now)
    target_shift = max(-config["preheat_degrees"], min(config["preheat_degrees"], target_shift)) if config["preheat_enabled"] else 0.0
    error = config["target_temperature"] + target_shift - indoor
    room_adjustment = 0 if abs(error) <= config["deadband"] else error * config["room_gain"]
    outdoor = None
    weather_adjustment = 0
    if config["weather_gain"]:
        outdoor = temperature(get(config["outdoor_entity"]))
        weather_adjustment = (config["weather_reference"] - outdoor) * config["weather_gain"]
    price = rank = None
    price_unit = ""
    price_error = None
    price_adjustment = 0
    price_rule = bool(config["cheap_boost"] or config["expensive_reduction"])
    if config["price_entity"]:
        # The price is shown even when it does not steer; only the price rule needs a valid one.
        try:
            price_entity = get(config["price_entity"])
            price = number(price_entity["state"], "Elpris")
            price_unit = price_entity.get("attributes", {}).get("unit_of_measurement", "")
            if config["price_mode"] == "relative":
                rank = price_rank(price_entity, now)
                if rank is None:
                    price_error = "Prisgivaren saknar dygnets priser; den relativa prisregeln gör inget just nu."
        except ControlError as exc:
            if price_rule:
                raise
            price_error = str(exc)
    if price_rule:
        price_adjustment = price_contribution(config, price, error, rank)
    fireplace_adjustment = fireplace_contribution(config, indoor_now, fireplace_active)
    base_correction = round(base_correction, 3) + 0.0 if config["auto_base_enabled"] else 0.0
    desired = (config["base_offset"] + base_correction + room_adjustment + weather_adjustment
               + price_adjustment + fireplace_adjustment)
    requested = desired
    desired = max(config["min_offset"], min(config["max_offset"], desired))
    result = {
        "indoor": round(indoor, 2), "indoor_smoothed": round(indoor, 2), "indoor_now": indoor_now,
        "outdoor": outdoor, "price": price,
        "price_unit": price_unit, "price_rank": None if rank is None else round(rank, 3),
        "price_level": price_level(config, price, rank) if price is not None else None,
        "price_error": price_error, "error": round(error, 2), "target_shift": round(target_shift, 2),
        "indoor_rooms": reading["rooms"], "skipped_rooms": reading["skipped"], "indoor_fallback": reading["fallback"],
        "contributions": {"room": round(room_adjustment, 2), "weather": round(weather_adjustment, 2),
                          "price": price_adjustment, "fireplace": fireplace_adjustment,
                          "base_correction": base_correction},
        "desired_offset": round(desired, 3), "current_offset": None,
        "next_offset": None, "writable": False,
        "control_limit": (
            {"direction": "upper", "value": config["max_offset"], "requested": round(requested, 3)}
            if requested > config["max_offset"] + 1e-6 else
            {"direction": "lower", "value": config["min_offset"], "requested": round(requested, 3)}
            if requested < config["min_offset"] - 1e-6 else None
        ),
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
    if abs(desired - current) < step * (0.5 + HYSTERESIS_STEPS) and not result["control_limit"]:
        next_value = current
    result.update(current_offset=current, next_offset=next_value, writable=True,
                  offset_limits={"min": minimum, "max": maximum, "step": step})
    return result


FIREPLACE_LABELS = {"idle": "Ingen", "learning": "Lär sig", "active": "Eldar", "cooldown": "Avsvalnar"}
PRICE_LABELS = {"cheap": "Billigt", "expensive": "Dyrt"}


def control_state(status, config):
    """One word for what the control is doing right now."""
    vacation = status.get("vacation") or {}
    if status.get("stopped"):
        return "Stoppad"
    if not config["enabled"]:
        return "Övervakning"
    if status.get("error_message"):
        return "Pausad"
    if vacation.get("phase") in ("active", "restoring"):
        return "Semester"
    if status.get("outage_since") is not None:
        return "Väntar på pumpen"
    kind = (status.get("preheat") or {}).get("kind")
    if kind == "preheat":
        return "Förvärmer"
    if kind == "coast":
        return "Sparar"
    if (status.get("fireplace") or {}).get("phase") == "active":
        return "Kamin"
    return "Styr"


def sensor_states(status, config):
    """Status sensors for Home Assistant as (entity_id, state, attributes).

    They go through Home Assistant's state API, so they are recreated by the next control
    round after a Home Assistant restart and have no entity registry entry.
    """
    def value(number_value):
        return "unknown" if number_value is None else number_value

    fireplace = status.get("fireplace") or {}
    preheat = status.get("preheat") or {}
    base = status.get("base_correction") or {}
    target = None
    if status.get("target_shift") is not None and not status.get("error_message"):
        target = round(config["target_temperature"] + status["target_shift"], 2)
    temperature_attrs = {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}
    sensors = [
        ("status", control_state(status, config), {
            "friendly_name": "NIBE Comfort status", "icon": "mdi:heat-pump",
            "message": status.get("error_message") or status.get("message"),
            "warnings": status.get("warnings") or [], "contributions": status.get("contributions"),
            "preheat_until": preheat.get("until"), "checked_at": status.get("checked_at"),
        }),
        ("innetemp", value(None if status.get("error_message") else status.get("indoor_smoothed")),
         {"friendly_name": "NIBE Comfort innetemp", **temperature_attrs}),
        ("mal", value(target), {"friendly_name": "NIBE Comfort mål", **temperature_attrs}),
        ("onskad_forskjutning", value(status.get("desired_offset")),
         {"friendly_name": "NIBE Comfort önskad förskjutning", "icon": "mdi:plus-minus-variant", "state_class": "measurement"}),
        ("nasta_forskjutning", value(status.get("next_offset")),
         {"friendly_name": "NIBE Comfort nästa förskjutning", "icon": "mdi:arrow-right-bold"}),
        ("kamin", FIREPLACE_LABELS.get(fireplace.get("phase"), fireplace.get("phase") or "unknown"), {
            "friendly_name": "NIBE Comfort kamin", "icon": "mdi:fireplace",
            "message": fireplace.get("message"), "learned": fireplace.get("learned"),
        }),
        ("elprisniva", PRICE_LABELS.get(status.get("price_level"), "Normalt" if status.get("price") is not None else "unknown"), {
            "friendly_name": "NIBE Comfort elprisnivå", "icon": "mdi:cash-clock",
            "price": status.get("price"), "rank": status.get("price_rank"), "mode": config["price_mode"],
        }),
    ]
    if config["auto_base_enabled"]:
        sensors.append(("baskorrigering", value(base.get("value")), {
            "friendly_name": "NIBE Comfort baskorrigering", "icon": "mdi:tune-vertical", "state_class": "measurement",
            "limit": base.get("limit"),
        }))
    return [(SENSOR_PREFIX + key, state, attrs) for key, state, attrs in sensors]


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
            connection.execute("CREATE INDEX IF NOT EXISTS events_timestamp ON events(timestamp)")

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

    def history(self, start, end):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,timestamp,kind,message,details FROM events "
                "WHERE timestamp BETWEEN ? AND ? OR id IN "
                "(SELECT json_extract(details,'$.request_id') FROM events WHERE timestamp BETWEEN ? AND ?) "
                "ORDER BY timestamp,id",
                (start, end, start, end),
            ).fetchall()
        return [dict(id=r[0], timestamp=r[1], kind=r[2], message=r[3], details=json.loads(r[4])) for r in rows]


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
                saved.setdefault("nearest_thermostat", "")
                saved.setdefault("thermostat_control_enabled", False)
                saved.setdefault("fireplace_enabled", False)
                saved.setdefault("fireplace_rooms", [])
                saved.setdefault("fireplace_max_age_minutes", 15.0)
                saved.setdefault("price_provider", "tibber")
                saved.setdefault("energy_entity", "")
                for key in ("skip_silent_rooms", "price_mode", "price_low_percent", "price_high_percent", "auto_base_enabled",
                            "auto_base_limit", "publish_sensors", "pause_notify_minutes", "notify_service",
                            "hot_water_price_enabled", "hot_water_mode_entity", "hot_water_cheap_option",
                            "hot_water_normal_option", *SOURCE_ENTITIES, *AUX_LIMIT_KEYS):
                    saved.setdefault(key, DEFAULTS[key])
            self.config = validate(saved)
        self.events = EventLog(Path(data_dir) / "events.sqlite")
        self.pending = None
        self.observed_offset = None
        self.last_error = None
        self.outage_since = None
        self.state_path = Path(data_dir) / "control-state.json"
        # Preserve the interval from the actual attempt, not the process start.
        self.last_write = 0
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.last_write = number(state["last_attempt"], "Senaste skrivförsök")
            if self.last_write < 0:
                raise ControlError("Ogiltig tid för senaste skrivförsök.")
            self.pending = state.get("pending")
        self.test_path = Path(data_dir) / "test-state.json"
        self.test = None
        if self.test_path.exists():
            saved_test = json.loads(self.test_path.read_text())
            self.test = saved_test if isinstance(saved_test, dict) and saved_test.get("phase") else None
        self.events.add("startup", "Tillägget startat. Återläser värden; väntetid räknas från senaste sparade styrförsök.", settings=self.config)
        self.status = {"message": "Väntar på första avläsningen.", "error_message": None}
        self.peer_cache = (0, {})
        # Recent control averages for smoothing; in memory only, so a restart starts from the current reading.
        self.indoor_rooms = ()
        self.indoor_samples = []
        self.data_dir = str(data_dir)
        # Set by the web layer: callable(config) -> {"windows": [...]} for price-based pre-heating.
        self.preheat_provider = None
        self.preheat_kind = None
        self.fireplace_path = Path(data_dir) / "fireplace-state.json"
        try:
            self.fireplace_state = validate_fireplace_state(json.loads(self.fireplace_path.read_text())) \
                if self.fireplace_path.exists() else empty_state()
        except (ValueError, TypeError) as exc:
            raise ControlError("Kunde inte läsa sparade kaminobservationer. Kontrollera fireplace-state.json.") from exc
        self.manual_fireplace_path = Path(data_dir) / "fireplace-manual.json"
        # A press not yet taken over by the fireplace observation (it needs fresh room values).
        self.manual_fireplace = None
        if self.manual_fireplace_path.exists():
            try:
                saved = json.loads(self.manual_fireplace_path.read_text())
                if saved.get("requested") is not None:
                    self.manual_fireplace = number(saved["requested"], "Eldning start")
            except (ValueError, TypeError, KeyError, AttributeError, ControlError):
                LOG.warning("Sparad manuell eldning kunde inte läsas; ignoreras.")
        self.thermostat_path = Path(data_dir) / "thermostat-state.json"
        self.thermostats = json.loads(self.thermostat_path.read_text()) if self.thermostat_path.exists() else {}
        if not isinstance(self.thermostats, dict):
            raise ControlError("Ogiltigt sparat termostattillstånd.")
        for state in self.thermostats.values():
            if not isinstance(state, dict):
                raise ControlError("Ogiltigt sparat termostattillstånd.")
            if "attempted_at" in state:
                state["attempted_at"] = number(state["attempted_at"], "Senaste termostatförsök")
                if state["attempted_at"] < 0:
                    raise ControlError("Ogiltig tid för senaste termostatförsök.")
        self.vacation_path = Path(data_dir) / "vacation-state.json"
        self.vacation = validate_vacation(json.loads(self.vacation_path.read_text())) if self.vacation_path.exists() else None
        self.base_path = Path(data_dir) / "base-correction.json"
        self.base_correction = {"value": 0.0, "updated_at": None}
        if self.base_path.exists():
            try:
                saved_base = json.loads(self.base_path.read_text())
                self.base_correction = {"value": number(saved_base["value"], "Sparad baskorrigering"),
                                        "updated_at": saved_base.get("updated_at")}
            except (ValueError, TypeError, KeyError, ControlError):
                LOG.warning("Sparad baskorrigering kunde inte läsas; börjar om från 0.")
        self.base_saved_at = 0.0
        self.base_seen_at = None
        self.paused_since = None
        self.pause_notified = False
        self.report_error = None
        self.hot_water_written = 0.0
        self.aux_written = 0.0
        # Set while our lower electric heater limit is in force, so it is lifted after errors and restarts.
        self.aux_limit_path = Path(data_dir) / "aux-limit.json"
        self.aux_limited = False
        self.aux_restore = None
        if self.aux_limit_path.exists():
            try:
                saved = json.loads(self.aux_limit_path.read_text())
                if saved.get("limited") is True and isinstance(saved.get("entity_id"), str) and isinstance(saved.get("normal"), str):
                    self.aux_limited, self.aux_restore = True, saved
            except (ValueError, AttributeError, OSError):
                LOG.warning("Sparat elpatronläge kunde inte läsas; ignoreras.")

    def peers(self):
        """Same-device entities per entity, refreshed every 5 minutes."""
        loaded_at, peers = self.peer_cache
        if time.time() - loaded_at >= 300:
            peers = device_peers(self.ha.integration_registry())
            self.peer_cache = (time.time(), peers)
        return peers

    def settings(self):
        # Published config/status objects are replaced, never edited in place.
        # Readers must not wait behind network calls made under the control lock.
        return copy.deepcopy(self.config)

    def snapshot(self):
        return {**copy.deepcopy(self.status), "vacation": copy.deepcopy(self.vacation)}

    def fireplace_observations(self):
        rows = fireplace_intervals(self.fireplace_state)
        if self.manual_fireplace is not None:
            # Heat the user reported is excluded from pump learning just like detected heat.
            rows.append({"start": self.manual_fireplace, "end": None})
        return rows

    def start_manual_fireplace(self):
        """The user says a fire is burning. The observation decides when the heat has faded."""
        with self.lock:
            if not self.config["fireplace_enabled"] or not self.config["fireplace_rooms"]:
                raise ControlError("Slå på braskaminen och välj kaminens rum under Värme från kaminen först.")
            if self.fireplace_state["active"]:
                return {"requested": None, "already_active": True}
            now = time.time()
            if self.manual_fireplace is None:
                atomic_json(self.manual_fireplace_path, {"requested": now})
                self.manual_fireplace = now
                self.events.add("fireplace_manual_start", "Du angav att du eldar. Kaminläget gäller tills kaminrummet "
                                "inte längre är varmare än övriga rum.")
            return {"requested": self.manual_fireplace, "already_active": False}

    def fireplace_step(self, entities, now, peers):
        # Longer limits are opt-in: quiet sensors and missed reports cannot be distinguished here.
        limit = min(self.config["fireplace_max_age_minutes"], self.config["max_age_minutes"])
        config = {**self.config, "max_age_minutes": limit}
        readings = current_readings(config, entities, now, peers)["rooms"] if config["fireplace_enabled"] else []
        manual = self.manual_fireplace if config["fireplace_enabled"] else None
        state, status, events = update_fireplace(self.fireplace_state, self.config, readings, now, manual_at=manual)
        if state != self.fireplace_state:
            atomic_json(self.fireplace_path, state)
            for event in events:
                details = {k: v for k, v in event.items() if k not in ("kind", "message")}
                self.events.add(event["kind"], event["message"], **details)
            self.fireplace_state = state
        if self.manual_fireplace is not None and (state["active"] or manual is None
                                                  or now - manual > MANUAL_FIREPLACE_PENDING_SECONDS):
            if manual is not None and not state["active"]:
                self.events.add("fireplace_manual_dropped", "Jag eldar nu släpptes: aktuella rumsvärden saknades i en halvtimme.")
            atomic_json(self.manual_fireplace_path, {"requested": None})
            self.manual_fireplace = None
        elif self.manual_fireplace is not None and status["phase"] != "active":
            status = {**status, "phase": "active", "manual": True,
                      "message": "Du har angett att du eldar. Väntar på aktuella rumsvärden."}
        return status

    def save(self, config):
        config = validate(config)
        with self.lock:
            if self.vacation and config["offset_entity"] != self.config["offset_entity"]:
                raise ControlError("Avsluta semesterperioden och invänta återställning innan du byter pumpanslutning.")
            if config["enabled"]:
                entities = self.ha.states()
                peers = self.peers()
                result = evaluate(config, entities, time.time(), peers)
                if not result["writable"]:
                    raise ControlError("Ingen skrivbar kurvförskjutning är konfigurerad.")
                if config["thermostat_control_enabled"]:
                    states = {e["entity_id"]: e for e in entities}
                    for key in config["room_sensors"]:
                        thermostat_state(config, states, key, time.time(), peers)
            changes = {key: {"from": self.config[key], "to": value} for key, value in config.items() if value != self.config[key]}
            event_id = self.events.add("settings_attempt", "Begäran att spara inställningar.", changes=changes)
            atomic_json(self.path, config)
            if config["base_offset"] != self.config["base_offset"] and self.base_correction["value"]:
                self.reset_base_correction("Grundvärdet ändrades för hand; den automatiska baskorrigeringen börjar om från 0.")
            if self.aux_limited and (not config["aux_limit_enabled"] or (config["aux_limit_entity"], config["aux_normal_option"])
                                     != (self.aux_restore["entity_id"], self.aux_restore["normal"])):
                # Restore with the old choice before the new one takes over.
                self.lift_aux_limit("inställningen för elpatronen ändrades")
            self.config = config
            self.status = {"message": "Inställningar sparade. Väntar på ny avläsning.", "error_message": None,
                           "enabled": config["enabled"]}
            self.events.add("settings_saved", "Inställningar sparade.", request_id=event_id, changes=changes)

    def reset_base_correction(self, message):
        previous = self.base_correction["value"]
        self.base_correction = {"value": 0.0, "updated_at": time.time()}
        atomic_json(self.base_path, self.base_correction)
        self.events.add("base_correction_reset", message, previous=round(previous, 3))

    def base_step(self, result, now, eligible):
        """Integrate a lasting room deviation into the base offset.

        The room rule alone leaves a steady deviation (whole pump steps, tolerance band). This
        slowly moves the base instead, at most auto_base_limit steps either way. It stands still
        during pre-heating, fireplace heat, tests, vacation, alarms and at the safety limits.
        """
        previous, self.base_seen_at = self.base_seen_at, now
        limit = self.config["auto_base_limit"]
        value = max(-limit, min(limit, self.base_correction["value"]))
        if self.config["auto_base_enabled"] and eligible and previous is not None:
            error = result["error"]
            limit_hit = bool(result["control_limit"]) and ((result["control_limit"]["direction"] == "upper") == (error > 0))
            if abs(error) >= AUTO_BASE_NOISE and not limit_hit:
                value = max(-limit, min(limit, value + error * min(now - previous, AUTO_BASE_MAX_GAP) * AUTO_BASE_RATE))
        if abs(value - self.base_correction["value"]) > 1e-9:
            self.base_correction = {"value": value, "updated_at": now}
            if now - self.base_saved_at >= AUTO_BASE_SAVE_SECONDS:
                atomic_json(self.base_path, self.base_correction)
                self.base_saved_at = now
        return {"enabled": self.config["auto_base_enabled"], "value": round(value, 3) + 0.0, "limit": limit}

    def warnings(self, result):
        """Things worth knowing that do not stop control."""
        names = {room["entity_id"]: room.get("name") or room["entity_id"] for room in result.get("rooms") or []}
        notes = [f"{names.get(room['entity_id'], room['entity_id'])} hoppas över: {room['error']}"
                 for room in result.get("skipped_rooms") or []]
        if result.get("indoor_fallback"):
            notes.append("Alla prioriterade rumsgivare är tysta; styr tillfälligt på dina övriga valda rum.")
        if not self.config["thermostat_control_enabled"]:
            controlling = control_rooms(self.config)
            for key in self.config["room_sensors"]:
                if key.startswith("climate."):
                    notes.append(f"{names.get(key, key)} är en termostat som mäter nära elementet och kan visa för varmt; "
                                 + ("den styr värmen." if key in controlling else "den räknas in i husets snitt."))
        if result.get("price_error") and (self.config["cheap_boost"] or self.config["expensive_reduction"]):
            notes.append(result["price_error"])
        return notes

    def tick(self):
        try:
            self.control_tick()
        finally:
            self.report(time.time())

    def report(self, now):
        """Tell Home Assistant about long pauses and publish status sensors. Never affects control."""
        with self.lock:
            status = self.snapshot()
            config = copy.deepcopy(self.config)
        error = status.get("error_message") if config["enabled"] else None
        try:
            if error:
                if self.paused_since is None:
                    self.paused_since = now
                minutes = config["pause_notify_minutes"]
                if minutes and not self.pause_notified and now - self.paused_since >= minutes * 60:
                    message = (f"Styrningen har varit pausad i {round((now - self.paused_since) / 60)} minuter: {error} "
                               "Pumpen behåller sin nuvarande kurvförskjutning.")
                    self.notify("NIBE Comfort pausad", message, config, create=True)
                    self.pause_notified = True
                    self.events.add("pause_notified", "Avisering skickad om lång paus.", error=error)
            else:
                if self.pause_notified:
                    self.notify("NIBE Comfort igång igen", "Giltiga mätvärden igen; styrningen fortsätter.", config, create=False)
                self.paused_since = None
                self.pause_notified = False
            self.publish(status, config)
            self.report_error = None
        except Exception as exc:  # reporting is optional; control has already finished this round
            if str(exc) != self.report_error:
                LOG.warning("Kunde inte rapportera till Home Assistant: %s", exc)
                self.report_error = str(exc)

    def hot_water_step(self, entities, now, result, preheat, hold):
        """Opt-in: the pump hot water mode follows the price. Leaves modes it does not manage alone."""
        config = self.config
        if not config["hot_water_price_enabled"]:
            return {"enabled": False}
        cheap, normal = config["hot_water_cheap_option"], config["hot_water_normal_option"]
        reason = ("förvärmer före dyr el" if preheat.get("kind") == "preheat"
                  else "billig el" if result.get("price_level") == "cheap" else None)
        desired = cheap if reason else normal
        report = {"enabled": True, "entity_id": config["hot_water_mode_entity"], "desired": desired,
                  "reason": reason or "inte billig el", "current": None, "note": None, "error": None, "next_change_at": None}
        state = next((e for e in entities if e["entity_id"] == config["hot_water_mode_entity"]), None)
        if state is None or state.get("state") in ("unavailable", "unknown", None):
            report["error"] = "Pumpens varmvattenläge kan inte läsas just nu."
            return report
        current = report["current"] = state["state"]
        options = (state.get("attributes") or {}).get("options") or []
        missing = [option for option in (cheap, normal) if option not in options]
        if missing:
            offered = ", ".join(map(str, options))
            report["error"] = f"Läget {missing[0]} finns inte. Pumpen erbjuder: {offered}."
            return report
        if current not in (cheap, normal):
            report["note"] = f"Läget {current} är valt för hand och lämnas orört."
            return report
        if not config["enabled"] or hold:
            report["note"] = "Övervakning eller paus: varmvattenläget ändras inte."
            return report
        if current == desired:
            return report
        wait = HOT_WATER_MIN_SECONDS - (now - self.hot_water_written)
        if wait > 0:
            report["next_change_at"] = now + wait
            return report
        self.hot_water_written = now
        try:
            self.ha.set_select(config["hot_water_mode_entity"], desired)
        except ControlError as exc:
            report["error"] = f"Varmvattenläget kunde inte ändras: {exc}"
            self.events.add("hot_water_failed", report["error"], entity_id=config["hot_water_mode_entity"], value=desired)
            return report
        self.events.add("hot_water_mode", f"Varmvatten {current} → {desired} ({report['reason']}).",
                        entity_id=config["hot_water_mode_entity"], previous=current, value=desired)
        report["current"] = desired
        return report

    def store_aux_limited(self, plan):
        """plan is where to restore the full limit, or None once it is restored."""
        atomic_json(self.aux_limit_path, plan or {"limited": False})
        self.aux_limited = plan is not None
        self.aux_restore = plan

    def lift_aux_limit(self, reason, timeout=SERVICE_TIMEOUT_SECONDS):
        """Give the electric heater its usual limit back. Safe to call any time; a no-op unless we lowered it."""
        if not self.aux_limited:
            return None
        plan = self.aux_restore
        try:
            self.ha.set_select(plan["entity_id"], plan["normal"], timeout=timeout)
        except ControlError as exc:
            message = f"Elpatronens vanliga max eltillsats kunde inte återställas: {exc}"
            self.events.add("aux_limit_failed", message, entity_id=plan["entity_id"], value=plan["normal"])
            return message
        self.store_aux_limited(None)
        self.events.add("aux_limit", f"Elpatronen får sin vanliga max eltillsats {plan['normal']} igen ({reason}).",
                        entity_id=plan["entity_id"], value=plan["normal"])
        return None

    def aux_limit_step(self, entities, now, result, preheat, hold):
        """Opt-in: lower the pump's electric heater limit in expensive hours, never while the house is cold."""
        config = self.config
        if not config["aux_limit_enabled"]:
            self.lift_aux_limit("begränsningen är avstängd")
            return {"enabled": False}
        entity_id, limited, normal = config["aux_limit_entity"], config["aux_limit_option"], config["aux_normal_option"]
        reason = None
        if preheat.get("kind") != "preheat":
            reason = ("dyr period efter förvärmning" if preheat.get("kind") == "coast"
                      else "dyr el" if result.get("price_level") == "expensive" else None)
        guard = None
        indoor, outdoor = result.get("indoor"), result.get("outdoor")
        if reason and (indoor is None or indoor < config["target_temperature"] - config["aux_limit_max_drop"]):
            guard = "För kallt inne: elpatronen får full effekt."
        elif reason and outdoor is not None and outdoor < config["aux_limit_min_outdoor"]:
            guard = "För kallt ute: elpatronen får full effekt."
        desired = limited if reason and not guard else normal
        report = {"enabled": True, "entity_id": entity_id, "desired": desired, "limited": desired == limited,
                  "reason": guard or reason or "inte dyr el", "current": None, "note": None, "error": None, "next_change_at": None}
        state = next((e for e in entities if e["entity_id"] == entity_id), None)
        if state is None or state.get("state") in ("unavailable", "unknown", None):
            report["error"] = "Pumpens max eltillsats kan inte läsas just nu."
            return report
        current = report["current"] = state["state"]
        options = (state.get("attributes") or {}).get("options") or []
        missing = [option for option in (limited, normal) if option not in options]
        if missing:
            report["error"] = f"Läget {missing[0]} finns inte. Pumpen erbjuder: {', '.join(map(str, options))}."
            return report
        if current not in (limited, normal):
            report["note"] = f"Max eltillsats {current} är vald för hand och lämnas orörd."
            if self.aux_limited:
                self.store_aux_limited(None)
            return report
        if not config["enabled"] or hold:
            report["note"] = "Övervakning eller paus: elpatronen begränsas inte."
            if current == limited and self.aux_limited:
                report["error"] = self.lift_aux_limit("övervakning eller paus")
                if not report["error"]:
                    report["current"] = normal
            return report
        if current == desired:
            if current == normal and self.aux_limited:
                self.store_aux_limited(None)
            return report
        if desired == limited:
            wait = AUX_LIMIT_MIN_SECONDS - (now - self.aux_written)
            if wait > 0:
                report["next_change_at"] = now + wait
                return report
        self.aux_written = now
        try:
            self.ha.set_select(entity_id, desired)
        except ControlError as exc:
            report["error"] = f"Max eltillsats kunde inte ändras: {exc}"
            self.events.add("aux_limit_failed", report["error"], entity_id=entity_id, value=desired)
            return report
        self.store_aux_limited({"limited": True, "entity_id": entity_id, "normal": normal} if desired == limited else None)
        self.events.add("aux_limit", f"Max eltillsats {current} → {desired} ({report['reason']}).",
                        entity_id=entity_id, previous=current, value=desired)
        report["current"] = desired
        return report

    def publish(self, status, config):
        if config["publish_sensors"]:
            for entity_id, state, attrs in sensor_states(status, config):
                self.ha.request(f"/states/{entity_id}", {"state": state, "attributes": attrs})

    def notify(self, title, message, config, create):
        if create:
            self.ha.request("/services/persistent_notification/create",
                            {"notification_id": PAUSE_NOTIFICATION_ID, "title": title, "message": message})
        else:
            self.ha.request("/services/persistent_notification/dismiss", {"notification_id": PAUSE_NOTIFICATION_ID})
        if config["notify_service"]:
            domain, service = config["notify_service"].split(".", 1)
            self.ha.request(f"/services/{domain}/{service}", {"title": title, "message": message})

    def test_notify(self, service):
        """Send a test message to a chosen, possibly unsaved, notify service."""
        if not isinstance(service, str) or not re.fullmatch(r"notify\.[a-z0-9_]+", service):
            raise ControlError("Välj en notify-tjänst först.")
        if service not in [s["service"] for s in self.ha.notify_services()]:
            raise ControlError(f"{service} finns inte i Home Assistant.")
        self.ha.request(f"/services/notify/{service.split('.', 1)[1]}",
                        {"title": "NIBE Comfort", "message": "Testavisering. Hit kommer meddelandet om styrningen pausas."},
                        timeout=SERVICE_TIMEOUT_SECONDS)
        return {"sent": service}

    def pause_alarm_thermostats(self):
        """Pause our limiter only; never reset the pump or open valves."""
        with self.lock:
            entities = self.ha.states()
            peers = self.peers()
            alarm = pump_alarm(entities, self.config, peers, time.time())
            if alarm["error"] or alarm["active"] is not True or alarm["code"] != 229:
                raise ControlError("Åtgärden kräver ett aktuellt larm 229 på vald pump. Uppdatera larmvyn.")
            if not self.config["thermostat_control_enabled"]:
                return copy.deepcopy(self.config)
            config = {**self.config, "thermostat_control_enabled": False}
            atomic_json(self.path, config)
            self.config = config
            self.events.add("settings_saved", "Termostatbegränsning pausad vid larm 229. Befintliga börvärden och ventiler är oförändrade.",
                            changes={"thermostat_control_enabled": {"from": True, "to": False}})
            return copy.deepcopy(config)

    def thermostat_step(self, entities, now, peers, testing, alarm_hold=False):
        result = {"enabled": self.config["thermostat_control_enabled"], "rooms": []}
        if not result["enabled"]:
            return result
        states = {e["entity_id"]: e for e in entities}
        for key in self.config["room_sensors"]:
            row = {"entity_id": key, "message": "", "error": None}
            result["rooms"].append(row)
            state = self.thermostats.get(key, {})
            try:
                current, setpoint, target = thermostat_state(self.config, states, key, now, peers)
                if states[key]["attributes"].get("target_temp_step") is None:
                    row["warning"] = "Steglängd rapporteras inte. Målet skickas utan avrundning och måste bekräftas genom återläsning."
                if state.get("last_error"):
                    self.events.add("thermostat_resumed", "Giltiga termostatvärden igen.", entity_id=key)
                    state.pop("last_error")
                    atomic_json(self.thermostat_path, self.thermostats)
                awaiting = state.get("pending") or state.get("outcome") == "unconfirmed"
                if awaiting and abs(setpoint - state["value"]) < 1e-6:
                    self.events.add("thermostat_observed", "Termostatens nya börvärde återläst.",
                                    entity_id=key, value=setpoint, request_id=state["request_id"])
                    state.update(pending=False, outcome="confirmed")
                    atomic_json(self.thermostat_path, self.thermostats)
                elif state.get("pending") and now - state["attempted_at"] >= 900:
                    self.events.add("thermostat_unconfirmed", "Termostatens ändring kunde inte bekräftas. Återläser före eventuellt nytt försök.",
                                    entity_id=key, value=state["value"], request_id=state["request_id"])
                    state.update(pending=False, outcome="unconfirmed")
                    atomic_json(self.thermostat_path, self.thermostats)
                elif state.get("pending"):
                    row["message"] = "Ändring skickad; väntar på återläsning."
                    if state.get("outcome") == "failed":
                        row["error"] = "Serviceanropet misslyckades; kan ändå ha tagits emot. Väntar på återläsning."
                    continue
                if state.get("outcome") in ("failed", "unconfirmed"):
                    row["error"] = "Senaste ändringen kunde inte bekräftas. Kontrollera termostaten i Home Assistant."
                if current <= self.config["target_temperature"] + 0.2:
                    if state.get("latched") and now - state["attempted_at"] >= 1800:
                        state["latched"] = False
                        atomic_json(self.thermostat_path, self.thermostats)
                    row["message"] = "Ingen övertemperatur. Börvärdet lämnas orört."
                    continue
                if current < self.config["target_temperature"] + 0.5:
                    row["message"] = "Under gränsen för begränsning (+0,5 °C)."
                    continue
                if setpoint <= target + 1e-6:
                    row["message"] = "Börvärdet är redan vid eller under målet; höjs inte."
                    continue
                if not self.config["enabled"] or testing or alarm_hold:
                    row["message"] = ("Väntar: pumpen rapporterar larm 229; befintligt börvärde lämnas orört."
                                      if alarm_hold else "Väntar: automatiken är avstängd eller pumptest pågår.")
                    continue
                remaining = max(0, 1800 - (now - state.get("attempted_at", 0)))
                retry = state.get("outcome") in ("failed", "unconfirmed")
                if retry and state.get("latched") and (
                    state.get("previous") != setpoint or state.get("value") != target
                ):
                    row["message"] = "Inget återförsök: börvärdet eller målet har ändrats, eller tidigare börvärde saknas. Kontrollera termostaten."
                    continue
                if state.get("latched") and not retry:
                    row["message"] = "Redan begränsad under denna varma period; väntar tills rummet svalnat."
                    continue
                if remaining > 0:
                    row["message"] = (
                        f"Nytt försök tidigast om {math.ceil(remaining / 60)} min; återläser först."
                        if retry else
                        f"Väntar {math.ceil(remaining / 60)} min: minsta intervall 30 min från senaste styrförsök."
                    )
                    continue
                request_id = self.events.add("thermostat_attempt", "Begränsar övertemperatur: sätter termostatens börvärde till rumsmålet.",
                                            entity_id=key, previous=setpoint, value=target, current=current,
                                            retry_of=state.get("request_id") if retry else None,
                                            target_temperature=self.config["target_temperature"],
                                            reported_step=states[key]["attributes"].get("target_temp_step"),
                                            unit=states[key]["attributes"]["temperature_unit"])
                state = {"attempted_at": now, "value": target, "previous": setpoint, "request_id": request_id,
                         "latched": True, "pending": True, "outcome": "sent"}
                self.thermostats[key] = state
                atomic_json(self.thermostat_path, self.thermostats)
                try:
                    self.ha.set_thermostat(key, target)
                except ControlError as exc:
                    state["outcome"] = "failed"
                    atomic_json(self.thermostat_path, self.thermostats)
                    self.events.add("thermostat_failed", "Termostatändringen misslyckades; kan ändå ha tagits emot.",
                                    entity_id=key, request_id=request_id, error=str(exc))
                    raise
                self.events.add("thermostat_sent", "Home Assistant accepterade termostatändringen; inväntar återläsning.",
                                entity_id=key, request_id=request_id, value=target)
                row["error"] = None
                row["message"] = "Ändring skickad; väntar på återläsning."
            except ControlError as exc:
                row["error"] = str(exc)
                if state.get("last_error") != str(exc):
                    self.events.add("thermostat_paused", "Termostatbegränsning pausad för rummet.", entity_id=key, error=str(exc))
                    self.thermostats.setdefault(key, {})["last_error"] = str(exc)
                    atomic_json(self.thermostat_path, self.thermostats)
        return result

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

    def store_vacation(self, plan):
        # Replaced, never edited in place: snapshot() reads it without the lock.
        atomic_json(self.vacation_path, plan or {})
        self.vacation = plan

    def vacation_entity(self):
        """The pump's Smart Home Mode selector: on the same device as the offset, with a vacation option."""
        offset = self.config["offset_entity"]
        if not offset:
            raise ControlError("Ingen kurvförskjutning är vald. Välj din pump under Inställningar först.")
        selectors = [key for key in self.peers().get(offset, ())
                     if key.startswith("select.") and key.endswith(VACATION_MODE_SUFFIX)]
        if len(selectors) != 1:
            raise ControlError("Pumpens Smart Home Mode hittades inte entydigt i Home Assistant.")
        entity = next((e for e in self.ha.states() if e["entity_id"] == selectors[0]), None)
        if not entity or VACATION_MODE not in entity["attributes"].get("options", ()):
            raise ControlError("Pumpens semesterläge är inte tillgängligt just nu. Försök igen om en stund.")
        return selectors[0]

    def schedule_vacation(self, start, end):
        """Plan a vacation. A running vacation keeps its start; only the end moves."""
        with self.lock:
            if self.test and self.test["phase"] in ("running", "restoring"):
                raise ControlError("Avsluta pumptestet och invänta återställning innan du planerar semester.")
            now = time.time()
            plan = self.vacation
            start = now if start is None else max(now, number(start, "Starttid"))
            end = number(end, "Sluttid")
            if plan and plan["phase"] == "restoring":
                raise ControlError("Semesterläget håller på att avslutas. Försök igen om en stund.")
            active = bool(plan and plan["phase"] == "active")
            if active:
                start = plan["start"]
            if end <= max(now, start):
                raise ControlError("Sluttiden måste ligga efter starttiden och framåt i tiden.")
            if end - now > VACATION_MAX_SECONDS:
                raise ControlError("Semesterperioden måste sluta inom ett år.")
            if active:
                plan = {**plan, "end": end}
            else:
                plan = {"phase": "scheduled", "entity_id": self.vacation_entity(), "start": start, "end": end,
                        "previous": None, "confirmed": False, "attempted_at": 0, "error": None}
            self.store_vacation(plan)
            self.events.add("vacation_scheduled", "Semesterperiod sparad.", start=plan["start"], end=end)
            return copy.deepcopy(plan)

    def cancel_vacation(self):
        with self.lock:
            plan = self.vacation
            if not plan or plan["phase"] == "restoring":
                raise ControlError("Ingen semesterperiod att avbryta.")
            if plan["phase"] == "scheduled":
                self.store_vacation(None)
                self.events.add("vacation_cancelled", "Planerad semesterperiod borttagen.")
                return None
            # The next tick restores the pump, the same way as at the planned end.
            plan = {**plan, "end": time.time()}
            self.store_vacation(plan)
            self.events.add("vacation_cancelled", "Semesterläget avbryts; pumpen återställs vid nästa avläsning.")
            return copy.deepcopy(plan)

    def vacation_paused(self):
        """Comfort control only yields once the pump has really confirmed vacation mode."""
        plan = self.vacation
        return bool(plan and (plan["phase"] == "restoring" or plan["confirmed"]))

    def vacation_write(self, plan, option, now):
        if now - plan["attempted_at"] < VACATION_RETRY_SECONDS:
            return
        retry = plan["attempted_at"] > 0
        plan = {**plan, "attempted_at": now}
        self.store_vacation(plan)
        try:
            self.ha.set_select(plan["entity_id"], option)
        except ControlError as exc:
            # The cloud write may still land: read back and retry instead of giving up.
            if plan["error"] != str(exc):
                self.events.add("vacation_failed", "Pumpens läge kunde inte ändras; försöker igen.",
                                entity_id=plan["entity_id"], value=option, error=str(exc))
            self.store_vacation({**plan, "error": str(exc)})
            return
        self.store_vacation({**plan, "error": "Pumpen har inte bekräftat ändringen ännu; försöker igen." if retry else None})

    def vacation_step(self, entities, now):
        """Follow the vacation plan. Runs before evaluate so failing room sensors cannot block the restore."""
        plan = self.vacation
        if not plan:
            return
        states = {e["entity_id"]: e for e in entities}
        try:
            entity = fresh_entity(states, plan["entity_id"], self.config["max_age_minutes"], now,
                                  self.peers().get(plan["entity_id"], ()))
            mode = entity["state"]
        except ControlError as exc:
            mode = None
            if plan["error"] != str(exc):
                plan = {**plan, "error": str(exc)}
                self.store_vacation(plan)
        if plan["phase"] == "scheduled":
            if now >= plan["end"]:
                self.store_vacation(None)
                self.events.add("vacation_missed", "Semesterperioden passerade utan att den kunde startas.")
                return
            if now < plan["start"] or mode is None:
                return
            if self.test and self.test["phase"] in ("running", "restoring"):
                self.store_vacation({**plan, "error": "Väntar tills pumptestet är återställt."})
                return
            if VACATION_MODE not in entity["attributes"].get("options", ()):
                self.store_vacation({**plan, "error": "Pumpen erbjuder inte semesterläge just nu."})
                return
            plan = {**plan, "phase": "active", "previous": None if mode == VACATION_MODE else mode}
            self.store_vacation(plan)
            self.events.add("vacation_started", "Semesterperioden börjar.", entity_id=plan["entity_id"], previous=mode)
        if plan["phase"] == "active":
            if now < plan["end"]:
                if mode == VACATION_MODE and not plan["confirmed"]:
                    self.store_vacation({**plan, "confirmed": True, "error": None})
                    self.events.add("vacation_confirmed", "Semesterläge återläst från pumpen. Automatiken pausas tills perioden är slut.")
                elif mode is not None and not plan["confirmed"]:
                    self.vacation_write(plan, VACATION_MODE, now)
                elif mode is not None and plan["error"]:
                    self.store_vacation({**plan, "error": None})
                return
            plan = {**plan, "phase": "restoring", "attempted_at": 0, "error": None}
            self.store_vacation(plan)
            self.events.add("vacation_ended", "Semesterperioden är slut. Inväntar återställning före vanlig styrning.",
                            previous=plan["previous"])
        if mode is None:
            return
        previous = plan["previous"]
        if mode == VACATION_MODE and previous is not None:
            if previous not in entity["attributes"].get("options", ()):
                self.store_vacation({**plan, "error": "Pumpens tidigare läge kan inte väljas just nu. Kontrollera pumpen."})
                return
            self.vacation_write(plan, previous, now)
            return
        self.store_vacation(None)
        self.events.add("vacation_done", (
            "Pumpen stod redan i semesterläge vid starten och lämnades orörd." if previous is None else
            "Pumpens läge är återställt." if mode == previous else
            "Pumpens läge ändrades av något annat och lämnades orört."
        ), previous=previous, observed=mode)

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
            alarm = pump_alarm(self.ha.states(), self.config, self.peers(), time.time())
            if alarm["active"] is True:
                raise ControlError("Pumpen har ett aktivt larm. Åtgärda larmet innan du startar ett pumptest.")
            if self.vacation:
                raise ControlError("Avsluta semesterperioden och invänta återställning innan du startar ett pumptest.")
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

    def control_tick(self):
        schedule = self.preheat_schedule()
        with self.lock:
            now = time.time()
            readings = {}
            result = {}
            fireplace = None
            try:
                entities = self.ha.states()
                now = time.time()
                offset = next((e for e in entities if e["entity_id"] == self.config["offset_entity"]), None)
                unavailable = offset is not None and offset["state"] == "unavailable"
                if not unavailable:
                    self.outage_since = None
                self.vacation_step(entities, now)
                peers = self.peers()
                readings = current_readings(self.config, entities, now, peers)
                alarm = pump_alarm(entities, self.config, peers, now)
                alarm_hold = alarm["active"] is True and alarm["code"] == 229 and not alarm["error"]
                fireplace = self.fireplace_step(entities, now, peers)
                if unavailable:
                    # Do not silently drop a missing controlling room during a pump outage.
                    states = {e["entity_id"]: e for e in entities}
                    indoor_temperature(self.config, lambda key: fresh_entity(
                        states, key, self.config["max_age_minutes"], now, peers.get(key, ())))
                    if self.outage_since is None:
                        self.outage_since = now
                    if now - self.outage_since < OUTAGE_GRACE_SECONDS and not self.last_error:
                        self.status = {
                            **readings, "fireplace": fireplace, "outage_since": self.outage_since,
                            "message": OUTAGE_MESSAGE, "error_message": None, "checked_at": now,
                            "enabled": self.config["enabled"], "settings": copy.deepcopy(self.config),
                            "desired_offset": None, "next_offset": None, "contributions": None,
                            "writable": False, "pending_write": copy.deepcopy(self.pending),
                        }
                        return
                self.test_step(entities, now)
                self.observe(entities, now, peers)
                if self.config["preheat_enabled"]:
                    try:
                        price = fresh_entity({e["entity_id"]: e for e in entities}, self.config["price_entity"],
                                             self.config["max_age_minutes"], now)
                        number(price["state"], "Aktuellt elpris")
                    except ControlError as exc:
                        schedule = {"error": str(exc)}
                preheat = self.preheat(now, schedule)
                rooms = tuple(control_rooms(self.config))
                if rooms != self.indoor_rooms:
                    self.indoor_rooms, self.indoor_samples = rooms, []
                self.indoor_samples = [(t, v) for t, v in self.indoor_samples if now - SMOOTHING_SECONDS < t < now]
                result = evaluate(self.config, entities, now, peers, preheat["shift"],
                                  fireplace_active=fireplace["phase"] == "active", indoor_samples=self.indoor_samples,
                                  base_correction=self.base_correction["value"])
                self.indoor_samples.append((now, result.pop("indoor_now")))
                result["preheat"] = preheat
                result.update(readings)
                vacation = self.vacation_paused()
                testing_now = bool(self.test and self.test["phase"] in ("running", "restoring"))
                result["base_correction"] = self.base_step(
                    result, now, self.config["enabled"] and result["writable"] and not (testing_now or vacation or alarm_hold)
                    and not preheat["kind"] and fireplace["phase"] != "active" and not result["indoor_fallback"])
                result["warnings"] = self.warnings(result)
                if alarm_hold and result["writable"]:
                    result["next_offset"] = result["current_offset"]
                if vacation and result["writable"]:
                    result["next_offset"] = result["current_offset"]
                fireplace["contribution"] = result["contributions"]["fireplace"]
                fireplace["control_note"] = (
                    "Extra kaminbidrag −1 steg i beräknad kurvförskjutning. Vanliga intervall, steg och gränser gäller."
                    if fireplace["contribution"] else
                    "Ingen extra kaminsänkning: det styrande snittet är under ditt vanliga mål minus toleransen."
                    if fireplace["phase"] == "active" else
                    "Ingen extra kaminsänkning utan aktuell, bestående misstänkt extravärme."
                )
                if not self.config["enabled"]:
                    fireplace["control_note"] += " Övervakning: inga automatiska ändringar skickas."
                result["fireplace"] = fireplace
                if self.last_error:
                    self.events.add("resumed", "Giltiga mätvärden igen; styrningen kan fortsätta.")
                    self.last_error = None
                remaining = max(0, self.config["min_interval_minutes"] * 60 - (now - self.last_write))
                result.update(
                    error_message=None, checked_at=now, enabled=self.config["enabled"],
                    settings=copy.deepcopy(self.config),
                    wait_seconds=math.ceil(remaining),
                    message="Övervakning: inga ändringar skickas.",
                )
                testing = bool(self.test and self.test["phase"] in ("running", "restoring"))
                self.status = {**copy.deepcopy(result), "message": "Avläst. Kontrollerar styrning och återläsning.",
                               "control_in_progress": True}
                result["pump_alarm"] = alarm
                result["thermostat_control"] = self.thermostat_step(entities, now, peers, testing or vacation, alarm_hold=alarm_hold)
                result["hot_water"] = self.hot_water_step(entities, now, result, preheat, testing or vacation or alarm_hold)
                result["aux_limit"] = self.aux_limit_step(entities, now, result, preheat, testing or vacation or alarm_hold)
                if testing:
                    result["message"] = "Test av styrningen pågår: automatiken väntar tills testet är klart."
                elif alarm_hold:
                    result["message"] = "Larm 229: kurv- och termostatstyrningen väntar. Kontrollera flödet och pumpens larm innan återställning."
                elif vacation:
                    result["message"] = "Semesterläge: pumpen sköter värmen och automatiken väntar tills perioden är slut."
                elif self.config["enabled"]:
                    result["message"] = "Ingen ändring behövs."
                    if result["control_limit"]:
                        upper = result["control_limit"]["direction"] == "upper"
                        result["message"] = (
                            "Övre säkerhetsgränsen nådd: mer värme begärs men högre förskjutning tillåts inte."
                            if upper else
                            "Nedre säkerhetsgränsen nådd: lägre förskjutning begärs men tillåts inte."
                        )
                    if abs(result["next_offset"] - result["current_offset"]) > 1e-6:
                        result["message"] = "Väntar på nästa tillåtna ändring."
                        if remaining == 0:
                            request_id = self.events.add(
                                "write_attempt", "Automatiken begär ändrad kurvförskjutning.",
                                entity_id=self.config["offset_entity"], previous=result["current_offset"],
                                value=result["next_offset"], target_temperature=self.config["target_temperature"],
                                indoor=result["indoor"], indoor_smoothed=result["indoor_smoothed"],
                                outdoor=result["outdoor"], price=result["price"],
                                price_unit=next((e["attributes"].get("unit_of_measurement") for e in entities
                                                 if e["entity_id"] == self.config["price_entity"]), None),
                                contributions=result["contributions"], desired_offset=result["desired_offset"],
                                target_shift=result["target_shift"], preheat=preheat.get("kind"),
                                fireplace=fireplace["phase"], fireplace_rooms=fireplace["rooms"],
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
                                pending["error"] = str(exc)
                                atomic_json(self.state_path, {"last_attempt": now, "pending": pending})
                                self.events.add("write_failed", "Skrivförsöket misslyckades; pumpen kan ändå ha tagit emot det.", request_id=request_id, error=str(exc))
                                raise
                            self.events.add("write_sent", "Home Assistant accepterade serviceanropet; inväntar återläsning.", request_id=request_id)
                            result["message"] = "Ändring skickad till Home Assistant; inväntar återläsning."
                            result["wait_seconds"] = math.ceil(self.config["min_interval_minutes"] * 60)
                if self.pending:
                    result["pending_write"] = copy.deepcopy(self.pending)
                    if self.pending.get("error") and not alarm_hold:
                        result["message"] = "Tidigare styrförsök misslyckades. Återläser; nytt försök efter minsta ändringsintervall om ändringen fortfarande behövs."
                self.status = result
            except ControlError as exc:
                if self.last_error != str(exc):
                    self.events.add("paused", "Styrningen pausad på grund av fel.", error=str(exc))
                    self.last_error = str(exc)
                self.status = {
                    **result, **readings,
                    "fireplace": fireplace,
                    "error_message": str(exc), "checked_at": now,
                    "settings": copy.deepcopy(self.config),
                    "pending_write": copy.deepcopy(self.pending),
                    "wait_seconds": math.ceil(max(0, self.config["min_interval_minutes"] * 60 - (time.time() - self.last_write))),
                    "enabled": self.config["enabled"],
                    "message": "Styrningen pausad: inga nya ändringar skickas.",
                }
                raise
