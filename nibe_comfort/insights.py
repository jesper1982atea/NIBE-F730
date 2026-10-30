"""Read-only weather, history and pump telemetry from Home Assistant."""

import copy
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from controller import ControlError, control_rooms, fresh_entity, next_step, number, price_contribution, temperature
from learning import LearningStore, hourly_means, learn, lead_hours, predict, preheat_windows, shift_at
from energy import consumption_report, meter_error, produced_energy


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


def nordpool_sensor(entity):
    return entity["entity_id"].startswith(("sensor.nord_pool_", "sensor.nordpool_"))


def discover_prices(states, registry, provider="tibber"):
    if provider == "tibber":
        return discover_tibber(states, registry)
    candidates = {s["entity_id"] for s in states if nordpool_sensor(s)
                  and (s["entity_id"].endswith(("_aktuellt_pris", "_current_price"))
                       or "raw_today" in s["attributes"])
                  and re.fullmatch(r"[^\s/]+/(?:kWh|MWh)", s["attributes"].get("unit_of_measurement", ""), re.I)}
    if len(candidates) == 1:
        selected = next(iter(candidates))
        return {
            "price_entity": selected, "price_message": "Nord Pools aktuella elpris hittades automatiskt."}
    return {"price_entity": "", "price_message": (
        "Flera Nord Pool-prisgivare finns. Välj rätt elområde manuellt." if candidates else
        "Ingen Nord Pool-prisgivare hittades. Välj prisgivare manuellt.")}


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
        if not produced_energy(entity) and attrs.get("device_class") in ("power", "energy") and any(
            word in text for word in ("consumption", "consumed", "electrical", "electricity", "electric power", "energy_spent")
        ):
            pump_electricity.append(reading(entity, attrs.get("friendly_name", eid), config["max_age_minutes"], now, pump_states))
    tibber_ids = {r["entity_id"] for r in registry
                  if config["price_provider"] == "tibber" and r.get("platform") == "tibber"}
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
            house.append({"label": label, "value": None, "error": (
                "Ingen förbrukningsmätare vald. Nord Pool ger elpris, inte husets förbrukning."
                if config["price_provider"] == "nordpool" else "Saknad eller flertydig Tibber-mätare."
            )})
    price = None
    price_id = config["price_entity"] or discover_prices(states, registry, config["price_provider"])["price_entity"]
    if price_id in by_id:
        provider = "Nord Pool" if config["price_provider"] == "nordpool" else "Tibber"
        price = reading(by_id[price_id], f"Aktuellt elpris · {provider}", config["max_age_minutes"], now)
    return {
        "checked_at": now, "pump": pump, "pump_electricity": pump_electricity,
        "house": house, "price": price,
        "electricity_note": (
            "Elpriset kommer från Nord Pool. Husets förbrukning från exempelvis Enegic är separat och hämtas inte automatiskt här. "
            if config["price_provider"] == "nordpool" else
            "Husets Tibber-värden gäller hela huset, inte bara pumpen. "
        ) + "Pumpens elförbrukning kräver en separat mätare eller en tydlig förbrukningsgivare från myUplink.",
    }


HOT_WATER = {
    "top": ("sensor.", "_hot_water_top_bt7"),
    "charge": ("sensor.", "_hot_water_charging_bt6"),
    "minutes": ("sensor.", "_hot_water_amount"),
    "demand": ("select.", "_hot_water_amount"),
    "status": ("sensor.", "_status"),
}
DEMAND = {"Small": "Liten", "Normal": "Normal", "Large": "Stor", "Smart control": "Smart styrning"}


def pump_device_entities(registry, offset_entity):
    selected = next((r for r in registry if r["entity_id"] == offset_entity and r.get("platform") == "myuplink"), None)
    if not selected or not selected.get("device_id"):
        return set()
    return {r["entity_id"] for r in registry if r.get("platform") == "myuplink" and r.get("device_id") == selected["device_id"]}


def hot_water_entities(registry, offset_entity):
    ids = pump_device_entities(registry, offset_entity)
    found = {}
    for key, (prefix, suffix) in HOT_WATER.items():
        matches = [e for e in ids if e.startswith(prefix) and e.endswith(suffix)]
        if len(matches) == 1:
            found[key] = matches[0]
    return found


