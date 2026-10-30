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


def samples(rows, entity_id, prices=False, power=False):
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
            error = meter_error({"entity_id": entity_id, "attributes": attrs})
            if error:
                raise ControlError(error)
            units = POWER_UNITS if power else ENERGY_UNITS
            if attrs["unit_of_measurement"] not in units:
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


def consumption_report(meter_rows, price_rows, meter_id, price_id, start, end, price_max_age, power=False):
    meter, _ = samples(meter_rows, meter_id, power=power)
    price_error = None
    try:
        prices, currency = samples(price_rows, price_id, prices=True)
    except ControlError as exc:
        prices, currency, price_error = [], None, str(exc)
    if not prices and not price_error:
        price_error = "Prishistorik saknas. Ingen kostnad beräknas utan historiska priser."
    hours = [{
        "start": stamp, "end": min(end, stamp + 3600), "measured_kwh": 0.0,
        "priced_kwh": 0.0, "partial_cost": 0.0, "coverage_seconds": 0.0, "priced_seconds": 0.0,
    } for stamp in range(int(start), int(end), 3600)]
    price_segments = []
    for index, (stamp, value) in enumerate(prices):
        until = min(end, stamp + price_max_age, prices[index + 1][0] if index + 1 < len(prices) else end)
        if value is not None and until > stamp:
            price_segments.append((stamp, until, value))
    skipped = 0
    price_starts = [segment[0] for segment in price_segments]
    for (left, previous), (right, current) in zip(meter, meter[1:]):
        if right <= start or left >= end:
            continue
        delta = None
        if current is not None and previous is not None:
            delta = previous * ((right - left) / 3600) if power else current - previous
        if delta is None or not math.isfinite(delta) or delta < 0 or right - left > (
                MAX_POWER_GAP_SECONDS if power else MAX_GAP_SECONDS):
            skipped += 1
            continue
        rate = delta / (right - left)
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
