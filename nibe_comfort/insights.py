"""Read-only weather, history and pump telemetry from Home Assistant."""

import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from controller import ControlError, fresh_entity, next_step, number, price_contribution, temperature


def discover_tibber(states, registry):
    by_id = {s["entity_id"]: s for s in states}
    candidates = []
    for item in registry:
        entity = by_id.get(item["entity_id"])
        if item.get("platform") != "tibber" or not entity or not item["entity_id"].startswith("sensor."):
            continue
        unit = entity["attributes"].get("unit_of_measurement", "").lower()
        if "/kwh" in unit or "/mwh" in unit:
            candidates.append(item["entity_id"])
    candidates = sorted(set(candidates))
    return {
        "price_entity": candidates[0] if len(candidates) == 1 else "",
        "price_message": (
            "Tibbers aktuella elpris hittades automatiskt."
            if len(candidates) == 1 else
            "Flera Tibber-prisgivare finns. Välj rätt hem manuellt."
            if candidates else "Ingen Tibber-prisgivare hittades. Välj prisgivare manuellt."
        ),
    }


LABELS = {
    "_status_compressor": ("compressor", "Kompressor"),
    "_current_compressor_frequency": ("frequency", "Kompressorfrekvens"),
    "_alarm": ("alarm", "Pumplarm"),
    "_status": ("operation", "Driftläge"),
    "_int_elec_add_heat": ("additional_heat", "Eltillsats"),
    "_supply_line_bt2": ("supply", "Framledning"),
    "_return_line_bt3": ("return", "Returledning"),
    "_hot_water_top_bt7": ("hot_water", "Varmvatten"),
    "_current_outd_temp_bt1": ("outdoor", "Utomhus · NIBE BT1"),
}
TRANSLATIONS = {
    "Operating": "I drift", "Stopped": "Stoppad", "Off": "Av",
    "Heating": "Värme", "Hot water": "Varmvatten", "Blocked": "Blockerad",
    "Idle": "Vilar", "Standby": "Viloläge",
}


def reading(entity, label, max_age, now, peers=()):
    result = {
        "entity_id": entity["entity_id"], "label": label, "value": None,
        "unit": entity["attributes"].get("unit_of_measurement", ""),
        "reported_at": entity.get("last_reported") or entity.get("last_updated"),
        "error": None,
    }
    try:
        fresh_entity(peers_states(entity, peers), entity["entity_id"], max_age, now, [p["entity_id"] for p in peers])
        result["value"] = entity["state"]
    except ControlError as exc:
        result["error"] = str(exc)
    return result


def peers_states(entity, peers):
    states = {p["entity_id"]: p for p in peers}
    states[entity["entity_id"]] = entity
    return states