def draw_offs(points, now):
    """Hot water use: fast drops in the lower tank sensor (BT6) as cold water flows in.

    Standby losses move the 0.5 °C-resolution sensor by single half-degree steps over hours,
    while taps drop it by a degree or more between reports. Close drops are one event."""
    valid = [(t, v) for t, v in points if v is not None]
    steps = []
    for (t0, v0), (t1, v1) in zip(valid, valid[1:]):
        drop = v0 - v1
        if drop >= 1.0 and (t1 - t0 <= 3600 or drop >= 1.5):
            steps.append([max(t0, t1 - 900), t1, v0, v1])
    events = []
    for step in steps:
        if events and step[0] - events[-1][1] <= 1800:
            events[-1][1], events[-1][3] = step[1], step[3]
        else:
            events.append(step)
    result = []
    for start, end, before, after in events:
        drop = round(before - after, 1)
        if drop < 1.5:
            continue
        size = "stor" if drop >= 10 else "mellan" if drop >= 4 else "liten"
        result.append({"start": start, "end": end, "from": before, "to": after, "drop": drop, "size": size})
    return result


def charge_periods(rows, now):
    periods = []
    for index, (stamp, state) in enumerate(rows):
        if state == "Hot water":
            end = rows[index + 1][0] if index + 1 < len(rows) else now
            if periods and stamp - periods[-1][1] <= 60:
                periods[-1][1] = end
            else:
                periods.append([stamp, end])
    return [{"start": a, "end": b} for a, b in periods]


def hot_water(states, registry, config, groups, now, days=7):
    by_id = {s["entity_id"]: s for s in states}
    ids = hot_water_entities(registry, config["offset_entity"])
    start = now - days * 86400
    result = {
        "now": now, "start": start, "days": days, "entities": ids,
        "top": None, "charge": None, "minutes": None, "demand": None, "charging": None,
        "level": None, "level_source": None, "max_minutes": None,
        "series": {"top": [], "charge": [], "minutes": []},
        "draw_offs": [], "charges": [], "error": None,
    }
    if "top" not in ids and "charge" not in ids:
        result["error"] = ("Inga varmvattengivare hittades. Välj pumpens kurvförskjutning först; "
                           "varmvattnet läses från samma myUplink-enhet (BT6/BT7).")
        return result
    peers = [by_id[e] for e in pump_device_entities(registry, config["offset_entity"]) if e in by_id]
    for key in ("top", "charge", "minutes", "demand"):
        if key in ids and ids[key] in by_id:
            item = reading(by_id[ids[key]], key, config["max_age_minutes"], now, peers)
            if key == "demand":
                result[key] = DEMAND.get(item["value"], item["value"])
                continue
            try:
                result[key] = number(item["value"], ids[key]) if item["value"] is not None else None
            except ControlError:
                result[key] = None
    if "status" in ids and ids["status"] in by_id:
        item = reading(by_id[ids["status"]], "status", config["max_age_minutes"], now, peers)
        result["charging"] = None if item["value"] is None else item["value"] == "Hot water"
    for key in ("top", "charge", "minutes"):
        if key in ids and ids[key] in groups:
            unit = by_id.get(ids[key], {}).get("attributes", {}).get("unit_of_measurement")
            result["series"][key] = numeric_steps(groups[ids[key]], ids[key], start, unit == "°F")
    if result["series"]["charge"]:
        result["draw_offs"] = draw_offs(result["series"]["charge"], now)
    if "status" in ids and ids["status"] in groups:
        rows = sorted(([max(start, parse_time(r.get("last_changed") or r.get("last_updated"), ids["status"])), r.get("state")]
                       for r in groups[ids["status"]] if r.get("state") not in ("unavailable", "unknown", None)),
                      key=lambda row: row[0])
        result["charges"] = charge_periods(rows, now)
    minutes = [v for _, v in result["series"]["minutes"] if v is not None]
    if result["minutes"] is not None and minutes and max(minutes) > 0:
        result["max_minutes"] = max(minutes)
        result["level"] = max(0.0, min(1.0, result["minutes"] / max(minutes)))
        result["level_source"] = "minutes"
    else:
        temps = [v for _, v in result["series"]["charge"] if v is not None]
        if result["charge"] is not None and temps and max(temps) > 30:
            result["level"] = max(0.0, min(1.0, (result["charge"] - 25) / (max(temps) - 25)))
            result["level_source"] = "charge"
    return result


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
        for point in sorted(points, key=lambda point: point[0]):
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
            line["setpoints"] = sorted(setpoints, key=lambda point: point[0])
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


