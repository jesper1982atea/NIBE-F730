"""Read-only consumption accounting from electricity or power measurements."""

import math
import re
from bisect import bisect_right
from datetime import datetime

from controller import ControlError, number


ENERGY_UNITS = {"Wh": 0.001, "kWh": 1, "MWh": 1000}
POWER_UNITS = {"W": 0.001, "kW": 1}
MAX_GAP_SECONDS = 7200
MAX_POWER_GAP_SECONDS = 900
# House flow counters often step 0.1 kWh at a time and can stand still for hours, so an
# increase is spread evenly over the time since the previous step, up to a day.
MAX_SOURCE_GAP_SECONDS = 86400


def produced_energy(entity):
    attrs = entity.get("attributes", {})
    text = (entity["entity_id"] + " " + attrs.get("friendly_name", "")).lower()
    return any(word in text for word in ("produced", "production", "producerad", "produktion", "yield"))


def meter_error(entity):
    attrs = entity.get("attributes", {})
    text = (entity["entity_id"] + " " + attrs.get("friendly_name", "")).lower()
    if produced_energy(entity):
        return "Producerad energi är inte använd el."
    if (re.search(r"(?:current|previous|last|this|past)_(?:hour|day|week|month|year)|(?:^|_)\d+_years?_consumption(?:$|[_\s])", text)
            or re.search(r"\b\d+ years?[’']? consumption\b", text)
            or any(word in text for word in ("current hour", "denna timme", "daily", "weekly", "monthly", "today", "yesterday", "i dag", "idag"))):
        return "Periodräknare stöds inte här. Välj en ackumulerad elmätare eller pumpens totala eleffekt."
    if (entity["entity_id"].startswith("sensor.") and attrs.get("device_class") == "power"
            and attrs.get("unit_of_measurement") in POWER_UNITS):
        return None
    if (not entity["entity_id"].startswith("sensor.") or attrs.get("device_class") != "energy"
            or attrs.get("unit_of_measurement") not in ENERGY_UNITS
            or attrs.get("state_class") not in ("total", "total_increasing")):
        return "Välj en ackumulerad energimätare med Wh/kWh/MWh och state_class total/total_increasing, eller en eleffektgivare med W/kW."
    return None


def discover_meter(states, registry, offset_entity):
    pump = next((r for r in registry if r["entity_id"] == offset_entity
                 and r.get("platform") == "myuplink" and r.get("device_id")), None)
    if not pump:
        raise ControlError("Välj pumpanslutning först. Ingen entydig myUplink-pump hittades för automatisk elmätning.")
    ids = {r["entity_id"] for r in registry if r.get("platform") == "myuplink"
           and r.get("device_id") == pump["device_id"]}
    candidates, power_candidates = [], []
    for entity in states:
        text = (entity["entity_id"] + " " + entity["attributes"].get("friendly_name", "")).lower()
        if entity["entity_id"] not in ids or meter_error(entity) is not None:
            continue
        if entity["attributes"].get("device_class") == "power":
            # Only the known total input, never a component or auxiliary heater.
            if entity["entity_id"].endswith("_el_current_power_consumption"):
                power_candidates.append(entity)
        elif any(
            word in text for word in ("energy_spent", "consumed", "consumption", "electrical", "electricity",
                                      "elförbrukning", "elanvändning", "använd el", "förbrukad el")
        ):
            candidates.append(entity)
    candidates = candidates or power_candidates
    candidates = {e["entity_id"]: e for e in candidates}
    if not candidates:
        raise ControlError("Ingen entydig ackumulerad elmätare eller total eleffektgivare hittades på vald myUplink-pump. "
                           "Producerad värme och periodräknare används inte. Välj en separat elmätare manuellt vid behov.")
    if len(candidates) != 1:
        raise ControlError("Flera möjliga elmätare finns på myUplink-pumpen: " + ", ".join(sorted(candidates))
                           + ". De kan överlappa eller avse olika delar. Välj elmätare manuellt; ingen summeras automatiskt.")
    return next(iter(candidates.values()))


def timestamp(row):
    value = row.get("last_updated") or row.get("last_changed")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timezone")
        return stamp.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ControlError("Energihistoriken innehåller en ogiltig tidsstämpel.") from exc


