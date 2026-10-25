"""Conservative, observational detection of local extra heat; never a fire/safety sensor."""

import copy
import math
from statistics import median

SAMPLE_SECONDS = 300
MAX_GAP = 900
MAX_DURATION = 18 * 3600
KEEP_SECONDS = 180 * 86400


def empty_state():
    return {"version": 1, "scope": [], "rooms": [], "samples": [], "candidate": None,
            "active": None, "episodes": []}


def validate_state(state):
    """Reject damaged persisted observations instead of inventing a clean history."""
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    def ids(value):
        return isinstance(value, list) and len(value) <= 20 and len(set(value)) == len(value) \
            and all(isinstance(e, str) and e.startswith(("sensor.", "climate.")) for e in value)

    def values(value):
        return isinstance(value, dict) and len(value) <= 20 and all(
            isinstance(e, str) and finite(v) and 5 <= v <= 40 for e, v in value.items())

    def episode(value, completed=False):
        if not isinstance(value, dict) or not ids(value.get("scope")) or not ids(value.get("rooms")):
            return False
        if not value["rooms"] or not set(value["rooms"]) < set(value["scope"]):
            return False
        if not finite(value.get("start")) or not values(value.get("baseline")) or not values(value.get("peaks")):
            return False
        if set(value["baseline"]) != set(value["scope"]) or set(value["peaks"]) != set(value["scope"]):
            return False
        if not isinstance(value.get("arrival"), dict) or not all(
            isinstance(e, str) and finite(v) and v >= 0 for e, v in value["arrival"].items()):
            return False
        if not set(value["arrival"]) <= set(value["scope"]):
            return False
        if value.get("settled_at") is not None and not finite(value["settled_at"]):
            return False
        return not completed or (finite(value.get("end")) and value["end"] >= value["start"]
                                 and type(value.get("complete")) is bool)

    try:
        valid = isinstance(state, dict) and state.get("version") == 1 \
            and ids(state.get("scope")) and ids(state.get("rooms")) \
            and set(state["rooms"]) <= set(state["scope"]) \
            and isinstance(state.get("samples"), list) and len(state["samples"]) <= 300 \
            and all(isinstance(s, dict) and finite(s.get("at")) and values(s.get("values"))
                    and set(s["values"]) == set(state["scope"])
                    for s in state["samples"]) \
            and all(a["at"] < b["at"] for a, b in zip(state["samples"], state["samples"][1:])) \
            and (state.get("candidate") is None or finite(state["candidate"])) \
            and (state.get("active") is None or episode(state["active"])) \
            and (state.get("active") is None or (state["active"]["scope"] == state["scope"]
                 and state["active"]["rooms"] == state["rooms"] and bool(state["samples"]))) \
            and isinstance(state.get("episodes"), list) and len(state["episodes"]) <= 100 \
            and all(episode(e, True) for e in state["episodes"])
    except (TypeError, KeyError):
        valid = False
    if not valid:
        raise ValueError("Ogiltiga sparade observationer för braskamin.")
    return state


def intervals(state):
    """All suspected episodes, including incomplete ones, are excluded from pump learning."""
    rows = list(state["episodes"])
    if state["active"]:
        rows.append({**state["active"], "end": None})
    return [{"start": row["start"], "end": row.get("end")} for row in rows]


def summary(state, config, now, phase, message):
    matching = [e for e in state["episodes"] if e["complete"]
                and e["scope"] == sorted(config["room_sensors"]) and e["rooms"] == sorted(config["fireplace_rooms"])]
    durations = [(e["end"] - e["start"]) / 3600 for e in matching]
    learned = len(matching) >= 3
    room_effects = []
    for key in config["room_sensors"]:
        rises = [max(0, e["peaks"][key] - e["baseline"][key]) for e in matching if key in e["baseline"] and key in e["peaks"]]
        arrivals = [e["arrival"][key] / 60 for e in matching if key in e["arrival"]]
        if rises:
            room_effects.append({"entity_id": key, "rise": round(median(rises), 2),
                                 "arrival_minutes": round(median(arrivals)) if arrivals else None})
    active = state["active"]
    return {"enabled": config["fireplace_enabled"], "phase": phase, "message": message,
            "rooms": config["fireplace_rooms"], "active_since": active["start"] if active else None,
            "completed": len(matching), "learned": learned,
            "duration_hours": round(median(durations), 1) if learned else None,
            "room_effects": room_effects if learned else [],
            "recent": [{"start": e["start"], "end": e["end"], "complete": e["complete"]}
                       for e in state["episodes"] if e["rooms"] == sorted(config["fireplace_rooms"])][-5:]}


