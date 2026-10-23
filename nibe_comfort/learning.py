"""Self-learning house model and price-based pre-heating.

The house is modelled per hour as
    indoor[h+1] - indoor[h] = b0 + b1 * indoor[h] + b2 * offset[h - lag] + b3 * outdoor[h]
which is a first-order heat balance: the house drifts towards an equilibrium that depends on the
heating-curve offset (after a delay) and the outdoor temperature. Samples are kept per entity and
hour in SQLite so learning continues beyond Home Assistant's recorder retention.
"""

import math
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

HOUR = 3600
LAGS = (0, 1, 2, 3, 4, 6)
KEEP_DAYS = 180
MIN_HOURS = 72
GOOD_HOURS = 7 * 24
GOOD_CHANGES = 6
GOOD_SKILL = 0.05
DEFAULT_LEAD = 3
HORIZON = 6


class LearningStore:
    """Hourly mean per entity. Without a path the store only lives in memory (tests, no /data)."""

    def __init__(self, path=None):
        self.path = path
        self.memory = {}
        self.lock = threading.Lock()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS hourly (entity_id TEXT NOT NULL, hour INTEGER NOT NULL, "
                                   "value REAL NOT NULL, PRIMARY KEY (entity_id, hour))")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(self, rows, now):
        """rows: iterable of (entity_id, hour_start, value). Newer readings of the same hour replace older."""
        rows = [(e, int(h), float(v)) for e, h, v in rows if v is not None and math.isfinite(v)]
        oldest = int(now - KEEP_DAYS * 86400)
        with self.lock:
            if not self.path:
                for e, h, v in rows:
                    self.memory[(e, h)] = v
                for key in [k for k in self.memory if k[1] < oldest]:
                    del self.memory[key]
                return
            with self.connect() as connection:
                connection.executemany("INSERT OR REPLACE INTO hourly(entity_id,hour,value) VALUES (?,?,?)", rows)
                connection.execute("DELETE FROM hourly WHERE hour < ?", (oldest,))

    def load(self, entity_ids):
        """{entity_id: {hour: value}} for the given entities."""
        result = {e: {} for e in entity_ids}
        with self.lock:
            if not self.path:
                for (e, h), v in self.memory.items():
                    if e in result:
                        result[e][h] = v
                return result
            with self.connect() as connection:
                marks = ",".join("?" * len(entity_ids))
                for e, h, v in connection.execute(f"SELECT entity_id,hour,value FROM hourly WHERE entity_id IN ({marks})",
                                                  list(entity_ids)):
                    result[e][h] = v
        return result


def hourly_means(steps, start, end):
    """Mean of a step series per whole hour in [start, end), sampled every 5 minutes.

    An hour counts only when at least half of the samples have a value."""
    result = {}
    if not steps:
        return result
    index, value = 0, None
    hour = math.ceil(start / HOUR) * HOUR
    while hour + HOUR <= end:
        samples = []
        for k in range(12):
            stamp = hour + k * 300
            while index < len(steps) and steps[index][0] <= stamp:
                value = steps[index][1]
                index += 1
            if value is not None:
                samples.append(value)
        if len(samples) >= 6:
            result[hour] = sum(samples) / len(samples)
        hour += HOUR
    return result


def solve(matrix, vector):
    size = len(vector)
    rows = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        if abs(rows[column][column]) < 1e-12:
            return None
        for r in range(size):
            if r != column:
                factor = rows[r][column] / rows[column][column]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[column])]
    return [rows[i][size] / rows[i][i] for i in range(size)]


def least_squares(xs, ys, ridge=1e-6):
    size = len(xs[0])
    matrix = [[sum(x[i] * x[j] for x in xs) + (ridge * len(xs) if i == j and i else 0) for j in range(size)] for i in range(size)]
    vector = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(size)]
    return solve(matrix, vector)


def offset_input(offset, hour, lag):
    """Mean offset of the two hours ending `lag` hours before `hour`."""
    values = [offset.get(hour - (lag + k) * HOUR) for k in (0, 1)]
    return None if None in values else sum(values) / 2


def step(beta, indoor, offset, outdoor):
    b0, b1, b2, b3 = beta
    return indoor + b0 + b1 * indoor + b2 * offset + b3 * outdoor


def build_table(indoor, outdoor, offset):
    fallback = sum(outdoor.values()) / len(outdoor) if outdoor else 0.0
    return {h: (indoor[h], outdoor.get(h, fallback)) for h in indoor}


