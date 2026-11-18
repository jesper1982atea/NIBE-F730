"""Calendar-period summaries for measured costs and observed control changes."""

from collections import defaultdict
from datetime import datetime, timedelta

from controller import ControlError
from energy import price_shift


PERIOD_DAYS = (7, 30, 90)


def period_bounds(now, days):
    if type(days) is not int or days not in PERIOD_DAYS:
        raise ControlError("Välj en period på 7, 30 eller 90 dagar.")
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(days=days)).timestamp(), midnight.timestamp()


def energy_periods(hours, price_segments):
    daily = defaultdict(list)
    monthly = defaultdict(list)
    for hour in hours:
        local = datetime.fromtimestamp(hour["start"]).astimezone()
        daily[local.date().isoformat()].append(hour)
        monthly[local.strftime("%Y-%m")].append(hour)
    return {
        "daily": [summarize(key, entries, price_segments) for key, entries in sorted(daily.items())],
        "monthly": [summarize(key, entries, price_segments) for key, entries in sorted(monthly.items())],
    }


def summarize(period, hours, price_segments):
    measured = [hour for hour in hours if hour["kwh"] is not None]
    priced = [hour for hour in hours if hour["cost"] is not None]
    return {
        "period": period,
        "kwh": sum(hour["kwh"] for hour in measured) if measured else None,
        "cost": sum(hour["cost"] for hour in priced) if priced else None,
        "coverage_hours": sum(hour["coverage_seconds"] for hour in hours) / 3600,
        "priced_hours": sum(hour["priced_seconds"] for hour in hours) / 3600,
        "complete_hours": len(measured),
        "priced_complete_hours": len(priced),
        "total_hours": len(hours),
        "price_shift": price_shift(hours, price_segments),
    }


def action_periods(comparisons):
    all_actions = defaultdict(lambda: {"kind": "", "observations": 0, "estimated_savings": 0.0,
                                       "estimated_kwh_change": 0.0})
    daily = defaultdict(lambda: {"observations": 0, "estimated_savings": 0.0})
    monthly = defaultdict(lambda: {"observations": 0, "estimated_savings": 0.0})
    labels = {"offset": "Kurvförskjutning", "thermostat": "Termostat"}
    for change in comparisons:
        if change.get("kind") not in labels:
            continue
        before, after = change.get("before"), change.get("after")
        if not before or not after or before.get("cost") is None or after.get("cost") is None:
            continue
        savings = before["cost"] - after["cost"]
        kwh_change = before["kwh"] - after["kwh"]
        local = datetime.fromtimestamp(change["timestamp"]).astimezone()
        groups = (
            all_actions[change["kind"]],
            daily[local.date().isoformat()],
            monthly[local.strftime("%Y-%m")],
        )
        for group in groups:
            group["observations"] += 1
            group["estimated_savings"] += savings
        all_actions[change["kind"]]["kind"] = change["kind"]
        all_actions[change["kind"]]["estimated_kwh_change"] += kwh_change
    by_kind = sorted(
        [{**value, "label": labels[key]} for key, value in all_actions.items()],
        key=lambda item: item["estimated_savings"], reverse=True,
    )
    return {
        "by_kind": by_kind,
        "daily": [{"period": key, **value} for key, value in sorted(daily.items())],
        "monthly": [{"period": key, **value} for key, value in sorted(monthly.items())],
        "observations": sum(item["observations"] for item in by_kind),
    }
