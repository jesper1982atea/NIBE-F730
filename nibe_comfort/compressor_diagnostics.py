"""Passive, bounded telemetry history for diagnosing short compressor runs."""

import json
import re
import sqlite3
import statistics
import time
from bisect import bisect_left
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

RETENTION_SECONDS = 7 * 86400
MAX_ENTITIES = 160
NUMERIC = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")
ON_STATES = {"1", "on", "true", "active", "operating", "running", "started", "i drift"}
OFF_STATES = {"0", "off", "false", "inactive", "stopped", "stop", "blocked", "idle",
              "standby", "vilar", "viloläge", "stoppad", "av"}


def numeric_state(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not NUMERIC.fullmatch(text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def compressor_state(value):
    if value is None:
        return None
    text = str(value).strip().casefold()
    if text in ON_STATES:
        return True
    if text in OFF_STATES:
        return False
    return None


def _safe_text(value, limit=160):
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return text[:limit]
    return None


def _entity_base(entity_id):
    return re.sub(r"_\d+$", "", entity_id)


def _reported_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _app_context(status, config):
    fields = ("checked_at", "enabled", "indoor", "outdoor", "house_indoor", "current_offset",
              "desired_offset", "next_offset", "target_shift", "price", "price_level", "price_rank",
              "contributions", "preheat", "hot_water", "aux_limit", "pump_alarm", "message", "error_message")
    result = {key: status.get(key) for key in fields if key in status}
    result["target_temperature"] = config.get("target_temperature")
    result["price_mode"] = config.get("price_mode")
    for key in ("min_interval_minutes", "max_step", "cheap_boost", "expensive_reduction",
                "preheat_enabled", "preheat_degrees"):
        result[key] = config.get(key)
    return result


def _entity_snapshot(entity, timestamp):
    attrs = entity.get("attributes") or {}
    reported = entity.get("last_reported")
    reported_timestamp = _reported_timestamp(reported)
    return {
        "entity_id": entity["entity_id"],
        "name": _safe_text(attrs.get("friendly_name") or entity["entity_id"], 100),
        "value": _safe_text(entity.get("state")),
        "unit": _safe_text(attrs.get("unit_of_measurement"), 24),
        "device_class": _safe_text(attrs.get("device_class"), 32),
        "reported_at": reported if isinstance(reported, str) else None,
        "age_seconds": max(0, round(timestamp - reported_timestamp)) if reported_timestamp is not None else None,
    }


class CompressorDiagnostics:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS samples "
                "(timestamp REAL PRIMARY KEY, data TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS samples_timestamp ON samples(timestamp)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS auto_resets "
                "(system_id TEXT NOT NULL, device_id TEXT NOT NULL, alarm_id TEXT NOT NULL, "
                "first_seen REAL NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL, "
                "message TEXT NOT NULL, analysis TEXT, PRIMARY KEY(system_id,device_id,alarm_id))"
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, states, peer_ids, offset_entity, config, status, timestamp=None):
        stamp = time.time() if timestamp is None else timestamp
        ids = {offset_entity, *(peer_ids or ())} if offset_entity else set()
        state_map = {item["entity_id"]: item for item in states or []
                     if isinstance(item, dict) and isinstance(item.get("entity_id"), str)}
        selected = []
        for entity_id in ids:
            entity = state_map.get(entity_id)
            if not entity or entity_id.split(".", 1)[0] not in {
                    "sensor", "binary_sensor", "number", "select", "switch", "climate"}:
                continue
            snapshot = _entity_snapshot(entity, stamp)
            snapshot["reported_at"] = _safe_text(entity.get("last_reported"), 40)
            selected.append(snapshot)
        selected.sort(key=lambda item: (
            0 if _entity_base(item["entity_id"]).endswith("_status_compressor") else
            1 if _entity_base(item["entity_id"]).endswith(("_alarm", "_alarms")) else
            2 if item.get("device_class") == "temperature" else 3,
            item["entity_id"],
        ))
        truncated = len(selected) > MAX_ENTITIES
        selected = selected[:MAX_ENTITIES]
        data = {
            "available": states is not None,
            "pump_configured": bool(offset_entity and peer_ids),
            "truncated": truncated,
            "pump": selected,
            "app": _app_context(status or {}, config or {}),
        }
        with self.connect() as connection:
            previous = connection.execute(
                "SELECT timestamp FROM samples ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if previous and stamp - previous[0] < 30:
                return False
            connection.execute(
                "INSERT OR REPLACE INTO samples(timestamp,data) VALUES (?,?)",
                (stamp, json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))),
            )
            connection.execute("DELETE FROM samples WHERE timestamp < ?", (stamp - RETENTION_SECONDS,))
        return True

    def report(self, hours=72, now=None):
        if type(hours) is not int or hours not in (6, 24, 72):
            raise ValueError("Välj 6, 24 eller 72 timmar.")
        end = time.time() if now is None else now
        start = end - hours * 3600
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT timestamp,data FROM samples WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (start, end),
            ).fetchall()
        samples = [{"timestamp": stamp, **json.loads(data)} for stamp, data in rows]
        if not samples:
            return {
                "available": False, "hours": hours, "start": None, "end": None, "sample_count": 0,
                "sample_interval_seconds": None, "max_gap_seconds": None, "gaps_over_120_seconds": 0,
                "samples_without_pump_values": 0,
                "entities": [], "temperatures": [], "series": [], "cycles": [], "alarms": [],
                "recommendations": [], "auto_resets": self.auto_reset_history(),
                "message": "Pumpdiagnostiken börjar samla värden när tillägget har en aktuell avläsning.",
            }
        if not any(sample["pump"] for sample in samples):
            configured = any(sample.get("pump_configured") for sample in samples)
            return {
                "available": False, "hours": hours, "start": samples[0]["timestamp"], "end": samples[-1]["timestamp"],
                "sample_count": len(samples), "sample_interval_seconds": None, "max_gap_seconds": None,
                "gaps_over_120_seconds": 0, "samples_without_pump_values": len(samples),
                "entities": [], "temperatures": [], "series": [],
                "cycles": [], "alarms": [], "recommendations": [], "auto_resets": self.auto_reset_history(),
                "message": ("Vald pumps entiteter kunde inte läsas i perioden. Kontrollera Home Assistant-anslutningen."
                            if configured else "Välj en myUplink-entitet för kurvförskjutning så tillägget hittar pumpens övriga entiteter."),
            }

        intervals = [b["timestamp"] - a["timestamp"] for a, b in zip(samples, samples[1:])]
        interval = round(statistics.median(intervals)) if intervals else None
        latest_values = {}
        for sample in samples:
            for item in sample["pump"]:
                latest_values[item["entity_id"]] = {**item, "sampled_at": sample["timestamp"]}
        entity_ids = sorted(latest_values)
        points_by_id = {entity_id: [] for entity_id in entity_ids}
        for sample in samples:
            at_time = {item["entity_id"]: item for item in sample["pump"]}
            for entity_id in entity_ids:
                item = at_time.get(entity_id)
                points_by_id[entity_id].append([
                    sample["timestamp"], numeric_state(item.get("value")) if item else None,
                ])
        entities = [latest_values[entity_id] for entity_id in sorted(latest_values)]
        series = [{**latest_values[entity_id], "points": points_by_id[entity_id]}
                  for entity_id in entity_ids if any(point[1] is not None for point in points_by_id[entity_id])]
        temperatures = [line for line in series if line.get("device_class") == "temperature"
                        or (line.get("unit") or "").casefold() in ("°c", "°f", "c", "f")]
        cycles = self._cycles(samples)
        alarms = self._alarms(samples)
        return {
            "available": True, "hours": hours, "start": samples[0]["timestamp"], "end": samples[-1]["timestamp"],
            "sample_count": len(samples), "sample_interval_seconds": interval,
            "max_gap_seconds": round(max(intervals)) if intervals else None,
            "gaps_over_120_seconds": sum(gap > 120 for gap in intervals),
            "samples_without_pump_values": sum(not sample["pump"] for sample in samples),
            "entities": entities, "temperatures": temperatures, "series": series,
            "cycles": cycles[-100:], "alarms": alarms[-30:],
            "recommendations": self._recommendations(samples, cycles),
            "auto_resets": self.auto_reset_history(),
            "truncated": any(sample.get("truncated") for sample in samples),
            "message": ("Proverna kommer från vald myUplink-pump via Home Assistant. "
                        "Tidsstämplar visar när tillägget läste värdena; själva pump-/integrationens rapport kan vara äldre."),
        }

    def hot_water_monitor(self, hours=72, now=None):
        if type(hours) is not int or hours not in (24, 72, 168):
            raise ValueError("Välj 24, 72 eller 168 timmar.")
        now = time.time() if now is None else now
        samples = []
        suffixes = ("_active_compressors_hot_water", "_supply_line_bt2", "_return_line_bt3",
                    "_hot_water_charging_bt6", "_hot_water_top_bt7", "_hot_water_amount",
                    "_hot_water_comfort_mode")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT timestamp,data FROM samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
                (now - hours * 3600, now),
            )
            for stamp, data in rows:
                sample = json.loads(data)
                pump = [item for item in sample.get("pump", [])
                        if _entity_base(item["entity_id"]).endswith(suffixes)]
                app = sample.get("app") or {}
                samples.append({"timestamp": stamp, "pump": pump,
                                "app": {key: app.get(key) for key in ("checked_at", "pump_alarm", "current_offset")}})
        timestamps = [sample["timestamp"] for sample in samples]
        ids = sorted({item["entity_id"] for sample in samples for item in sample.get("pump", [])
                      if _entity_base(item["entity_id"]).endswith("_active_compressors_hot_water")})
        result = {
            "hours": hours, "sample_count": len(samples),
            "last_sample": samples[-1]["timestamp"] if samples else None,
            "current": "unknown", "current_reported_at": None,
            "charges": [], "offset_changes": [], "truncated": False,
            "message": "Väntar på pumpens varmvattenstatus och diagnostikprover.",
        }
        if len(ids) != 1:
            if len(ids) > 1:
                result["message"] = "Flera varmvattenstatusgivare hittades; välj en entydig pump."
            return result

        def mode(sample):
            value = next((numeric_state(item.get("value")) for item in sample.get("pump", [])
                          if item["entity_id"] == ids[0]), None)
            return True if value is not None and value > 0 else False if value == 0 else None

        def values(sample):
            found = {}
            for item in sample.get("pump", []):
                base = _entity_base(item["entity_id"])
                for suffix, key in (("_supply_line_bt2", "bt2"), ("_return_line_bt3", "bt3"),
                                    ("_hot_water_charging_bt6", "bt6"), ("_hot_water_top_bt7", "bt7")):
                    if base.endswith(suffix):
                        found.setdefault(key, []).append(numeric_state(item.get("value")))
            result = {key: items[0] for key, items in found.items() if len(items) == 1}
            modes = [item.get("value") for item in sample.get("pump", [])
                     if item["entity_id"].startswith("select.")
                     and _entity_base(item["entity_id"]).endswith(("_hot_water_amount", "_hot_water_comfort_mode"))]
            result["mode"] = modes[0] if len(modes) == 1 and modes[0] not in ("unknown", "unavailable") else None
            return result

        def alarm(sample):
            app = sample.get("app") or {}
            status = app.get("pump_alarm")
            checked = app.get("checked_at")
            if (not isinstance(status, dict) or status.get("error")
                    or type(status.get("active")) is not bool
                    or not isinstance(checked, (int, float)) or isinstance(checked, bool)
                    or abs(sample["timestamp"] - checked) > 120):
                return None
            return status["active"]

        def alarm_window(start, end, can_complete=True):
            window = []
            for index in range(bisect_left(timestamps, start), len(samples)):
                sample = samples[index]
                if sample["timestamp"] > end + 120:
                    break
                window.append(sample)
                if sample["timestamp"] >= end:
                    break
            if any(alarm(sample) is True for sample in window):
                return "observed"
            continuous = bool(window) and all(
                b["timestamp"] - a["timestamp"] <= 120 for a, b in zip(window, window[1:]))
            if (can_complete and window and window[-1]["timestamp"] >= end and continuous
                    and all(alarm(sample) is False for sample in window)):
                return "none_observed"
            return "unknown"

        charges = []
        active = None
        previous = None
        offsets = []
        previous_offset = None
        for sample in samples:
            stamp = sample["timestamp"]
            state = mode(sample)
            contiguous = previous is not None and stamp - previous["timestamp"] <= 120
            if active is not None and (not contiguous or state is None):
                active.update(end=None, interrupted=True)
                charges.append(active)
                active = None
            if state is True:
                if active is None:
                    active = {"start": stamp, "last_on": stamp, "start_values": values(sample),
                              "start_known": contiguous and mode(previous) is False}
                active["last_on"] = stamp
            elif state is False and active is not None:
                active.update(end=stamp, end_values=values(sample), interrupted=False)
                charges.append(active)
                active = None
            offset = numeric_state((sample.get("app") or {}).get("current_offset"))
            if contiguous and previous_offset is not None and offset is not None and offset != previous_offset:
                offsets.append({"timestamp": stamp, "previous": previous_offset, "value": offset})
            previous_offset = offset
            previous = sample
        if active is not None:
            active.update(end=None, interrupted=False)
            charges.append(active)

        for charge in charges:
            start = charge["start"]
            end = charge["end"]
            charge["observed_seconds"] = max(0, round(charge["last_on"] - start))
            window_end = end + 600 if end is not None else charge["last_on"]
            charge["alarm_result"] = alarm_window(
                start, window_end, end is not None and not charge["interrupted"] and charge["start_known"])
        for change in offsets:
            change["alarm_result"] = alarm_window(change["timestamp"], change["timestamp"] + 600)
        latest = samples[-1] if samples else None
        if latest and now - latest["timestamp"] <= 120:
            current = mode(latest)
            result["current"] = "charging" if current is True else "idle" if current is False else "unknown"
            item = next((item for item in latest["pump"] if item["entity_id"] == ids[0]), None)
            result["current_reported_at"] = item.get("reported_at") if item else None
        result.update(
            charges=list(reversed(charges[-100:])), offset_changes=list(reversed(offsets[-100:])),
            truncated=len(charges) > 100 or len(offsets) > 100,
            message=("Varmvattenladdning avläses från pumpens statusgivare. "
                     "Prov och rapporterade värden är inte exakta mätningar."),
        )
        return result

    def analyze_alarm_window(self, alarm_seen_at, now=None, config=None):
        """Check that the requested 30-minute-before/10-minute-after sample window is complete."""
        now = time.time() if now is None else now
        start, end = alarm_seen_at - 1800, alarm_seen_at + 600
        if now < end:
            return {"ready": False, "reason": "Väntar på tio minuters prov efter larmet.", "recommendations": []}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT timestamp,data FROM samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
                (start - 120, end + 120),
            ).fetchall()
        samples = [{"timestamp": stamp, **json.loads(data)} for stamp, data in rows]
        if len(samples) < 37 or samples[0]["timestamp"] > start or samples[-1]["timestamp"] < end:
            return {"ready": False, "reason": "Saknas sammanhängande prover för hela analysfönstret.", "recommendations": []}
        intervals = [b["timestamp"] - a["timestamp"] for a, b in zip(samples, samples[1:])]
        if any(gap > 120 for gap in intervals):
            return {"ready": False, "reason": "Datagap över två minuter gör analysfönstret ofullständigt.",
                    "recommendations": []}
        if any(not sample.get("available") or not sample.get("pump") for sample in samples):
            return {"ready": False, "reason": "Pumpvärden saknas i delar av analysfönstret.", "recommendations": []}

        compressor_ids = sorted({entity_id for sample in samples for entity_id in self._compressor_ids(sample)})
        if len(compressor_ids) != 1:
            return {"ready": False, "reason": "Kompressorns statusgivare saknas eller är tvetydig.",
                    "recommendations": []}
        temperature_ids = sorted({
            item["entity_id"] for sample in samples for item in sample["pump"]
            if _entity_base(item["entity_id"]).endswith(("_supply_line_bt2", "_return_line_bt3"))
        })
        if not temperature_ids:
            temperature_ids = sorted({
                item["entity_id"] for sample in samples for item in sample["pump"]
                if item.get("device_class") == "temperature"
            })
        if not temperature_ids:
            return {"ready": False, "reason": "Fram-/returledningens temperaturgivare saknas.",
                    "recommendations": []}
        for sample in samples:
            pump = {item["entity_id"]: item for item in sample["pump"]}
            compressor = pump.get(compressor_ids[0])
            if compressor_state(compressor.get("value") if compressor else None) is None:
                return {"ready": False, "reason": "Kompressorstatus saknas i delar av analysfönstret.",
                        "recommendations": []}
            if not any(numeric_state(pump.get(entity_id, {}).get("value")) is not None
                       for entity_id in temperature_ids):
                return {"ready": False, "reason": "Fram-/returtemperatur saknas i delar av analysfönstret.",
                        "recommendations": []}
            if sample["timestamp"] < alarm_seen_at:
                for item in sample["pump"]:
                    if (item["entity_id"].startswith("binary_sensor.")
                            and _entity_base(item["entity_id"]).endswith("_alarm")
                            and str(item.get("value", "")).casefold() == "on"):
                        return {"ready": False, "reason": "Larmstatus var redan aktiv före första myUplink-observationen.",
                                "recommendations": []}

        cycles = self._cycles(samples)
        recommendations = self._recommendations(samples, cycles, config)
        return {
            "ready": True, "reason": "30 minuter före och 10 minuter efter larmet har analyserats.",
            "start": samples[0]["timestamp"], "end": samples[-1]["timestamp"],
            "sample_count": len(samples), "cycles": cycles, "recommendations": recommendations,
        }

    @staticmethod
    def _recommendations(samples, cycles, config=None):
        short_runs = [cycle for cycle in cycles if cycle["classification"] == "Kort körning enligt prover"]
        if not short_runs:
            return ["Ingen kort kompressorkörning har kunnat bekräftas i den här perioden. "
                    "Om ett aktivt 229-larm ändå rapporteras, kontrollera pumpens display och installatörens råd."]
        start = short_runs[-1]["start"]
        window = [sample for sample in samples if start - 3600 <= sample["timestamp"] <= start]
        offset_changes = []
        prior = None
        for sample in window:
            value = numeric_state((sample.get("app") or {}).get("current_offset"))
            if value is not None and prior is not None and abs(value - prior[1]) > 1e-6:
                offset_changes.append((sample["timestamp"], prior[1], value))
            if value is not None:
                prior = (sample["timestamp"], value)
        target_changes = []
        prior_target = None
        for sample in window:
            app = sample.get("app") or {}
            contributions = app.get("contributions") if isinstance(app.get("contributions"), dict) else {}
            target = (app.get("price_level"), numeric_state(contributions.get("price")),
                      numeric_state(app.get("target_shift")))
            if prior_target is not None and target != prior_target[1]:
                target_changes.append(sample["timestamp"])
            prior_target = (sample["timestamp"], target)
        if offset_changes:
            interval = (config or {}).get("min_interval_minutes", 30)
            return [
                f"Kurvförskjutningen ändrades {len(offset_changes)} gång(er) under timmen före den korta körningen. "
                "Det visar tidsmässigt samband, inte orsak.",
                f"Försiktigt kodtest: pausa pris-/förvärmningsbidraget och prova sedan minsta ändringsintervall "
                f"på minst 60 minuter (nu konfigurerat: {interval} min). Ändra en sak i taget och jämför loggarna.",
                "Kontrollera samtidigt flöde, öppna ventiler, luft och filter med installatör; mjukvaran kan inte "
                "mäta vattenflödet eller bekräfta att grundorsaken är åtgärdad.",
            ]
        if target_changes:
            return [
                "Prisnivå eller förvärmningsbidrag ändrades under timmen före den korta körningen, men ingen "
                "kurvförskjutning ändrades. Samvariation bevisar inte orsak.",
                "Försiktigt kodtest: kör tillfälligt utan prisstyrd kurvjustering; om sambandet återkommer kan "
                "prisbidraget rampas långsammare och hållas stabilt längre mellan prisfönster.",
                "Kontrollera även värmeflöde, ventiler, luft och filter; pumpens skydd kan inte säkert diagnostiseras "
                "enbart från appdata.",
            ]
        return [
            "Ingen ändring av kurvförskjutning, prisnivå eller förvärmningsbidrag hittades under timmen före "
            "den korta körningen; loggen ger inget stöd för att NIBE Comforts styrskrivning utlöste den.",
            "Prioritera kontroll av systemflöde, öppna ventiler, luft, filter och returtemperatur med installatör. "
            "Ändra inte styralgoritmen utan belägg från en ny logg.",
        ]

    def begin_auto_reset(self, system_id, device_id, alarm_id, first_seen):
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO auto_resets "
                "(system_id,device_id,alarm_id,first_seen,state,updated_at,message,analysis) "
                "VALUES (?,?,?,?,'pending',?,'Väntar på 10 minuters efterloggning.',NULL)",
                (system_id, device_id, alarm_id, first_seen, first_seen),
            )
            return connection.execute(
                "SELECT first_seen,state FROM auto_resets WHERE system_id=? AND device_id=? AND alarm_id=?",
                (system_id, device_id, alarm_id),
            ).fetchone()

    def auto_reset_claim(self, system_id, device_id, alarm_id, now, analysis):
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE auto_resets SET state='attempting',updated_at=?,message=?,analysis=? "
                "WHERE system_id=? AND device_id=? AND alarm_id=? AND state='pending'",
                (now, "En engångsbegäran om automatisk kvittering pågår.",
                 json.dumps(analysis, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                 system_id, device_id, alarm_id),
            )
            return cursor.rowcount == 1

    def auto_reset_update_pending(self, system_id, device_id, alarm_id, now, message):
        with self.connect() as connection:
            connection.execute(
                "UPDATE auto_resets SET updated_at=?,message=? "
                "WHERE system_id=? AND device_id=? AND alarm_id=? AND state='pending'",
                (now, message, system_id, device_id, alarm_id),
            )

    def auto_reset_finish(self, system_id, device_id, alarm_id, now, state, message, analysis=None):
        encoded = json.dumps(analysis, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if analysis else None
        with self.connect() as connection:
            connection.execute(
                "UPDATE auto_resets SET state=?,updated_at=?,message=?,analysis=COALESCE(?,analysis) "
                "WHERE system_id=? AND device_id=? AND alarm_id=?",
                (state, now, message, encoded, system_id, device_id, alarm_id),
            )

    def auto_reset_block(self, system_id, device_id, alarm_id, now, message):
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO auto_resets "
                "(system_id,device_id,alarm_id,first_seen,state,updated_at,message,analysis) "
                "VALUES (?,?,?,?,'blocked',?,?,NULL)",
                (system_id, device_id, alarm_id, now, now, message),
            )
            connection.execute(
                "UPDATE auto_resets SET state='blocked',updated_at=?,message=? "
                "WHERE system_id=? AND device_id=? AND alarm_id=? AND state='pending'",
                (now, message, system_id, device_id, alarm_id),
            )

    def auto_reset_cancel_pending(self, system_id, device_id, now):
        with self.connect() as connection:
            connection.execute(
                "UPDATE auto_resets SET state='cleared',updated_at=?,message='Larmet försvann innan analysen var klar.' "
                "WHERE system_id=? AND device_id=? AND state='pending'",
                (now, system_id, device_id),
            )

    def auto_reset_block_pending(self, system_id, device_id, now, message):
        with self.connect() as connection:
            connection.execute(
                "UPDATE auto_resets SET state='blocked',updated_at=?,message=? "
                "WHERE system_id=? AND device_id=? AND state='pending'",
                (now, message, system_id, device_id),
            )

    def auto_reset_history(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT system_id,device_id,alarm_id,first_seen,state,updated_at,message,analysis "
                "FROM auto_resets ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        return [{
            "system_id": row[0], "device_id": row[1], "alarm_id": row[2],
            "first_seen": row[3], "state": row[4], "updated_at": row[5],
            "message": row[6], "analysis": json.loads(row[7]) if row[7] else None,
        } for row in rows]

    @staticmethod
    def _compressor_ids(sample):
        return [item["entity_id"] for item in sample.get("pump", [])
                if _entity_base(item["entity_id"]).endswith("_status_compressor")]

    @classmethod
    def _cycles(cls, samples):
        ids = sorted({entity_id for sample in samples for entity_id in cls._compressor_ids(sample)})
        events = []
        for entity_id in ids:
            active = None
            previous_off = None
            last_on = None
            previous_sample = None
            for sample in samples:
                if previous_sample is not None and sample["timestamp"] - previous_sample > 120:
                    active = None
                    previous_off = None
                    last_on = None
                previous_sample = sample["timestamp"]
                item = next((v for v in sample.get("pump", []) if v["entity_id"] == entity_id), None)
                state = compressor_state(item.get("value") if item else None)
                if item and item.get("age_seconds") is not None and item["age_seconds"] > 120:
                    state = None
                if state is False:
                    if active is not None and last_on is not None:
                        minimum = max(0, last_on["timestamp"] - active["timestamp"])
                        maximum = (max(minimum, sample["timestamp"] - previous_off)
                                   if previous_off is not None else None)
                        classification = ("Kort körning enligt prover" if maximum is not None and maximum < 300 else
                                          "Minst fem minuter enligt prover" if minimum >= 300 else
                                          "Osäkert intervall runt fem minuter")
                        events.append({
                            "entity_id": entity_id, "start": active["timestamp"], "end": sample["timestamp"],
                            "observed_seconds": round(sample["timestamp"] - active["timestamp"]),
                            "minimum_seconds": round(minimum),
                            "maximum_seconds": round(maximum) if maximum is not None else None,
                            "classification": classification, "start_sample": active,
                            "end_sample": sample,
                        })
                    active = None
                    last_on = None
                    previous_off = sample["timestamp"]
                elif state is True:
                    if active is None:
                        active = sample
                    last_on = sample
                else:
                    active = None
                    previous_off = None
                    last_on = None
        return events

    @staticmethod
    def _alarms(samples):
        events = []
        previously_active = False
        for sample in samples:
            alarm_state = None
            alarm_code = None
            for item in sample.get("pump", []):
                base = _entity_base(item["entity_id"])
                if base.endswith("_alarm") and item["entity_id"].startswith("binary_sensor."):
                    alarm_state = str(item.get("value", "")).casefold() == "on"
                elif base.endswith("_alarms"):
                    alarm_code = numeric_state(item.get("value"))
            app_alarm = sample.get("app", {}).get("pump_alarm") or {}
            if alarm_state is None and isinstance(app_alarm, dict) and app_alarm.get("active") is True:
                alarm_state = True
                alarm_code = app_alarm.get("code", alarm_code)
            if alarm_state is True and not previously_active:
                events.append({
                    "timestamp": sample["timestamp"],
                    "code": int(alarm_code) if alarm_code is not None and alarm_code.is_integer() else None,
                    "sample": sample,
                })
            previously_active = alarm_state is True
        return events