def price_unit(unit):
    match = re.fullmatch(r"(SEK|EUR|NOK|DKK|USD|GBP|kr|öre)/(kWh|MWh)", unit, re.I) if isinstance(unit, str) else None
    if not match:
        raise ControlError("Kostnad kräver en prisgivare med valuta/kWh eller valuta/MWh, exempelvis SEK/kWh.")
    currency, scale = match.groups()
    factor = 0.001 if scale.lower() == "mwh" else 1
    if currency.lower() == "öre":
        return "SEK", factor * 0.01
    return ("SEK" if currency.lower() == "kr" else currency.upper()), factor


def samples(rows, entity_id, prices=False, power=False, check_meter=True):
    attrs = {}
    result = {}
    currency = None
    last_reset = None
    for row in rows:
        if not isinstance(row, dict):
            raise ControlError("Home Assistant returnerade ogiltig energihistorik.")
        if "attributes" in row:
            attrs = row["attributes"]
        if not isinstance(attrs, dict):
            raise ControlError("Energihistoriken saknar giltiga givaruppgifter.")
        scale = 1
        if prices:
            current_currency, scale = price_unit(attrs.get("unit_of_measurement", ""))
            if currency is not None and current_currency != currency:
                raise ControlError("Prisgivarens valuta har ändrats under perioden. Kostnaden kan inte summeras.")
            currency = current_currency
        else:
            error = meter_error({"entity_id": entity_id, "attributes": attrs}) if check_meter else None
            if error:
                raise ControlError(error)
            units = POWER_UNITS if power else ENERGY_UNITS
            if attrs.get("unit_of_measurement") not in units:
                raise ControlError("Givarens mätmetod har ändrats under perioden. Historiken kan inte kombineras.")
            scale = units[attrs["unit_of_measurement"]]
        value = None
        if row.get("state") not in ("unknown", "unavailable", None):
            try:
                value = number(row["state"], entity_id) * scale
            except ControlError:
                value = None
        if not prices and value is not None and value < 0:
            value = None
        if not prices and result and attrs.get("last_reset") != last_reset:
            value = None
        last_reset = attrs.get("last_reset")
        if value is not None and not math.isfinite(value):
            raise ControlError("Energihistoriken innehåller ett för stort tal.")
        result[timestamp(row)] = value
    return sorted(result.items()), currency


def price_intervals(price_rows, price_id, end, price_max_age):
    price_error = None
    try:
        prices, currency = samples(price_rows, price_id, prices=True)
    except ControlError as exc:
        prices, currency, price_error = [], None, str(exc)
    if not prices and not price_error:
        price_error = "Prishistorik saknas. Ingen kostnad beräknas utan historiska priser."
    price_segments = []
    for index, (stamp, value) in enumerate(prices):
        until = min(end, stamp + price_max_age, prices[index + 1][0] if index + 1 < len(prices) else end)
        if value is not None and until > stamp:
            price_segments.append((stamp, until, value))
    return price_segments, currency, price_error


def usage_intervals(meter, start, end, power=False, hold_zero=False):
    """Intervals with a known rate, and how many were skipped.

    Auxiliary power can retain a recorded zero until its next state change or
    ``end``. Unknown/unavailable rows break that hold. Non-zero power keeps the
    normal gap limit and is never extrapolated after its last report.
    """
    if power and hold_zero and meter and meter[-1][0] < end and meter[-1][1] == 0:
        meter = [*meter, (end, None)]
    intervals = []
    skipped = 0
    for (left, previous), (right, current) in zip(meter, meter[1:]):
        if right <= start or left >= end:
            continue
        if power and hold_zero and previous == 0:
            intervals.append((max(start, left), min(end, right), 0.0))
            continue
        delta = None
        if current is not None and previous is not None:
            delta = previous * ((right - left) / 3600) if power else current - previous
        if delta is None or not math.isfinite(delta) or delta < 0 or right - left > (
                MAX_POWER_GAP_SECONDS if power else MAX_GAP_SECONDS):
            skipped += 1
            continue
        intervals.append((max(start, left), min(end, right), delta / (right - left)))
    return intervals, skipped


