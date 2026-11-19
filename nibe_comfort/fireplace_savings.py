"""What a fire in the stove saved on the heat pump's electricity (an estimate, not a measurement).

Hours without fire teach how much electricity the pump uses per hour at a given outdoor temperature
(a straight line). During a fire and the six hours after it, the expected use from that line is compared
with what the pump actually used. The difference is the saving, including what the automation saved by
lowering the curve while the stove heated the house. Hot water and other heat make single hours noisy,
so every result carries a rough uncertainty and short fires are often too uncertain to say anything about.
"""

import math

HOUR = 3600
# Heat stored in the house keeps the pump idle for a while after the fire has died down.
AFTER_SECONDS = 6 * HOUR
MIN_BASELINE_HOURS = 72
MIN_OUTDOOR_SPAN = 2.0


def in_window(hour, episode):
    return episode["start"] - HOUR <= hour <= episode["end"] + AFTER_SECONDS


def baseline(points):
    """Least-squares line kWh = a + b * outdoor, and the spread of single hours around it."""
    n = len(points)
    if n < MIN_BASELINE_HOURS:
        return None
    mean_out = sum(o for o, _ in points) / n
    mean_kwh = sum(k for _, k in points) / n
    spread = sum((o - mean_out) ** 2 for o, _ in points)
    if spread <= 0 or max(o for o, _ in points) - min(o for o, _ in points) < MIN_OUTDOOR_SPAN:
        # All hours at nearly the same outdoor temperature: use the plain mean, no slope.
        slope = 0.0
    else:
        slope = sum((o - mean_out) * (k - mean_kwh) for o, k in points) / spread
    intercept = mean_kwh - slope * mean_out
    residuals = [k - (intercept + slope * o) for o, k in points]
    sd = math.sqrt(sum(r * r for r in residuals) / max(1, n - 2))
    return {"intercept": intercept, "slope": slope, "sd": sd, "hours": n,
            "outdoor_min": min(o for o, _ in points), "outdoor_max": max(o for o, _ in points)}


def fireplace_savings(hours, outdoor, episodes, now):
    """Saving per finished fire within the period of ``hours`` (energy report hours with kwh and cost).

    ``outdoor`` maps hour start to the mean outdoor temperature. Ongoing fires are left out until
    their aftermath is over, so a fire is never judged before the house has cooled down again.
    """
    finished = [e for e in episodes if e.get("end") is not None and e["end"] + AFTER_SECONDS <= now]
    ongoing = len(episodes) - len(finished)
    usable = [(h["start"], h["kwh"], outdoor[h["start"]], h.get("cost")) for h in hours
              if h.get("kwh") is not None and outdoor.get(h["start"]) is not None]
    clean = [(o, k) for start, k, o, _ in usable
             if not any(in_window(start, {**e, "end": e["end"] if e.get("end") is not None else now}) for e in episodes)]
    model = baseline(clean)
    result = {"model": model, "episodes": [], "ongoing": ongoing, "after_hours": AFTER_SECONDS // HOUR,
              "totals": {"fires": 0, "saved_kwh": 0.0, "saved_cost": None, "margin_kwh": 0.0, "fire_hours": 0.0}}
    if not model or not hours:
        return result
    period_start, period_end = hours[0]["start"], hours[-1]["end"]
    by_start = {start: (k, o, cost) for start, k, o, cost in usable}
    priced_any = False
    for episode in finished:
        if episode["end"] + AFTER_SECONDS < period_start or episode["start"] > period_end:
            continue
        window = [h["start"] for h in hours if in_window(h["start"], episode)]
        covered = [start for start in window if start in by_start]
        if not window:
            continue
        expected = sum(model["intercept"] + model["slope"] * by_start[s][1] for s in covered)
        actual = sum(by_start[s][0] for s in covered)
        saved_cost, priced = 0.0, 0
        for s in covered:
            kwh, out, cost = by_start[s]
            if cost is not None and kwh > 0:
                saved_cost += (model["intercept"] + model["slope"] * out - kwh) * cost / kwh
                priced += 1
        # Neighbouring hours are not independent (hot water, defrost), so the margin is doubled on top of 2 sd.
        margin = 2 * model["sd"] * math.sqrt(len(covered)) * 1.5 if covered else None
        saved = expected - actual if covered else None
        row = {
            "start": episode["start"], "end": episode["end"], "fire_hours": (episode["end"] - episode["start"]) / HOUR,
            "window_hours": len(window), "covered_hours": len(covered), "complete": len(covered) == len(window),
            "expected_kwh": expected if covered else None, "actual_kwh": actual if covered else None,
            "saved_kwh": saved, "margin_kwh": margin,
            "saved_cost": saved_cost if covered and priced == len(covered) else None,
            "clear": bool(covered) and saved > margin,
            "mean_outdoor": sum(by_start[s][1] for s in covered) / len(covered) if covered else None,
        }
        result["episodes"].append(row)
        if covered:
            totals = result["totals"]
            totals["fires"] += 1
            totals["saved_kwh"] += saved
            totals["margin_kwh"] = math.sqrt(totals["margin_kwh"] ** 2 + margin ** 2)
            totals["fire_hours"] += row["fire_hours"]
            if row["saved_cost"] is not None:
                totals["saved_cost"] = (totals["saved_cost"] or 0.0) + row["saved_cost"]
                priced_any = True
    result["totals"]["cost_complete"] = priced_any and all(
        e["saved_cost"] is not None for e in result["episodes"] if e["covered_hours"])
    return result