def fit(indoor, outdoor, offset):
    """Best model over the candidate lags, judged by 6-hour simulation against 'nothing changes'."""
    table = build_table(indoor, outdoor, offset)
    best = None
    for lag in LAGS:
        xs, ys = [], []
        for hour, (ti, to) in table.items():
            following = table.get(hour + HOUR)
            u = offset_input(offset, hour, lag)
            if following is None or u is None:
                continue
            xs.append([1.0, ti, u, to])
            ys.append(following[0] - ti)
        if len(xs) < 24:
            continue
        beta = least_squares(xs, ys)
        if beta is None or not all(math.isfinite(b) for b in beta):
            continue
        mean = sum(ys) / len(ys)
        total = sum((y - mean) ** 2 for y in ys) or 1e-9
        r2 = 1 - sum((y - sum(a * b for a, b in zip(x, beta))) ** 2 for x, y in zip(xs, ys)) / total
        model_error, base_error, count = 0.0, 0.0, 0
        for hour, (ti, _) in table.items():
            end = table.get(hour + HORIZON * HOUR)
            if end is None:
                continue
            value = ti
            for k in range(HORIZON):
                current = table.get(hour + k * HOUR)
                u = offset_input(offset, hour + k * HOUR, lag)
                if current is None or u is None:
                    value = None
                    break
                value = step(beta, value, u, current[1])
            if value is None:
                continue
            model_error += (value - end[0]) ** 2
            base_error += (ti - end[0]) ** 2
            count += 1
        skill = 1 - math.sqrt(model_error / count) / math.sqrt(base_error / count) if count and base_error > 0 else None
        candidate = {"beta": beta, "lag": lag, "samples": len(xs), "r2": r2, "skill": skill, "checks": count}
        key = (skill if skill is not None else -9, r2)
        if best is None or key > (best["skill"] if best["skill"] is not None else -9, best["r2"]):
            best = candidate
    return best


def describe(beta, lag):
    b0, b1, b2, b3 = beta
    if b1 >= 0:
        return None
    tau = -1 / b1
    gain = -b2 / b1
    if not 2 <= tau <= 500 or not 0.02 <= gain <= 3:
        return None
    after = lambda hours: gain * (1 - math.exp(-max(0, hours - lag) / tau))
    return {
        "time_constant_hours": round(tau, 1),
        "degrees_per_step": round(gain, 2),
        "outdoor_effect": round(-b3 / b1, 3),
        "lag_hours": lag,
        "step_after_3h": round(after(3), 2),
        "step_after_6h": round(after(6), 2),
        "half_response_hours": round(lag + tau * math.log(2), 1),
    }


def offset_changes(offset):
    hours = sorted(offset)
    return sum(1 for a, b in zip(hours, hours[1:]) if b - a == HOUR and abs(offset[b] - offset[a]) >= 0.5)


def learn(indoor, outdoor, offset, now):
    """Model summary with an honest confidence level: learning, rough or good."""
    hours = len(indoor)
    changes = offset_changes(offset)
    days = round(hours / 24, 1)
    shown = f"{days:g}".replace(".", ",")
    result = {"status": "learning", "hours": hours, "days": days, "changes": changes, "needed_hours": GOOD_HOURS,
              "needed_changes": GOOD_CHANGES, "beta": None, "lag": None, "skill": None, "r2": None, "facts": None,
              "message": ""}
    if hours < MIN_HOURS:
        result["message"] = (f"Lär sig: {shown} dygn med mätvärden hittills. Minst 3 dygn behövs innan något kan "
                             "visas, och runt en vecka med några kurvändringar innan förvärmningen räknar själv.")
        return result
    model = fit(indoor, outdoor, offset)
    facts = describe(model["beta"], model["lag"]) if model else None
    if not facts:
        result["message"] = ("Lär sig: mätvärdena räcker inte än för att se hur huset svarar på värmekurvan. "
                             "Det blir bättre när kurvan har ändrats några gånger.")
        return result
    result.update(beta=model["beta"], lag=model["lag"], skill=model["skill"], r2=round(model["r2"], 3), facts=facts)
    good = hours >= GOOD_HOURS and changes >= GOOD_CHANGES and model["skill"] is not None and model["skill"] >= GOOD_SKILL
    result["status"] = "good" if good else "rough"
    if good:
        result["message"] = "Modellen känner huset tillräckligt bra för att själv räkna ut när förvärmningen ska börja."
    else:
        missing = []
        if hours < GOOD_HOURS:
            missing.append(f"{shown} av 7 dygn")
        if changes < GOOD_CHANGES:
            missing.append(f"{changes} av {GOOD_CHANGES} kurvändringar")
        if model["skill"] is None or model["skill"] < GOOD_SKILL:
            missing.append("träffsäkerheten är ännu för låg")
        result["message"] = ("Grov uppskattning (" + ", ".join(missing) + "). Förvärmningen använder försiktiga "
                             f"standardvärden ({DEFAULT_LEAD} timmar innan) tills modellen är säker.")
    return result