def update(state, config, readings, now):
    """Return a replacement state, UI summary and auditable transitions. No physical commands."""
    state = copy.deepcopy(state)
    events = []
    scope, rooms = sorted(config["room_sensors"]), sorted(config["fireplace_rooms"])

    def finish(end, complete, reason):
        active = state["active"]
        if active:
            state["episodes"].append({**active, "end": max(active["start"], end), "complete": complete})
            events.append({"kind": "fireplace_end", "message": reason, "start": active["start"],
                           "end": max(active["start"], end), "complete": complete, "rooms": active["rooms"]})
        state["active"], state["candidate"] = None, None

    state["episodes"] = [e for e in state["episodes"] if e["end"] >= now - KEEP_SECONDS][-99:]
    if scope != state["scope"] or rooms != state["rooms"] or not config["fireplace_enabled"]:
        end = min(now, state["samples"][-1]["at"] + SAMPLE_SECONDS) if state["samples"] else now
        finish(end, False, "Observation av extravärme avbruten: kaminval ändrade eller avstängda.")
        state.update(scope=scope, rooms=rooms, samples=[], candidate=None)
    if not config["fireplace_enabled"]:
        return state, summary(state, config, now, "off", "Kaminobservation är av."), events
    references = [e for e in scope if e not in rooms]
    if not references:
        return state, summary(state, config, now, "insufficient",
                              "Välj även minst ett rum utan kamin som jämförelse. Inget misstänks utan jämförelse."), events
    values = {r["entity_id"]: r["temperature"] for r in readings
              if r["temperature"] is not None and not r.get("error")}
    if any(e not in values for e in scope):
        if state["samples"] and now - state["samples"][-1]["at"] > MAX_GAP:
            finish(state["samples"][-1]["at"], False, "Observation av extravärme avbruten: aktuella rumsvärden saknas.")
            state["samples"], state["candidate"] = [], None
        return state, summary(state, config, now, "unavailable",
                              "Aktuella temperaturer saknas i något valt rum. Ingen upptäckt eller varaktighetsbedömning."), events
    samples = state["samples"]
    if samples and (now < samples[-1]["at"] or now - samples[-1]["at"] > MAX_GAP):
        finish(samples[-1]["at"], False, "Observation av extravärme avbruten: uppehåll i avläsningen.")
        samples = state["samples"] = []
        state["candidate"] = None
    sampled = not samples or now - samples[-1]["at"] >= SAMPLE_SECONDS
    if sampled:
        samples.append({"at": now, "values": {e: values[e] for e in scope}})
        state["samples"] = samples = [s for s in samples if s["at"] >= now - 24 * 3600]
    active = state["active"]
    if active and sampled:
        for key in scope:
            active["peaks"][key] = max(active["peaks"][key], values[key])
            if values[key] - active["baseline"][key] >= 0.3 - 1e-6 and key not in active["arrival"]:
                active["arrival"][key] = now - active["start"]
        local = sum(values[e] - active["baseline"][e] for e in rooms) / len(rooms)
        other = sum(values[e] - active["baseline"][e] for e in references) / len(references)
        if local - other <= 0.2:
            if active["settled_at"] is None:
                active["settled_at"] = now
            if now - active["settled_at"] >= 1800 and now - active["start"] >= 2700:
                finish(now, True, "Den lokala extravärmen har klingat av jämfört med övriga rum.")
        else:
            active["settled_at"] = None
        if state["active"] and now - active["start"] >= MAX_DURATION:
            finish(now, False, "Observation av extravärme avbruten efter 18 timmar; förloppet är osäkert.")
    if not state["active"] and sampled:
        past = [s for s in samples if 2700 <= now - s["at"] <= 4500]
        if past:
            baseline = min(past, key=lambda s: abs(now - s["at"] - 3600))
            hours = (now - baseline["at"]) / 3600
            local = sum(values[e] - baseline["values"][e] for e in rooms) / len(rooms)
            other = sum(values[e] - baseline["values"][e] for e in references) / len(references)
            suspect = local >= 0.5 and local / hours >= 0.7 and local - other >= 0.5
            if suspect:
                if state["candidate"] is None:
                    state["candidate"] = now
                elif now - state["candidate"] >= 600:
                    observed = [s for s in samples if s["at"] >= baseline["at"]]
                    state["active"] = {"scope": scope, "rooms": rooms, "start": baseline["at"],
                                       "baseline": baseline["values"],
                                       "peaks": {e: max(s["values"][e] for s in observed) for e in scope},
                                       "arrival": {}, "settled_at": None}
                    for key in scope:
                        arrival = next((s["at"] for s in observed
                                        if s["values"][key] - baseline["values"][key] >= 0.3 - 1e-6), None)
                        if arrival is not None:
                            state["active"]["arrival"][key] = arrival - baseline["at"]
                    state["candidate"] = None
                    events.append({"kind": "fireplace_start", "message": "Misstänkt lokal extravärme i kaminrum. Sol eller annan värme kan ge samma mönster.",
                                   "start": baseline["at"], "rooms": rooms})
            else:
                state["candidate"] = None
    if state["active"]:
        phase, message = "active", "Misstänkt extravärme i kaminrum. Det kan vara eldning, sol eller annan lokal värme."
    elif state["candidate"] is not None:
        phase, message = "candidate", "Kaminrummet värms snabbare än övriga rum. Väntar på ett bestående mönster."
    elif not samples or now - samples[0]["at"] < 2700:
        phase, message = "learning", "Samlar jämförelsevärden. Minst 45 minuter med sammanhängande avläsningar behövs."
    else:
        phase, message = "idle", "Ingen tydlig lokal extravärme upptäckt. Det utesluter inte eldning."
    return state, summary(state, config, now, phase, message), events