def overview(states, registry, config, now):
    by_id = {s["entity_id"]: s for s in states}
    selected = next((r for r in registry if r["entity_id"] == config["offset_entity"]
                     and r.get("platform") == "myuplink"), None)
    pump_ids = {
        r["entity_id"] for r in registry if selected and selected["device_id"]
        and r.get("platform") == "myuplink" and r["device_id"] == selected["device_id"]
    }
    pump = []
    pump_states = [by_id[e] for e in sorted(pump_ids) if e in by_id]
    for suffix, (key, label) in LABELS.items():
        matches = [by_id[e] for e in pump_ids if e in by_id and e.endswith(suffix)
                   and e.startswith(("sensor.", "binary_sensor."))]
        if len(matches) != 1:
            pump.append({"key": key, "label": label, "value": None, "error": "Ingen entydig givare hittades."})
            continue
        # myUplink only reports changed values; other values from the same pump prove it is still online.
        item = reading(matches[0], label, config["max_age_minutes"], now, pump_states)
        item["key"] = key
        item["active"] = None
        if item["value"] is not None:
            if key == "alarm":
                item["active"] = {"on": True, "off": False}.get(item["value"])
                item["value"] = {"on": "Larm aktivt", "off": "Inget aktivt larm"}.get(item["value"], item["value"])
            else:
                item["value"] = TRANSLATIONS.get(item["value"], item["value"])
        pump.append(item)
    pump_electricity = []
    for eid in sorted(pump_ids):
        entity = by_id.get(eid)
        if not entity:
            continue
        attrs = entity["attributes"]
        text = (eid + " " + attrs.get("friendly_name", "")).lower()
        if attrs.get("device_class") in ("power", "energy") and any(
            word in text for word in ("consumption", "consumed", "electrical", "electricity", "electric power", "energy_spent")
        ):
            pump_electricity.append(reading(entity, attrs.get("friendly_name", eid), config["max_age_minutes"], now, pump_states))
    tibber_ids = {r["entity_id"] for r in registry if r.get("platform") == "tibber"}
    house = []
    for device_class, label in (("power", "Husets effekt just nu"), ("energy", "Husets förbrukning i dag")):
        candidates = []
        for eid in tibber_ids:
            entity = by_id.get(eid)
            if not entity or entity["attributes"].get("device_class") != device_class:
                continue
            # Exclude production, averages, meter totals and current-hour estimates.
            if (device_class == "power" and eid.startswith("sensor.power_") and not eid.startswith("sensor.power_production_")
                    or device_class == "energy" and eid.startswith("sensor.accumulated_consumption_")
                    and not eid.startswith("sensor.accumulated_consumption_current_hour_")):
                candidates.append(entity)
        if len(candidates) == 1:
            house.append(reading(candidates[0], label, config["max_age_minutes"], now))
        else:
            house.append({"label": label, "value": None, "error": "Saknad eller flertydig Tibber-mätare."})
    price = None
    price_id = config["price_entity"] or discover_tibber(states, registry)["price_entity"]
    if price_id in by_id:
        price = reading(by_id[price_id], "Aktuellt elpris", config["max_age_minutes"], now)
    return {
        "checked_at": now, "pump": pump, "pump_electricity": pump_electricity,
        "house": house, "price": price,
        "electricity_note": "Husets Tibber-värden gäller hela huset, inte bara pumpen. Pumpens elförbrukning kräver en separat mätare eller en tydlig förbrukningsgivare från myUplink.",
    }