def consumption_report(meter_rows, price_rows, meter_id, price_id, start, end, price_max_age, power=False, hold_zero=False):
    meter, _ = samples(meter_rows, meter_id, power=power)
    price_segments, currency, price_error = price_intervals(price_rows, price_id, end, price_max_age)
    hours = [{
        "start": stamp, "end": min(end, stamp + 3600), "measured_kwh": 0.0,
        "priced_kwh": 0.0, "partial_cost": 0.0, "coverage_seconds": 0.0, "priced_seconds": 0.0,
    } for stamp in range(int(start), int(end), 3600)]
    intervals, skipped = usage_intervals(meter, start, end, power, hold_zero)
    price_starts = [segment[0] for segment in price_segments]
    for left, right, rate in intervals:
        for hour in hours:
            low, high = max(left, hour["start"]), min(right, hour["end"])
            if high <= low:
                continue
            hour["coverage_seconds"] += high - low
            hour["measured_kwh"] += rate * (high - low)
            first = max(0, bisect_right(price_starts, low) - 1)
            for index in range(first, len(price_segments)):
                price_start, price_end, price = price_segments[index]
                if price_start >= high:
                    break
                duration = max(0, min(high, price_end) - max(low, price_start))
                hour["priced_seconds"] += duration
                hour["priced_kwh"] += rate * duration
                hour["partial_cost"] += rate * duration * price
    for hour in hours:
        full = math.isclose(hour["coverage_seconds"], hour["end"] - hour["start"], abs_tol=0.01)
        priced = math.isclose(hour["priced_seconds"], hour["end"] - hour["start"], abs_tol=0.01)
        hour["kwh"] = hour["measured_kwh"] if full else None
        hour["cost"] = hour["partial_cost"] if full and priced else None
    totals = {key: sum(hour[key] for hour in hours) for key in
              ("measured_kwh", "priced_kwh", "partial_cost", "coverage_seconds", "priced_seconds")}
    totals["kwh"] = totals["measured_kwh"] if all(h["kwh"] is not None for h in hours) else None
    totals["cost"] = totals["partial_cost"] if all(h["cost"] is not None for h in hours) else None
    if any(isinstance(value, (int, float)) and not math.isfinite(value)
           for entry in [*hours, totals] for value in entry.values()):
        raise ControlError("Energihistoriken gav för stora värden för en säker kostnadsberäkning.")
    if not price_error and totals["priced_seconds"] + 0.01 < totals["coverage_seconds"]:
        price_error = "Historiska priser saknas, är otillgängliga eller för gamla för delar av perioden. Endast prissatt del ingår."
    return {
        "start": start, "end": end, "hours": hours, "totals": totals, "currency": currency,
        "price_error": price_error, "skipped_intervals": skipped,
        "method": "power" if power else "counter",
    }


def hourly_energy(rows, entity_id, start, end, power=False):
    """kWh per whole hour for one of the house's electricity flows; None where an hour is not covered.

    Counters such as solar yield stay unchanged for hours at night and step 0.1 kWh at a
    time. Home Assistant then records no new rows, so an increase is spread over the time
    since the previous step (at most a day), and the last value is held until ``end``.
    """
    meter, _ = samples(rows, entity_id, power=power, check_meter=False)
    if not power and meter and meter[-1][0] < end and meter[-1][1] is not None:
        meter = [*meter, (end, meter[-1][1])]
    intervals = []
    for (left, previous), (right, current) in zip(meter, meter[1:]):
        if right <= start or left >= end or previous is None or current is None:
            continue
        if power:
            if right - left > MAX_POWER_GAP_SECONDS:
                continue
            delta = previous * ((right - left) / 3600)
        else:
            delta = current - previous
            if delta < 0 or right - left > MAX_SOURCE_GAP_SECONDS:
                continue
        intervals.append((max(start, left), min(end, right), delta / (right - left)))
    hours = []
    for stamp in range(int(start), int(end), 3600):
        covered = kwh = 0.0
        for left, right, rate in intervals:
            low, high = max(left, stamp), min(right, stamp + 3600)
            if high > low:
                covered += high - low
                kwh += rate * (high - low)
        hours.append(kwh if math.isclose(covered, min(3600, end - stamp), abs_tol=0.01) else None)
    return hours