def nordpool_prices(rows, value_key="value", factor=1):
    if not isinstance(rows, list) or len(rows) > 300:
        raise ControlError("Nord Pool returnerade ogiltig eller för stor prislista.")
    intervals = []
    for row in rows:
        if not isinstance(row, dict):
            raise ControlError("Nord Pool returnerade en ogiltig prisperiod.")
        bounds = []
        for key in ("start", "end"):
            try:
                stamp = datetime.fromisoformat(row[key].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ControlError("Nord Pool: ogiltig tidsstämpel.") from exc
            if stamp.tzinfo is None:
                raise ControlError("Nord Pools prisperioder måste ha tidszon.")
            bounds.append(stamp.timestamp())
        start, end = bounds
        if end - start not in (900, 3600):
            raise ControlError("Nord Pools prisperiod måste vara 15 eller 60 minuter.")
        intervals.append((start, end, number(row.get(value_key), "Nord Pool-pris") * factor))
    intervals.sort()
    if any(a[1] != b[0] for a, b in zip(intervals, intervals[1:])):
        raise ControlError("Nord Pools prisperioder överlappar eller har luckor.")
    resolution = min((end - start for start, end, _ in intervals), default=900)
    return [[start + i * resolution, value] for start, end, value in intervals
            for i in range(int((end - start) / resolution))]


def price_end(prices):
    if not prices:
        return None
    slot = min((b[0] - a[0] for a, b in zip(prices, prices[1:]) if b[0] > a[0]), default=900)
    return prices[-1][0] + slot


def planned_offsets(config, status, forecast, prices, now, end, use_prices, windows=None):
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
    prices_until = price_end(prices)
    preheat_on = bool(config.get("preheat_enabled")) and bool(windows)
    fireplace_active = (status.get("fireplace") or {}).get("phase") == "active"
    fireplace = status["contributions"].get("fireplace", 0) if config["fireplace_enabled"] and fireplace_active else 0
    base_error = status["error"] - (status.get("target_shift") or 0)

    def desired_at(stamp):
        outdoor = price = None
        weather = price_adjustment = 0
        room = status["contributions"]["room"]
        window = shift_at(windows, stamp) if preheat_on else None
        error = base_error + (window["shift"] if window else 0)
        if preheat_on:
            room = 0 if abs(error) <= config["deadband"] else error * config["room_gain"]
        if weather_on:
            future = [row for row in forecast if row[0] <= stamp]
            outdoor = (future[-1] if future else forecast[0])[1] if forecast else None
            if outdoor is None:
                return None, []
            weather = (config["weather_reference"] - outdoor) * config["weather_gain"]
        if price_on:
            price = value_at(prices, stamp) if prices_until is not None and stamp < prices_until else None
            if price is not None:
                price_adjustment = price_contribution(config, price, error)
        value = config["base_offset"] + room + weather + price_adjustment + fireplace
        reasons = []
        if window and window["kind"] == "preheat":
            reasons.append(f"förvärmer +{window['shift']:g}° före dyrt elpris")
        elif window:
            reasons.append(f"dyrt elpris, får bli {-window['shift']:g}° svalare")
        if price_adjustment > 0:
            reasons.append(f"billigt elpris ({price:g})")
        elif price_adjustment < 0:
            reasons.append(f"dyrt elpris ({price:g})")
        if weather:
            reasons.append(f"utetemperatur {outdoor:.1f} °C")
        if room:
            reasons.append("rumstemperatur " + ("under" if room > 0 else "över") + " målet")
        if fireplace:
            reasons.append("misstänkt extravärme: kaminbidrag −1 steg")
        return round(max(config["min_offset"], min(config["max_offset"], value)), 2), reasons

    slots = [now]
    if price_on:
        slots += [t for t, _ in prices if now < t <= end]
        if prices_until is not None and now < prices_until <= end:
            slots.append(prices_until)
    if weather_on:
        slots += [t for t, _ in forecast if now < t <= end]
    if preheat_on:
        slots += [t for w in windows for t in (w["start"], w["end"]) if now < t <= end]
    slots = sorted(set(slots))
    for stamp in slots:
        value, _ = desired_at(stamp)
        if value is not None and (not plan["desired"] or plan["desired"][-1][1] != value):
            plan["desired"].append([stamp, value])

    if not weather_on and not price_on and not preheat_on:
        plan["notes"].append("Väder- och elprispåverkan är 0, så planen följer bara rumstemperaturen.")
    if price_configured and not use_prices:
        provider = "Nord Pool" if config["price_provider"] == "nordpool" else "Tibber"
        plan["notes"].append(f"Prisplanen kräver kommande priser från vald {provider}-givare, i samma enhet som givaren.")
    if price_on and prices_until is not None and prices_until < end:
        plan["notes"].append("Prislistan täcker inte hela planen. Efter sista prisperioden antas ingen extra kurvpåverkan från priset; framtida priser gissas inte.")
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
                item["reasons"] = [f"{label} {c[key]:+g}" for key, label in (
                    ("room", "rum"), ("weather", "väder"), ("price", "elpris"), ("fireplace", "kamin"))
                                   if c.get(key)]
                kind = match["details"].get("preheat")
                if kind:
                    item["reasons"].append("förvärmning" if kind == "preheat" else "sparläge vid dyrt elpris")
            changes.append(item)
        previous = value
    return changes


def control_history(events, config, start, end):
    """One entry per logged request, with its latest outcome; never infer physical changes."""
    attempts = {e["id"]: e for e in events if e["kind"] in ("write_attempt", "thermostat_attempt")}
    outcomes = {}
    for event in sorted(events, key=lambda e: (e["timestamp"], e["id"])):
        if event["kind"] in ("write_sent", "write_observed", "write_failed", "write_unconfirmed",
                             "thermostat_sent", "thermostat_observed", "thermostat_failed", "thermostat_unconfirmed"):
            outcomes[event["details"].get("request_id")] = event
    result = []
    for event in events:
        d = event["details"]
        if (event["kind"] == "offset_observed" and d.get("entity_id") == config["offset_entity"]
                and start <= event["timestamp"] <= end):
            result.append({"id": event["id"], "timestamp": event["timestamp"], "kind": "offset",
                           "entity_id": d["entity_id"], "status": "observed", "previous": d.get("previous"),
                           "value": d.get("value"), "unit": ""})
    for attempt in attempts.values():
        details = attempt["details"]
        thermostat = attempt["kind"] == "thermostat_attempt"
        entity_id = details.get("entity_id")
        if entity_id not in (config["room_sensors"] if thermostat else [config["offset_entity"]]):
            continue
        outcome = outcomes.get(attempt["id"])
        stamp = attempt["timestamp"]
        if not start <= stamp <= end:
            if not outcome or not start <= outcome["timestamp"] <= end:
                continue
            stamp = outcome["timestamp"]
        status = "attempt"
        if outcome:
            status = {"observed": "confirmed", "unconfirmed": "unconfirmed",
                      "failed": "failed", "sent": "sent"}[outcome["kind"].split("_")[-1]]
        result.append({"id": attempt["id"], "timestamp": stamp, "requested_at": attempt["timestamp"],
                       "outcome_at": outcome["timestamp"] if outcome else None,
                       "kind": "thermostat" if thermostat else "offset", "entity_id": entity_id,
                       "status": status, "previous": details.get("previous"), "value": details.get("value"),
                       "unit": details.get("unit", ""), "contributions": details.get("contributions"),
                       "indoor": details.get("indoor", details.get("current")), "outdoor": details.get("outdoor"),
                       "price": details.get("price"), "price_unit": details.get("price_unit"),
                       "target": details.get("target_temperature"),
                       "preheat": details.get("preheat"), "error": outcome["details"].get("error") if outcome else None})
    return sorted(result, key=lambda e: (e["timestamp"], e["id"]))


class Insights:
    def __init__(self, ha, data_dir=None):
        self.ha = ha
        self.cache = {}
        self.lock = threading.Lock()
        self.loading = {}
        self.history_lock = threading.Lock()
        self.store = LearningStore(f"{data_dir}/learning.sqlite" if data_dir else None)
        self.fireplace_provider = None
        self.control_schedule = None
        self.control_loading = False

    def control_preheat(self, config):
        """Never let optional historical analysis delay current control readings."""
        key = json.dumps(config, sort_keys=True), self.fireplace_intervals()
        with self.lock:
            entry = self.control_schedule
            matching = entry is not None and entry[0] == key
            age = time.monotonic() - entry[1] if matching else float("inf")
            if not self.control_loading and age >= 60:
                self.control_loading = True
                threading.Thread(target=self._load_control_preheat, args=(key, copy.deepcopy(config)),
                                 daemon=True).start()
            if matching and age < 900:
                if isinstance(entry[2], str):
                    raise ControlError(entry[2])
                return copy.deepcopy(entry[2])
        raise ControlError("Förvärmningsplanen hämtas i bakgrunden. Vanligt temperaturmål används tills den är klar.")

    def _load_control_preheat(self, key, config):
        result = "Förvärmningshämtningen avbröts. Vanligt temperaturmål används."
        try:
            result = self.preheat(config)
        except ControlError as exc:
            result = str(exc)
            logging.getLogger("nibe_comfort").warning("Förvärmningsplanen kunde inte hämtas: %s", exc)
        except Exception as exc:
            logging.getLogger("nibe_comfort").exception("Internt fel vid hämtning av förvärmningsplan")
            result = f"Internt fel i förvärmningsplaneringen: {type(exc).__name__}. Kontrollera tilläggsloggen."
        else:
            result = copy.deepcopy(result)
        finally:
            with self.lock:
                self.control_schedule = (key, time.monotonic(), result)
                self.control_loading = False

    def fireplace_intervals(self):
        return self.fireplace_provider() if self.fireplace_provider else []

    def cached(self, key, seconds, loader):
        with self.lock:
            now = time.time()
            entry = self.cache.get(key)
            if entry and now - entry[0] < seconds:
                return copy.deepcopy(entry[1])
            pending = self.loading.get(key)
            owner = pending is None
            if owner:
                pending = self.loading[key] = Future()
        if not owner:
            try:
                return copy.deepcopy(pending.result(timeout=15))
            except FutureTimeout as exc:
                if pending.done():
                    raise
                raise ControlError("Underlaget håller redan på att hämtas. Försök igen när hämtningen är klar.") from exc
        try:
            value = loader()
            with self.lock:
                self.cache[key] = (time.time(), value)
                for old in [k for k, (stamp, _) in self.cache.items() if time.time() - stamp > 3600]:
                    del self.cache[old]
                while len(self.cache) > 24:
                    del self.cache[min(self.cache, key=lambda k: self.cache[k][0])]
            pending.set_result(value)
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self.lock:
                del self.loading[key]
        return copy.deepcopy(value)

    def history_request(self, path):
        # Different dashboards must not allocate large Recorder responses concurrently.
        if not self.history_lock.acquire(timeout=15):
            raise ControlError("Historikhämtningen är upptagen. Försök igen om en stund.")
        try:
            return self.ha.request(path)
        finally:
            self.history_lock.release()

    def registry(self):
        return self.cached("registry", 300, self.ha.integration_registry)

    def overview(self, config):
        key = ("overview", config["offset_entity"], config["price_entity"], config["price_provider"], config["max_age_minutes"])
        return self.cached(key, 60, lambda: overview(self.ha.states(), self.registry(), config, time.time()))

    def energy(self, config):
        key = ("energy", config["energy_entity"], config["price_entity"], config["price_provider"],
               config["max_age_minutes"])

        def load():
            now = time.time()
            end = int(now // 3600) * 3600
            start = end - 86400
            if not config["energy_entity"]:
                raise ControlError("Välj pumpens ackumulerade elmätare i inställningarna för att följa förbrukning och kostnad.")
            states = {s["entity_id"]: s for s in self.ha.states()}
            meter = states.get(config["energy_entity"])
            if not meter:
                raise ControlError("Den valda elmätaren saknas i Home Assistant.")
            error = meter_error(meter)
            if error:
                raise ControlError(error)
            price_id = config["price_entity"]
            price_problem = None
            if price_id:
                platform = next((r.get("platform") for r in self.registry() if r["entity_id"] == price_id), None)
                if (config["price_provider"] == "tibber" and platform != "tibber"
                        or config["price_provider"] == "nordpool" and platform == "tibber"):
                    price_problem = "Prisgivaren hör inte till din sparade elpriskälla. Välj rätt källa och givare i inställningarna."
            query = urlencode({
                "filter_entity_id": ",".join(filter(None, (config["energy_entity"], price_id))),
                "end_time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "significant_changes_only": "0",
            })
            stamp = datetime.fromtimestamp(start - 3600, timezone.utc).isoformat()
            groups = history_groups(self.history_request("/history/period/" + quote(stamp, safe="") + "?" + query))
            report = consumption_report(groups.get(config["energy_entity"], []),
                                        [] if price_problem else groups.get(price_id, []),
                                        config["energy_entity"], price_id, start, end,
                                        min(config["max_age_minutes"] * 60, 7200))
            if price_problem:
                report["price_error"] = price_problem
            report["meter"] = {"entity_id": meter["entity_id"], "name": meter["attributes"].get("friendly_name", meter["entity_id"])}
            report["price_entity"] = price_id
            report["price_provider"] = config["price_provider"]
            report["checked_at"] = time.time()
            return report
        return self.cached(key, 300, load)

    def hot_water(self, config):
        key = ("hot_water", config["offset_entity"], config["max_age_minutes"])

        def load():
            now = time.time()
            registry = self.registry()
            states = self.ha.states()
            ids = [e for k, e in hot_water_entities(registry, config["offset_entity"]).items() if k != "demand"]
            groups = {}
            if ids:
                query = urlencode({
                    "filter_entity_id": ",".join(ids),
                    "end_time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "minimal_response": "", "no_attributes": "", "significant_changes_only": "0",
                })
                stamp = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).isoformat()
                groups = history_groups(self.history_request("/history/period/" + quote(stamp, safe="") + "?" + query))
            return hot_water(states, registry, config, groups, now)
        return self.cached(key, 120, load)

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
            histories = self.history_request("/history/period/" + quote(stamp, safe="") + "?" + query)
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

    def prices(self, config):
        price_id = config["price_entity"]
        if not price_id:
            provider = "Nord Pool" if config["price_provider"] == "nordpool" else "Tibber"
            raise ControlError(f"Välj en elprisgivare för kommande priser från {provider}.")

        def load():
            registry = self.registry()
            states = {s["entity_id"]: s for s in self.ha.states()}
            entity = fresh_entity(states, price_id, config["max_age_minutes"], time.time())
            number(entity["state"], "Aktuellt elpris")
            platform = next((r.get("platform") for r in registry if r["entity_id"] == price_id), None)
            if config["price_provider"] == "tibber" and platform != "tibber":
                raise ControlError("Tibber är vald elpriskälla. Välj en Tibber-prisgivare eller byt källa till Nord Pool och spara.")
            if config["price_provider"] == "nordpool" and platform == "tibber":
                raise ControlError("Nord Pool är vald elpriskälla men givaren tillhör Tibber. Välj rätt prisgivare och spara.")
            if config["price_provider"] == "nordpool" and "raw_today" in entity["attributes"]:
                attrs = entity["attributes"]
                if not re.fullmatch(r"[^\s/]+/(?:kWh|MWh)", attrs.get("unit_of_measurement", ""), re.I):
                    raise ControlError("Nord Pool-givarens prisenhet måste vara valuta/kWh eller valuta/MWh.")
                tomorrow = attrs.get("raw_tomorrow", []) if attrs.get("tomorrow_valid") is True else []
                if not isinstance(attrs["raw_today"], list) or not isinstance(tomorrow, list):
                    raise ControlError("Nord Pool-givaren har ogiltiga prisattribut.")
                return nordpool_prices(attrs["raw_today"] + tomorrow)
            if config["price_provider"] == "nordpool" and platform == "nordpool":
                return self.nordpool_service_prices(entity, states, registry)
            if config["price_provider"] == "nordpool":
                raise ControlError("Vald Nord Pool-givare saknar kommande priser. Nord Pool med raw_today/raw_tomorrow eller Nord Pools prisåtgärd behövs.")
            now = time.time()
            local = datetime.fromtimestamp(now).astimezone()
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            body = {
                "start": (midnight - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "end": (midnight + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            }
            return tibber_prices(self.ha.request("/services/tibber/get_prices?return_response", body))
        return self.cached(("prices", config["price_provider"], price_id, config["max_age_minutes"]), 900, load)

    def nordpool_service_prices(self, entity, states, registry):
        selected = next(r for r in registry if r["entity_id"] == entity["entity_id"])
        entry = selected.get("config_entry_id")
        area = entity["attributes"].get("region")
        if area not in ("SE1", "SE2", "SE3", "SE4"):
            match = re.search(r"(?:^|_)(se[1-4])(?:_|$)", entity["entity_id"], re.I)
            area = match[1].upper() if match else None
        unit = entity["attributes"].get("unit_of_measurement", "")
        match = re.fullmatch(r"([A-Z]{3})/(kWh|MWh)", unit)
        if not isinstance(entry, str) or not entry or not area or not match:
            raise ControlError("Nord Pool: kan inte identifiera konfiguration, valuta eller elområde. Behåll SE1–SE4 i givarens entitets-id och använd valuta/kWh eller valuta/MWh.")
        local_date = self.ha.request("/template", {"template": "{{ now().date().isoformat() | to_json }}"})
        try:
            today = datetime.strptime(local_date, "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise ControlError("Home Assistant returnerade ett ogiltigt lokalt datum för Nord Pool.") from exc
        days = [today]
        # The official integration has one tomorrow-availability sensor per area/device.
        availability = [states[r["entity_id"]] for r in registry if r.get("platform") == "nordpool"
                        and r.get("device_id") and r.get("device_id") == selected.get("device_id")
                        and r["entity_id"].startswith("binary_sensor.") and r["entity_id"] in states]
        if len(availability) == 1 and availability[0]["state"] == "on":
            fresh_entity(states, availability[0]["entity_id"], 24 * 60, time.time())
            days.append(today + timedelta(days=1))
        rows = []
        for day in days:
            response = self.ha.request("/services/nordpool/get_prices_for_date?return_response", {
                "config_entry": entry, "date": day.isoformat(), "areas": [area], "currency": match[1]})
            try:
                values = response["service_response"][area]
            except (KeyError, TypeError) as exc:
                raise ControlError(f"Nord Pool saknar priser för {area} {day}.") from exc
            if not isinstance(values, list) or not values:
                raise ControlError(f"Nord Pool saknar priser för {area} {day}.")
            rows.extend(values)
        return nordpool_prices(rows, "price", 0.001 if match[2] == "kWh" else 1)

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
                groups = history_groups(self.history_request("/history/period/" + quote(stamp, safe="") + "?" + query))

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
                prices = self.prices(config)
            except ControlError as exc:
                result["prices"]["error"] = str(exc)
            result["prices"]["points"] = [p for p in prices if start - 3600 <= p[0] <= end]
            result["prices"]["end"] = price_end(prices)
            price_id = config["price_entity"]
            if price_id in states:
                result["prices"]["unit"] = states[price_id]["attributes"].get("unit_of_measurement", "")
            use_prices = bool(prices)

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
        windows = []
        if config["preheat_enabled"]:
            try:
                windows = self.preheat(config)["windows"]
            except ControlError as exc:
                result["notes"].append(f"Förvärmning: {exc}")
        plan = planned_offsets(config, status, forecast, prices, now, end, use_prices, windows)
        result["offset"].update(planned=plan["planned"], desired=plan["desired"], changes=plan["changes"])
        result["offset"]["done"] = actual_changes(result["offset"]["history"], events or [], result["start"])
        result["notes"].extend(plan["notes"])
        if (status.get("fireplace") or {}).get("active_since") is not None:
            result["notes"].append("Misstänkt extravärme: planen antar nuvarande rumstemperatur och fortsatt kaminläge. Varaktigheten är okänd; planen räknas om vid nästa avläsning.")
        return result

    def house_model(self, config):
        """Learn from Home Assistant history (7 days) plus everything stored earlier. Cached for an hour."""
        episodes = self.fireplace_intervals()
        key = ("model", tuple(control_rooms(config)), config["outdoor_entity"], config["offset_entity"],
               tuple((e["start"], e["end"]) for e in episodes))

        def load():
            now = time.time()
            rooms = list(control_rooms(config))
            result = {"model": None, "indoor": {}, "outdoor": {}, "offset": {}, "now": now, "error": None}
            if not rooms or not config["offset_entity"]:
                result["error"] = "Välj rum och pumpens kurvförskjutning först."
                return result
            ids = rooms + ([config["outdoor_entity"]] if config["outdoor_entity"].startswith("sensor.") else [])
            states = self.ha.states()
            stored = self.store.load(ids + [config["offset_entity"]])
            latest = [max(stored[e]) for e in ids + [config["offset_entity"]] if stored[e]]
            # Backfill once, then refresh only recent hours, retaining a two-day overlap.
            start = max(now - 7 * 86400, min(latest) - 2 * 86400) if len(latest) == len(ids) + 1 else now - 7 * 86400
            start = math.floor(start / 3600) * 3600
            while start < now:
                end = min(start + 86400, now)
                stamp = quote(datetime.fromtimestamp(start, timezone.utc).isoformat(), safe="")
                end_time = datetime.fromtimestamp(end, timezone.utc).isoformat()
                histories = self.history_request("/history/period/" + stamp + "?" + urlencode({
                    "filter_entity_id": ",".join(ids), "end_time": end_time, "significant_changes_only": "0"}))
                series = history_series(histories, config, states, self.ha._temperature_unit, start, end)["series"]
                rows = [(line["entity_id"], h, v) for line in series
                        for h, v in hourly_means(line["points"], start, end).items()]
                del histories, series
                offset_rows = history_groups(self.history_request("/history/period/" + stamp + "?" + urlencode({
                    "filter_entity_id": config["offset_entity"], "end_time": end_time,
                    "minimal_response": "", "no_attributes": "", "significant_changes_only": "0"})))
                offset_steps = numeric_steps(offset_rows.get(config["offset_entity"], []), config["offset_entity"], start)
                rows += [(config["offset_entity"], h, v) for h, v in hourly_means(offset_steps, start, end).items()]
                self.store.add(rows, now)
                del offset_rows, offset_steps, rows
                start = end
            stored = self.store.load(rooms + [config["outdoor_entity"], config["offset_entity"]])
            hours = set.intersection(*(set(stored[e]) for e in rooms))
            indoor = {h: sum(stored[e][h] for e in rooms) / len(rooms) for h in hours}
            result.update(indoor=indoor, outdoor=stored.get(config["outdoor_entity"], {}), offset=stored[config["offset_entity"]])
            training = {h: value for h, value in indoor.items() if not any(
                e["start"] - 3600 <= h <= (e["end"] + 6 * 3600 if e["end"] is not None else now)
                for e in episodes)}
            result["excluded_hours"] = len(indoor) - len(training)
            result["model"] = learn(training, result["outdoor"], result["offset"], now)
            if result["excluded_hours"]:
                result["model"]["message"] += (
                    f" {result['excluded_hours']} timmar kring misstänkt extravärme undantas från pumpinlärningen."
                )
            return result
        return self.cached(key, 3600, load)

    def preheat(self, config):
        """Pre-heating windows from the selected price source and learned lead time."""
        if config["preheat_enabled"]:
            entity = fresh_entity({s["entity_id"]: s for s in self.ha.states()}, config["price_entity"],
                                  config["max_age_minutes"], time.time())
            number(entity["state"], "Aktuellt elpris")
        key = ("preheat", tuple(control_rooms(config)), config["offset_entity"], config["preheat_degrees"],
               config["price_provider"], config["price_entity"], config["max_age_minutes"],
               tuple((e["start"], e["end"]) for e in self.fireplace_intervals()))

        def load():
            prices = self.prices(config)
            model = None
            try:
                model = self.house_model(config)["model"]
            except Exception:  # the model only tunes the lead time; defaults work without it
                model = None
            lead = lead_hours(model, config["preheat_degrees"])
            result = preheat_windows(prices, time.time(), lead, config["preheat_degrees"])
            result["lead_hours"] = lead
            result["lead_source"] = "learned" if model and model["status"] == "good" else "default"
            return result
        return self.cached(key, 900, load)

    def learning(self, config, status, events=None):
        """Model summary, recent indoor temperature and a 24-hour prediction along the current plan."""
        data = self.house_model(config)
        now = time.time()
        model = data["model"]
        result = {"now": now, "error": data["error"], "model": model, "history": [], "prediction": [],
                  "fireplace_periods": [{"start": e["start"], "end": e["end"] if e["end"] is not None else now}
                                       for e in self.fireplace_intervals()
                                       if e["end"] is None or e["end"] >= now - 86400],
                  "target": config["target_temperature"], "preheat": {"enabled": config["preheat_enabled"],
                  "degrees": config["preheat_degrees"], "windows": [], "lead_hours": None, "lead_source": None, "note": None}}
        result["history"] = [[h + 1800, round(v, 2)] for h, v in sorted(data["indoor"].items()) if h >= now - 36 * 3600]
        try:
            schedule = self.preheat(config)
            result["preheat"].update(windows=schedule["windows"], lead_hours=schedule["lead_hours"],
                                     lead_source=schedule["lead_source"], note=schedule["note"] or None)
        except ControlError as exc:
            result["preheat"]["note"] = str(exc)
        if (status.get("fireplace") or {}).get("active_since") is not None:
            result["prediction_note"] = "Temperaturprognosen väntar: misstänkt extravärme har okänd fortsatt effekt."
            return result
        if not model or not model.get("beta") or status.get("indoor") is None:
            return result
        plan = self.plan(config, status, events)
        steps = sorted([[h, v] for h, v in data["offset"].items()] + [list(p) for p in plan["offset"]["planned"]])
        current = status.get("current_offset")
        offset_at = lambda t: (value_at(steps, t) if value_at(steps, t) is not None else current if current is not None else config["base_offset"])
        forecast = plan["outdoor"]["forecast"]
        outdoor_now = next((v for _, v in reversed(plan["outdoor"]["history"]) if v is not None), None)
        fallback = outdoor_now if outdoor_now is not None else (sum(data["outdoor"].values()) / len(data["outdoor"]) if data["outdoor"] else 0)
        outdoor_at = lambda t: (value_at(forecast, t) if forecast and value_at(forecast, t) is not None else fallback)
        result["prediction"] = predict(model, status["indoor"], now, offset_at, outdoor_at)
        return result
