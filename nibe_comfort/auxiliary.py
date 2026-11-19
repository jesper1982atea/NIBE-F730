"""Observed auxiliary-heater runs, not inferred pump trigger decisions."""

import math
from bisect import bisect_right

from controller import ControlError, clean_state, entity_base, operation_sensors
from energy import BRIDGE_GAP_SECONDS, POWER_UNITS, bridge_gaps, consumption_report, price_intervals, samples, timestamp, usage_intervals


CONTEXT = {
    "operation": ("_status", "Driftläge"),
    "outdoor": ("_current_outd_temp_bt1", "Utetemperatur"),
    "degree_minutes": ("_degree_minutes", "Gradminuter"),
    "hot_water": ("_hot_water_top_bt7", "Varmvatten"),
}
# myUplink writes a row only when a value changes (and again when the integration comes back
# after an outage), so a value without newer rows is still the current one. Older than this
# and it is no longer shown as what happened at the start of a run.
CONTEXT_MAX_AGE_SECONDS = 3 * 3600


def heater_entities(states, pump_ids):
    if not pump_ids:
        raise ControlError("Välj en myUplink-pump för att följa elpatronen.")
    selected = [s for s in states if s["entity_id"] in pump_ids]
    candidates = [s for s in selected if entity_base(s["entity_id"]).endswith("_power_internal_add_heat")]
    if not candidates:
        candidates = [s for s in selected if entity_base(s["entity_id"]).endswith("_int_elec_add_heat")
                      and s["attributes"].get("unit_of_measurement") in POWER_UNITS]
    if len(candidates) != 1:
        raise ControlError("Ingen entydig givare för elpatronens egen eleffekt hittades på vald pump. "
                           "Pumpens totala effekt eller producerad värme används inte som elpatroneffekt.")
    power = candidates[0]
    if power["attributes"].get("unit_of_measurement") not in POWER_UNITS:
        raise ControlError("Elpatronens effektgivare måste rapportera W eller kW.")
    contexts = {}
    for key, (suffix, _) in CONTEXT.items():
        matches = operation_sensors(selected) if key == "operation" else [
            s for s in selected if entity_base(s["entity_id"]).endswith(suffix)]
        if len(matches) == 1:
            contexts[key] = matches[0]
    return power, contexts


def context_history(rows):
    points = {}
    attrs = {}
    for row in rows:
        attrs = row.get("attributes", attrs)
        if not isinstance(attrs, dict):
            raise ControlError("Drifthistoriken saknar giltiga givaruppgifter.")
        value = row.get("state")
        points[timestamp(row)] = None if value in ("unknown", "unavailable", None) else {
            "value": clean_state(str(value)), "unit": attrs.get("unit_of_measurement", ""),
        }
    return sorted(points.items())


def context_at(points, stamp):
    """The value a sensor showed at ``stamp``: its last valid report, unless that is older than
    CONTEXT_MAX_AGE_SECONDS or the sensor had been unavailable for longer than a short outage."""
    index = bisect_right(points, stamp, key=lambda point: point[0]) - 1
    if index >= 0 and points[index][1] is None:
        outage = index
        while outage > 0 and points[outage - 1][1] is None:
            outage -= 1
        if stamp - points[outage][0] > BRIDGE_GAP_SECONDS:
            return None
        index = outage - 1
    if index < 0 or stamp - points[index][0] > CONTEXT_MAX_AGE_SECONDS:
        return None
    return {**points[index][1], "reported_at": points[index][0]}


def heater_report(rows, price_rows, power_id, price_id, contexts, start, end, price_max_age):
    report = consumption_report(rows, price_rows, power_id, price_id, start, end, price_max_age,
                                power=True, hold_zero=True)
    readings, _ = samples(rows, power_id, power=True)
    readings, _ = bridge_gaps(readings)
    intervals, _ = usage_intervals(readings, start, end, power=True, hold_zero=True)
    prices, _, _ = price_intervals(price_rows, price_id, end, price_max_age)
    context_points = {key: context_history(group) for key, group in contexts.items()}
    runs = []
    for index, (left, right, rate) in enumerate(intervals):
        if rate <= 0:
            continue
        if not runs or runs[-1]["end"] != left:
            previous = intervals[index - 1] if index else None
            runs.append({
                "start": left, "end": right, "seconds": 0, "kwh": 0.0,
                "partial_cost": 0.0, "priced_seconds": 0.0, "max_kw": 0.0,
                "start_observed": bool(previous and previous[1] == left and previous[2] == 0 and left > start),
                "end_observed": False,
                "context": {key: context_at(points, left) for key, points in context_points.items()},
            })
        run = runs[-1]
        run["end"] = right
        run["seconds"] += right - left
        run["kwh"] += rate * (right - left)
        run["max_kw"] = max(run["max_kw"], rate * 3600)
        following = intervals[index + 1] if index + 1 < len(intervals) else None
        run["end_observed"] = bool(following and following[0] == right and following[2] == 0 and right < end)
        run["continues"] = right >= end
        for price_start, price_end, price in prices:
            seconds = max(0, min(right, price_end) - max(left, price_start))
            run["priced_seconds"] += seconds
            run["partial_cost"] += rate * seconds * price
    for run in runs:
        run["cost"] = run["partial_cost"] if math.isclose(run["seconds"], run["priced_seconds"], abs_tol=0.01) else None
    if any(not math.isfinite(run[key]) for run in runs
           for key in ("seconds", "kwh", "partial_cost", "priced_seconds", "max_kw")):
        raise ControlError("Elpatronhistoriken gav för stora värden för en säker beräkning.")
    report.update(
        runs=runs,
        runtime_seconds=sum(run["seconds"] for run in runs),
        observed_starts=sum(run["start_observed"] for run in runs),
        context_sources={key: CONTEXT[key][1] for key in contexts},
    )
    return report