def split_sources(flows, start, end):
    """Where the house's electricity came from each hour.

    House use = bought + solar + battery out - sold - battery in. What was not bought came
    from the battery (at most what it delivered) and the rest straight from the panels.
    """
    count = len(range(int(start), int(end), 3600))
    get = lambda key, index: flows[key][index] if key in flows else 0.0
    hours = []
    for index, stamp in enumerate(range(int(start), int(end), 3600)):
        hour = {"start": stamp, "end": min(end, stamp + 3600), "house_kwh": None, "grid_kwh": None,
                "solar_kwh": None, "battery_kwh": None, "grid_share": None, "solar_share": None, "battery_share": None}
        values = {key: get(key, index) for key in ("grid_import", "grid_export", "solar", "battery_discharge", "battery_charge")}
        if any(value is None for value in values.values()):
            hours.append(hour)
            continue
        house = (values["grid_import"] + values["solar"] + values["battery_discharge"]
                 - values["grid_export"] - values["battery_charge"])
        if house < -0.001:
            hours.append(hour)
            continue
        if house <= 0.001:
            hour.update(house_kwh=0.0, grid_kwh=0.0, solar_kwh=0.0, battery_kwh=0.0)
            hours.append(hour)
            continue
        local = max(0.0, house - values["grid_import"])
        battery = min(values["battery_discharge"], local)
        hour.update(house_kwh=house, grid_kwh=house - local, solar_kwh=local - battery, battery_kwh=battery,
                    grid_share=(house - local) / house, solar_share=(local - battery) / house, battery_share=battery / house)
        hours.append(hour)
    assert len(hours) == count
    return hours


def pump_sources(pump_hours, source_hours):
    """The pump gets the same mix as the whole house in each hour (an estimate, not a measurement)."""
    by_start = {hour["start"]: hour for hour in source_hours}
    result = []
    for pump in pump_hours:
        house = by_start.get(pump["start"])
        row = {"start": pump["start"], "end": pump["end"], "kwh": pump["kwh"], "grid_kwh": None, "solar_kwh": None,
               "battery_kwh": None, "grid_cost": None, "avoided_cost": None}
        if (pump["kwh"] is not None and house and house["grid_share"] is not None
                and pump["kwh"] <= house["house_kwh"] + 0.001):
            row.update(grid_kwh=pump["kwh"] * house["grid_share"], solar_kwh=pump["kwh"] * house["solar_share"],
                       battery_kwh=pump["kwh"] * house["battery_share"])
            if pump["cost"] is not None:
                row.update(grid_cost=pump["cost"] * house["grid_share"], avoided_cost=pump["cost"] * (1 - house["grid_share"]))
        result.append(row)
    keys = ("kwh", "grid_kwh", "solar_kwh", "battery_kwh", "grid_cost", "avoided_cost")
    totals = {key: sum(values) if values else None
              for key in keys for values in [[row[key] for row in result if row[key] is not None]]}
    totals["complete"] = all(row["grid_kwh"] is not None for row in result)
    return result, totals


def change_comparisons(hours, changes):
    """Only compare complete hour pairs; no attribution or savings claims."""
    result = []
    seen = set()
    for change in sorted(changes, key=lambda c: c["timestamp"]):
        if change["status"] not in ("confirmed", "observed", "saved"):
            continue
        stamp = change["timestamp"]
        key = (int(stamp), change["kind"], change.get("entity_id"), change.get("value"))
        if key in seen:
            continue
        seen.add(key)
        boundary = int(stamp // 3600) * 3600
        before = next((h for h in hours if h["start"] == boundary - 3600), None)
        after = next((h for h in hours if h["start"] == boundary + 3600), None)
        result.append({
            "timestamp": stamp, "kind": change["kind"], "entity_id": change.get("entity_id"),
            "value": change.get("value"), "label": change.get("label", "Återläst ändring"),
            "before": before, "after": after,
            "difference_kwh": after["kwh"] - before["kwh"]
            if before and after and before["kwh"] is not None and after["kwh"] is not None else None,
            "difference_cost": after["cost"] - before["cost"]
            if before and after and before["cost"] is not None and after["cost"] is not None else None,
        })
    return result[-20:]