def history_series(histories, config, states, unit, start, end):
    if not isinstance(histories, list) or any(
        not isinstance(group, list) or any(not isinstance(item, dict) for item in group)
        for group in histories
    ):
        raise ControlError("Home Assistant returnerade ogiltig temperaturhistorik.")
    names = {s["entity_id"]: s["attributes"].get("friendly_name", s["entity_id"]) for s in states}
    allowed = set(config["room_sensors"])
    if config["outdoor_entity"].startswith("sensor."):
        allowed.add(config["outdoor_entity"])
    groups = {}
    for group in histories:
        for item in group:
            eid = item.get("entity_id")
            if eid not in allowed:
                continue
            groups.setdefault(eid, []).append(item)
    series = []
    for eid in sorted(allowed):
        points = []
        setpoints = []
        for item in groups.get(eid, []):
            try:
                stamp = datetime.fromisoformat(item["last_updated"].replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ControlError(f"{eid}: historiken innehåller en ogiltig tidsstämpel.") from exc
            item = copy.deepcopy(item)
            if eid.startswith("climate."):
                item.setdefault("attributes", {})["temperature_unit"] = unit
            try:
                if item.get("state") in ("unknown", "unavailable"):
                    raise ControlError("Otillgängligt historikvärde.")
                value = temperature(item)
            except ControlError:
                value = None
            points.append([max(start, stamp), value])
            if eid.startswith("climate."):
                setpoint = None
                raw = item.get("attributes", {}).get("temperature")
                if item.get("state") not in ("unknown", "unavailable", "off") and raw is not None:
                    try:
                        setpoint = temperature({"entity_id": eid, "attributes": {"current_temperature": raw, "temperature_unit": unit}})
                    except ControlError:
                        setpoint = None
                if not setpoints or setpoints[-1][1] != setpoint:
                    setpoints.append([max(start, stamp), setpoint])
        # Keep all availability transitions, plus the first/last reading per five-minute bin.
        reduced = []
        for point in sorted(points):
            if reduced and point[0] // 300 == reduced[-1][0] // 300 and point[1] is not None and reduced[-1][1] is not None:
                if len(reduced) > 1 and reduced[-2][0] // 300 == point[0] // 300 and reduced[-2][1] is not None:
                    reduced[-1] = point
                else:
                    reduced.append(point)
            else:
                reduced.append(point)
        line = {"entity_id": eid, "name": names.get(eid, eid), "outdoor": eid == config["outdoor_entity"],
                "kind": "climate" if eid.startswith("climate.") else "sensor", "points": reduced}
        if eid.startswith("climate."):
            line["setpoints"] = sorted(setpoints)
        series.append(line)
    return {"start": start, "end": end, "target": config["target_temperature"], "series": series}


def parse_time(value, what):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ControlError(f"{what}: ogiltig tidsstämpel.") from exc


def history_groups(histories):
    """Map entity_id to its rows; minimal_response rows after the first omit entity_id."""
    if not isinstance(histories, list) or any(not isinstance(group, list) for group in histories):
        raise ControlError("Home Assistant returnerade ogiltig historik.")
    groups = {}
    for group in histories:
        if not group:
            continue
        if any(not isinstance(item, dict) for item in group) or not isinstance(group[0].get("entity_id"), str):
            raise ControlError("Home Assistant returnerade ogiltig historik.")
        groups.setdefault(group[0]["entity_id"], []).extend(group)
    return groups


def numeric_steps(rows, entity_id, start, fahrenheit=False):
    """Value changes of one entity as [time, value|None]; values hold until the next change."""
    steps = []
    for item in rows:
        stamp = parse_time(item.get("last_changed") or item.get("last_updated"), entity_id)
        value = None
        if item.get("state") not in ("unknown", "unavailable", None):
            try:
                value = number(item["state"], entity_id)
                if fahrenheit:
                    value = (value - 32) * 5 / 9
            except ControlError:
                value = None
        steps.append([max(start, stamp), value])
    steps.sort(key=lambda row: row[0])
    return steps


def value_at(steps, stamp):
    value = None
    for time_, step_value in steps:
        if time_ > stamp:
            break
        value = step_value
    return value


def curve_points(supply, outdoor, offset):
    """Pair the pump's calculated supply with outdoor temperature and offset at the same moment."""
    events = sorted([(t, 0, v) for t, v in outdoor] + [(t, 1, v) for t, v in supply] + [(t, 2, v) for t, v in offset],
                    key=lambda row: (row[0], row[1]))
    current = [None, None, None]
    bins = {}
    for index, (stamp, kind, value) in enumerate(events):
        current[kind] = value
        if index + 1 < len(events) and events[index + 1][0] == stamp:
            continue
        if None not in current:
            bins[int(stamp // 1800)] = [round(current[0], 2), round(current[1], 2), current[2]]
    return [bins[key] for key in sorted(bins)]


def fit_curve(points, offset):
    """Least-squares line through points recorded at the given offset."""
    selected = [(o, s) for o, s, off in points if offset is not None and abs(off - offset) < 1e-6]
    if len(selected) < 6:
        return None
    xs = [o for o, _ in selected]
    if max(xs) - min(xs) < 3:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(s for _, s in selected) / len(selected)
    variance = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((o - mean_x) * (s - mean_y) for o, s in selected) / variance
    return {
        "slope": round(slope, 4), "intercept": round(mean_y - slope * mean_x, 3),
        "min_outdoor": min(xs), "max_outdoor": max(xs), "count": len(selected), "offset": offset,
    }


def supply_entity(registry, offset_entity):
    """The pump's own calculated supply temperature for the same climate system as the offset."""
    selected = next((r for r in registry if r["entity_id"] == offset_entity and r.get("platform") == "myuplink"), None)
    if not selected or not selected.get("device_id") or "climate_system_" not in offset_entity:
        return None
    system = offset_entity.rsplit("climate_system_", 1)[1]
    matches = [
        r["entity_id"] for r in registry if r.get("platform") == "myuplink" and r.get("device_id") == selected["device_id"]
        and r["entity_id"].startswith("sensor.") and r["entity_id"].endswith("_calculated_supply_climate_system_" + system)
    ]
    return matches[0] if len(matches) == 1 else None


def tibber_prices(response):
    try:
        homes = response["service_response"]["prices"]
    except (KeyError, TypeError) as exc:
        raise ControlError("Tibber returnerade inga priser.") from exc
    if not isinstance(homes, dict) or len(homes) != 1:
        raise ControlError("Tibber har flera hem eller inga hem; priser kan inte väljas entydigt.")
    rows = next(iter(homes.values()))
    if not isinstance(rows, list):
        raise ControlError("Tibber returnerade ogiltiga priser.")
    prices = []
    for row in rows:
        if not isinstance(row, dict):
            raise ControlError("Tibber returnerade ogiltiga priser.")
        prices.append([parse_time(row.get("start_time"), "Tibber-pris"), number(row.get("price"), "Tibber-pris")])
    return sorted(prices)


def planned_offsets(config, status, forecast, prices, now, end, use_prices):
    """Simulate what the controller would write later, if the room stays as it is now.

    Uses the controller's own rules: room deviation, weather rule on the forecast, price rule on
    future prices, the pump's step grid, max_step and the minimum interval between writes.
    """
    plan = {"planned": [], "desired": [], "changes": [], "notes": []}
    if status.get("error_message") or "contributions" not in status or status.get("error") is None:
        plan["notes"].append("Ingen plan: styrningen saknar aktuella värden just nu.")
        return plan
    weather_on = bool(config["weather_gain"])
    price_configured = bool(config["cheap_boost"] or config["expensive_reduction"])
    price_on = price_configured and use_prices
    room = status["contributions"]["room"]

    def desired_at(stamp):
        outdoor = price = None
        weather = price_adjustment = 0
        if weather_on:
            future = [row for row in forecast if row[0] <= stamp]
            outdoor = (future[-1] if future else forecast[0])[1] if forecast else None
            if outdoor is None:
                return None, []
            weather = (config["weather_reference"] - outdoor) * config["weather_gain"]
        if price_on:
            price = value_at(prices, stamp)
            if price is not None:
                price_adjustment = price_contribution(config, price, status["error"])
        value = config["base_offset"] + room + weather + price_adjustment
        reasons = []
        if price_adjustment > 0:
            reasons.append(f"billigt elpris ({price:g})")
        elif price_adjustment < 0:
            reasons.append(f"dyrt elpris ({price:g})")
        if weather:
            reasons.append(f"utetemperatur {outdoor:.1f} °C")
        if room:
            reasons.append("rumstemperatur " + ("under" if room > 0 else "över") + " målet")
        return round(max(config["min_offset"], min(config["max_offset"], value)), 2), reasons

    slots = [now]
    if price_on:
        slots += [t for t, _ in prices if now < t <= end]
    if weather_on:
        slots += [t for t, _ in forecast if now < t <= end]
    slots = sorted(set(slots))
    for stamp in slots:
        value, _ = desired_at(stamp)
        if value is not None and (not plan["desired"] or plan["desired"][-1][1] != value):
            plan["desired"].append([stamp, value])

    if not weather_on and not price_on:
        plan["notes"].append("Väder- och elprispåverkan är 0, så planen följer bara rumstemperaturen.")
    if price_configured and not use_prices:
        plan["notes"].append("Prisplanen kräver Tibber-priser och att elprisgivaren är Tibbers, så att priserna har samma enhet.")
    if not config["enabled"]:
        plan["notes"].append("Automatiken är avstängd: planen visar vad som skulle ske, men inget skickas till pumpen.")
    limits = status.get("offset_limits")
    current = status.get("current_offset")
    if not limits or current is None:
        plan["planned"] = copy.deepcopy(plan["desired"])
        plan["notes"].append("Pumpens steglängd är okänd, så planen visar bara beräknat mål.")
        return plan
    interval = config["min_interval_minutes"] * 60
    next_allowed = now + max(0, status.get("wait_seconds") or 0)
    plan["planned"].append([now, current])
    stamp = now
    for _ in range(500):
        if stamp > end:
            break
        value, reasons = desired_at(stamp)
        pending = False
        if value is not None:
            target = next_step(config, current, value, limits["min"], limits["step"])
            if abs(target - current) > 1e-6:
                pending = True
                if stamp >= next_allowed:
                    plan["changes"].append({"timestamp": stamp, "previous": current, "offset": target,
                                            "desired": value, "reasons": reasons or ["fortsätter mot målet"]})
                    plan["planned"].append([stamp, target])
                    current = target
                    next_allowed = stamp + interval
                    pending = abs(next_step(config, current, value, limits["min"], limits["step"]) - current) > 1e-6
        candidates = [t for t in slots if t > stamp][:1]
        if pending:
            candidates.append(max(next_allowed, stamp + 60))
        if not candidates:
            break
        stamp = min(candidates)
    plan["notes"].append("Planen antar att rumstemperaturen ligger kvar där den är nu. Automatiken räknar om varje minut, "
                         "följer pumpens steglängd och väntar minst " + f"{config['min_interval_minutes']} minuter mellan ändringar.")
    return plan


def actual_changes(history, events, start):
    """Offset changes read back from Home Assistant, attributed to this add-on only when a matching write was logged."""
    attempts = [e for e in events if e.get("kind") == "write_attempt"]
    changes, previous = [], None
    for stamp, value in history:
        if value is None:
            continue
        if previous is not None and abs(value - previous) > 1e-6 and stamp > start:
            match = next((e for e in attempts if abs(e["details"].get("value", float("nan")) - value) < 1e-6
                          and stamp - 3600 <= e["timestamp"] <= stamp + 300), None)
            item = {"timestamp": stamp, "previous": previous, "offset": value,
                    "source": "automatik" if match else "manuell eller annan automation", "reasons": []}
            if match:
                c = match["details"].get("contributions", {})
                item["reasons"] = [f"{label} {c[key]:+g}" for key, label in (("room", "rum"), ("weather", "väder"), ("price", "elpris"))
                                   if c.get(key)]
            changes.append(item)
        previous = value
    return changes


class Insights:
    def __init__(self, ha):
        self.ha = ha
        self.cache = {}
        self.lock = threading.Lock()

    def cached(self, key, seconds, loader):
        with self.lock:
            now = time.time()
            entry = self.cache.get(key)
            if entry and now - entry[0] < seconds:
                return copy.deepcopy(entry[1])
        value = loader()
        with self.lock:
            self.cache[key] = (time.time(), value)
            for old in [k for k, (stamp, _) in self.cache.items() if time.time() - stamp > 3600]:
                del self.cache[old]
        return copy.deepcopy(value)

    def registry(self):
        return self.cached("registry", 300, self.ha.integration_registry)

    def overview(self, config):
        key = ("overview", config["offset_entity"], config["price_entity"], config["max_age_minutes"])
        return self.cached(key, 60, lambda: overview(self.ha.states(), self.registry(), config, time.time()))

    def history(self, config):
        key = ("history", tuple(config["room_sensors"]), config["outdoor_entity"], config["target_temperature"])

        def load():
            end = time.time()
            start = end - 86400
            ids = list(config["room_sensors"])
            if config["outdoor_entity"].startswith("sensor."):
                ids.append(config["outdoor_entity"])
            if not ids:
                return {"start": start, "end": end, "target": config["target_temperature"], "series": []}
            stamp = datetime.fromtimestamp(start, timezone.utc).isoformat()
            query = urlencode({
                "filter_entity_id": ",".join(ids),
                "end_time": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            })
            histories = self.ha.request("/history/period/" + quote(stamp, safe="") + "?" + query)
            states = self.ha.states()
            return history_series(histories, config, states, self.ha._temperature_unit, start, end)
        return self.cached(key, 300, load)

    def forecast(self, configured):
        def load():
            states = self.ha.states()
            supported = [
                s for s in states if s["entity_id"].startswith("weather.")
                and int(s["attributes"].get("supported_features", 0)) & 3
            ]
            if configured:
                candidates = [s for s in supported if s["entity_id"] == configured]
            else:
                smhi = {r["entity_id"] for r in self.registry() if r.get("platform") == "smhi"}
                candidates = [s for s in supported if s["entity_id"] in smhi]
                if not candidates:
                    candidates = supported
            if len(candidates) != 1:
                raise ControlError("Välj en väderentitet för prognosen; ingen entydig väderkälla hittades.")
            weather = candidates[0]
            eid = weather["entity_id"]
            if weather["state"] in ("unknown", "unavailable"):
                raise ControlError("Väderkällan är otillgänglig.")
            forecast_type = "hourly" if int(weather["attributes"]["supported_features"]) & 2 else "daily"
            response = self.ha.request("/services/weather/get_forecasts?return_response", {"entity_id": eid, "type": forecast_type})
            try:
                rows = response["service_response"][eid]["forecast"]
            except (KeyError, TypeError) as exc:
                raise ControlError("Väderkällan returnerade ingen prognos.") from exc
            if not isinstance(rows, list) or not rows:
                raise ControlError("Väderprognosen är tom.")
            points = []
            now = time.time()
            unit = weather["attributes"].get("temperature_unit")
            for row in rows[:72]:
                try:
                    stamp = datetime.fromisoformat(row["datetime"].replace("Z", "+00:00")).timestamp()
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    raise ControlError("Väderprognosen innehåller en ogiltig tidpunkt.") from exc
                if stamp < now - (3600 if forecast_type == "hourly" else 86400):
                    continue
                temp = temperature({"entity_id": eid, "attributes": {"temperature": row.get("temperature"), "temperature_unit": unit}})
                points.append({"timestamp": stamp, "temperature": temp, "condition": row.get("condition", "")})
            if not points:
                raise ControlError("Väderprognosen innehåller bara gamla tidpunkter.")
            return {
                "entity_id": eid, "name": weather["attributes"].get("friendly_name", eid),
                "type": forecast_type, "points": points, "fetched_at": now,
                "note": "Prognosen visas som beslutsstöd. Den ändrar inte värmestyrningen; väderpåverkan använder fortfarande NIBEs aktuella utetemperatur.",
            }
        return self.cached(("forecast", configured), 1800, load)

    def prices(self):
        def load():
            if not any(r.get("platform") == "tibber" for r in self.registry()):
                raise ControlError("Ingen Tibber-integration hittades; elprisgrafen kräver Tibber.")
            now = time.time()
            local = datetime.fromtimestamp(now).astimezone()
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            body = {
                "start": (midnight - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "end": (midnight + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            }
            return tibber_prices(self.ha.request("/services/tibber/get_prices?return_response", body))
        return self.cached("prices", 900, load)

    def plan(self, config, status, events=None):
        key = ("plan", json.dumps(config, sort_keys=True))

        def load():
            now = time.time()
            start, end = now - 86400, now + 36 * 3600
            notes = []
            result = {
                "start": start, "end": end, "now": now,
                "outdoor": {"history": [], "forecast": [], "error": None},
                "prices": {"points": [], "low": config["price_low"], "high": config["price_high"], "unit": "", "error": None},
                "offset": {"history": [], "planned": [], "desired": [], "changes": [], "done": [], "min": config["min_offset"],
                           "max": config["max_offset"], "base": config["base_offset"], "error": None},
                "curve": {"points": [], "fit": None, "current": None, "forecast_range": None, "supply_entity": None, "error": None},
                "notes": notes,
            }
            states = {s["entity_id"]: s for s in self.ha.states()}
            registry = self.registry()
            supply_id = supply_entity(registry, config["offset_entity"])
            result["curve"]["supply_entity"] = supply_id
            ids = [e for e in (config["outdoor_entity"], config["offset_entity"], supply_id) if e and e.startswith(("sensor.", "number."))]
            groups = {}
            if ids:
                curve_start = now - 7 * 86400
                query = urlencode({
                    "filter_entity_id": ",".join(ids),
                    "end_time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "minimal_response": "", "no_attributes": "", "significant_changes_only": "0",
                })
                stamp = datetime.fromtimestamp(curve_start, timezone.utc).isoformat()
                groups = history_groups(self.ha.request("/history/period/" + quote(stamp, safe="") + "?" + query))

            def steps(eid, since):
                unit = states.get(eid, {}).get("attributes", {}).get("unit_of_measurement")
                return numeric_steps(groups.get(eid, []), eid, since, unit == "°F")

            outdoor_all = steps(config["outdoor_entity"], now - 7 * 86400) if config["outdoor_entity"] in groups else []
            offset_all = steps(config["offset_entity"], now - 7 * 86400) if config["offset_entity"] in groups else []
            recent = lambda rows: [[max(start, t), v] for i, (t, v) in enumerate(rows)
                                   if t >= start or i + 1 == len(rows) or rows[i + 1][0] > start]
            result["outdoor"]["history"] = recent(outdoor_all)
            result["offset"]["history"] = recent(offset_all)
            if not outdoor_all:
                result["outdoor"]["error"] = "Ingen historik för utomhusgivaren."
            if not offset_all:
                result["offset"]["error"] = "Ingen historik för kurvförskjutningen."

            forecast = []
            try:
                forecast = [[p["timestamp"], p["temperature"]] for p in self.forecast(config["forecast_entity"])["points"]]
            except ControlError as exc:
                result["outdoor"]["error"] = str(exc)
            result["outdoor"]["forecast"] = [p for p in forecast if p[0] <= end]

            prices = []
            try:
                prices = self.prices()
            except ControlError as exc:
                result["prices"]["error"] = str(exc)
            result["prices"]["points"] = [p for p in prices if start - 3600 <= p[0] <= end]
            price_id = config["price_entity"]
            platform = next((r.get("platform") for r in registry if r["entity_id"] == price_id), None)
            if price_id in states:
                result["prices"]["unit"] = states[price_id]["attributes"].get("unit_of_measurement", "")
            use_prices = bool(prices) and platform == "tibber"

            result["_forecast"], result["_prices"], result["_use_prices"] = forecast, prices, use_prices

            if not supply_id:
                result["curve"]["error"] = "Pumpens beräknade framledning hittades inte; värmekurvan kan inte visas."
            elif supply_id not in groups or not outdoor_all or not offset_all:
                result["curve"]["error"] = "För lite historik för att visa värmekurvan ännu."
            else:
                points = curve_points(steps(supply_id, now - 7 * 86400), outdoor_all, offset_all)
                current_offset = offset_all[-1][1]
                result["curve"]["points"] = points
                result["curve"]["fit"] = fit_curve(points, current_offset)
                if points:
                    result["curve"]["current"] = {"outdoor": points[-1][0], "supply": points[-1][1], "offset": points[-1][2]}
                if result["curve"]["fit"] is None:
                    result["curve"]["error"] = "För få punkter med nuvarande förskjutning för att rita en kurva ännu."
            future = [t for s, t in forecast if now <= s <= end]
            if future:
                result["curve"]["forecast_range"] = [min(future), max(future)]
            return result
        result = self.cached(key, 300, load)
        now = time.time()
        forecast, prices, use_prices = result.pop("_forecast"), result.pop("_prices"), result.pop("_use_prices")
        result["now"] = now
        end = result["end"] = max(result["end"], now + 36 * 3600)
        plan = planned_offsets(config, status, forecast, prices, now, end, use_prices)
        result["offset"].update(planned=plan["planned"], desired=plan["desired"], changes=plan["changes"])
        result["offset"]["done"] = actual_changes(result["offset"]["history"], events or [], result["start"])
        result["notes"].extend(plan["notes"])
        return result
