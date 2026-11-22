"""Radiator thermostats steered by a separate room sensor.

A radiator thermostat measures next to the hot radiator, so its own reading drifts and a
fireplace can overheat the room while the valve stays open. Here a room sensor elsewhere in
the room decides: when the room stays clearly above the saved target the thermostat's setpoint
is lowered so the valve throttles; when the room has cooled the previous setpoint is written
back. The pump curve is untouched.

Safety: the previous setpoint is stored before the write and restored when the room cools, the
room sensor goes silent, the function or the main automation is switched off, the pair is
removed, the pump reports alarm 229 (short compressor runs, often too little flow) or the
add-on stops. A setpoint the user changes while throttled is left alone and not restored.
"""

import math

# At most one write per thermostat this often; Matter/Zigbee valves react slowly anyway.
MIN_WRITE_SECONDS = 600
# A write that is not read back within this time is reported; a close is then forgotten.
CONFIRM_SECONDS = 900
MATCH = 0.05
MAX_PAIRS = 10


class RadiatorError(Exception):
    pass


def validate_pairs(pairs):
    if not isinstance(pairs, list) or len(pairs) > MAX_PAIRS:
        raise RadiatorError(f"Välj högst {MAX_PAIRS} radiatortermostater.")
    seen = set()
    result = []
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {"thermostat", "sensor"}:
            raise RadiatorError("Varje radiatortermostat behöver en termostat och en rumsgivare.")
        thermostat, sensor = pair["thermostat"], pair["sensor"]
        if not isinstance(thermostat, str) or not thermostat.startswith("climate."):
            raise RadiatorError("Radiatortermostaten måste vara en climate-entitet.")
        if not isinstance(sensor, str) or not sensor.startswith("sensor."):
            raise RadiatorError("Rumsgivaren för en radiatortermostat måste vara en sensor-entitet.")
        if thermostat in seen:
            raise RadiatorError("Samma radiatortermostat får inte väljas flera gånger.")
        seen.add(thermostat)
        result.append({"thermostat": thermostat, "sensor": sensor})
    return result


def validate_state(saved):
    if not isinstance(saved, dict):
        raise RadiatorError("Ogiltigt sparat radiatortillstånd.")
    for key, state in saved.items():
        if not isinstance(key, str) or not isinstance(state, dict) or state.get("phase") not in ("open", "closed"):
            raise RadiatorError("Ogiltigt sparat radiatortillstånd.")
        for field in ("restore", "closed_value", "last_write", "warm_since"):
            value = state.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise RadiatorError("Ogiltigt sparat radiatortillstånd.")
        if state["phase"] == "closed" and state.get("restore") is None:
            raise RadiatorError("Ogiltigt sparat radiatortillstånd: tidigare börvärde saknas.")
    return saved


def fmt(value):
    return f"{value:.1f}".replace(".", ",")