def lead_hours(model, degrees):
    """Hours of pre-heating needed for the house to bank half the shift; default until the model is good."""
    if not model or model["status"] != "good":
        return DEFAULT_LEAD
    facts = model["facts"]
    needed = degrees / 2
    for hours in range(1, 7):
        if facts["degrees_per_step"] * (1 - math.exp(-max(0, hours - facts["lag_hours"]) / facts["time_constant_hours"])) >= needed:
            return max(1, hours)
    return 6


def preheat_windows(prices, now, lead, degrees, horizon=24 * HOUR):
    """Raise the target before expensive periods and allow the house to coast during them.

    Expensive = at least 25 % above the median of the coming day's prices. Pre-heating only happens
    when the hours before are clearly cheaper than the expensive period itself."""
    if len(prices) < 2:
        return {"windows": [], "reference": None, "note": "Inga elpriser att planera efter."}
    slot = min(b[0] - a[0] for a, b in zip(prices, prices[1:]) if b[0] > a[0]) if len(prices) > 1 else HOUR
    coming = [(t, p) for t, p in prices if t + slot > now and t < now + horizon]
    if sum(1 for t, _ in coming if t >= now) * slot < 8 * HOUR:
        return {"windows": [], "reference": None, "note": "För få kommande elpriser för att planera förvärmning."}
    ordered = sorted(p for _, p in coming)
    reference = ordered[len(ordered) // 2]
    if reference <= 0:
        return {"windows": [], "reference": reference, "note": "Elpriset är noll eller negativt; ingen förvärmning behövs."}
    blocks = []
    for t, p in coming:
        if p >= reference * 1.25:
            if blocks and t <= blocks[-1]["end"]:
                blocks[-1]["end"] = t + slot
                blocks[-1]["prices"].append(p)
            else:
                blocks.append({"start": t, "end": t + slot, "prices": [p]})
    blocks = [b for b in blocks if b["end"] - b["start"] >= HOUR]
    windows = []
    previous_end = now - horizon
    for block in blocks:
        expensive = sum(block["prices"]) / len(block["prices"])
        start = max(block["start"] - lead * HOUR, previous_end)
        before = [p for t, p in prices if start <= t < block["start"]]
        if before and block["start"] - start >= HOUR and sum(before) / len(before) <= expensive * 0.8:
            windows.append({"kind": "preheat", "start": start, "end": block["start"], "shift": degrees,
                            "price": round(sum(before) / len(before), 4)})
        windows.append({"kind": "coast", "start": block["start"], "end": block["end"], "shift": -degrees,
                        "price": round(expensive, 4)})
        previous_end = block["end"]
    windows = [w for w in windows if w["end"] > now]
    note = "" if windows else "Inga tydligt dyra timmar närmaste dygnet; ingen förvärmning behövs."
    return {"windows": windows, "reference": reference, "note": note}


def shift_at(windows, stamp):
    for window in windows or []:
        if window["start"] <= stamp < window["end"]:
            return window
    return None


def predict(model, indoor_now, now, offset_at, outdoor_at, hours=24):
    """Indoor temperature hour by hour from now, given functions for offset and outdoor over time."""
    if not model or not model.get("beta"):
        return []
    beta, lag = model["beta"], model["lag"]
    points = [[now, round(indoor_now, 2)]]
    value = indoor_now
    for k in range(hours):
        stamp = now + k * HOUR
        u = (offset_at(stamp - lag * HOUR) + offset_at(stamp - (lag + 1) * HOUR)) / 2
        value = step(beta, value, u, outdoor_at(stamp))
        if not 5 <= value <= 40:
            break
        points.append([stamp + HOUR, round(value, 2)])
    return points
