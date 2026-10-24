import copy
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError, Controller, DEFAULTS, HomeAssistant, current_readings, device_peers, discover_nibe, evaluate, validate


def entity(entity_id, value, now=None, **attributes):
    return {
        "entity_id": entity_id,
        "state": str(value),
        "last_updated": datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat(),
        "attributes": attributes,
    }


def fixtures(now=None):
    return [
        entity("sensor.living", 20, now, unit_of_measurement="°C"),
        entity("sensor.bedroom", 22, now, unit_of_measurement="°C"),
        entity("sensor.outdoor", -10, now, unit_of_measurement="°C"),
        entity("sensor.price", 3, now, unit_of_measurement="SEK/kWh"),
        entity("number.offset", 0, now, min=-10, max=10, step=1),
    ]


def config(**overrides):
    return validate({**copy.deepcopy(DEFAULTS), "room_sensors": ["sensor.living", "sensor.bedroom"], "offset_entity": "number.offset", **overrides})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.offset = "number.pump_heating_offset_climate_system_1"
        self.outdoor = "sensor.pump_current_outd_temp_bt1"
        self.entities = [
            entity(self.offset, 3, min=-10, max=10, step=1),
            entity(self.outdoor, 10, unit_of_measurement="°C"),
        ]
        self.registry = [
            {"entity_id": self.offset, "device_id": "pump"},
            {"entity_id": self.outdoor, "device_id": "pump"},
        ]

    def test_identifies_pump_and_its_bt1(self):
        result = discover_nibe(self.entities, self.registry)
        self.assertEqual(result["offset_entity"], self.offset)
        self.assertEqual(result["outdoor_entity"], self.outdoor)

    def test_entity_name_alone_does_not_prove_myuplink_membership(self):
        self.assertFalse(discover_nibe(self.entities, [])["offset_entity"])

    def test_outdoor_from_another_device_is_not_selected(self):
        self.registry[1]["device_id"] = "other-pump"
        result = discover_nibe(self.entities, self.registry)
        self.assertEqual(result["offset_entity"], self.offset)
        self.assertFalse(result["outdoor_entity"])

    def test_multiple_pumps_require_manual_selection_even_if_one_unavailable(self):
        other = "number.second_heating_offset_climate_system_1"
        self.entities.append(entity(other, "unavailable", min=-10, max=10, step=1))
        self.registry.append({"entity_id": other, "device_id": "second"})
        result = discover_nibe(self.entities, self.registry)
        self.assertFalse(result["offset_entity"])
        self.assertFalse(result["outdoor_entity"])
        self.assertIn("Flera", result["message"])

    def test_unavailable_pump_is_identified_not_replaced(self):
        self.entities[0]["state"] = "unavailable"
        self.assertEqual(discover_nibe(self.entities, self.registry)["offset_entity"], self.offset)

    def test_wrong_domain_parameter_and_missing_device_are_not_selected(self):
        for entity_id in (
            "sensor.pump_heating_offset_climate_system_1",
            "number.pump_heating_offset_smart_price_adaption",
            "number.pump_heating_curve_climate_system_1",
            "number.pump_heating_offset_climate_system_2",
        ):
            with self.subTest(entity_id=entity_id):
                entities = [entity(entity_id, 3)]
                registry = [{"entity_id": entity_id, "device_id": "pump"}]
                self.assertFalse(discover_nibe(entities, registry)["offset_entity"])
        self.registry[0]["device_id"] = None
        self.assertFalse(discover_nibe(self.entities, self.registry)["offset_entity"])

    def test_missing_state_is_not_selected(self):
        self.assertFalse(discover_nibe([], self.registry)["offset_entity"])

    def test_multiple_bt1_candidates_are_not_guessed(self):
        other = "sensor.duplicate_current_outd_temp_bt1"
        self.entities.append(entity(other, 10))
        self.registry.append({"entity_id": other, "device_id": "pump"})
        self.assertFalse(discover_nibe(self.entities, self.registry)["outdoor_entity"])

    def test_invalid_registry_response_is_explicit_error(self):
        for invalid in ({}, [None], [{"entity_id": self.offset}]):
            with self.subTest(invalid=invalid), self.assertRaises(ControlError):
                discover_nibe(self.entities, invalid)

    def test_client_uses_integration_and_device_identity(self):
        ha = HomeAssistant("token")
        ha.request = Mock(side_effect=[self.registry, self.entities])
        result = ha.discover()
        self.assertEqual(result["outdoor_entity"], self.outdoor)
        self.assertEqual(ha.request.call_args_list[0].args[0], "/template")
        template = ha.request.call_args_list[0].args[1]["template"]
        self.assertIn("integration_entities(platform)", template)
        self.assertIn("'myuplink'", template)
        self.assertIn("'tibber'", template)
        self.assertIn("device_id(eid)", template)
        self.assertIn("states.climate", template)
        self.assertIn("device_entities(did)", template)
        self.assertIn("did not in ns.items", template)


class ValidationTests(unittest.TestCase):
    def test_defaults_are_monitor_only(self):
        self.assertFalse(validate(DEFAULTS)["enabled"])

    def test_reject_invalid_config(self):
        cases = [
            {"enabled": 1}, {"target_temperature": float("nan")},
            {"target_temperature": True}, {"target_temperature": 31},
            {"room_sensors": ["sensor.x", "sensor.x"]},
            {"room_sensors": ["number.x"]}, {"offset_entity": "climate.pump"},
            {"price_low": 2, "price_high": 1},
            {"base_offset": 5}, {"weather_gain": 0.1},
            {"cheap_boost": 1}, {"max_step": 0},
            {"min_interval_minutes": 1},
            {"enabled": True, "room_sensors": []},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ControlError):
                config(**values)

    def test_unknown_and_missing_fields_rejected(self):
        for invalid in ({**DEFAULTS, "unknown": True}, {}, []):
            with self.subTest(invalid=invalid), self.assertRaises(ControlError):
                validate(invalid)


class EvaluationTests(unittest.TestCase):
    def test_comfort_reads_setpoint_and_mode_without_affecting_control(self):
        now = time.time()
        cfg = config(room_sensors=["climate.living"])
        states = [entity("climate.living", "heat", now, current_temperature=21, temperature=20,
                         temperature_unit="°C")]
        room = current_readings(cfg, states, now, {})["rooms"][0]
        self.assertEqual((room["temperature"], room["setpoint"], room["thermostat_mode"]), (21, 20, "heat"))
        states[0]["attributes"].update(current_temperature=69.8, temperature=68, temperature_unit="°F")
        room = current_readings(cfg, states, now, {})["rooms"][0]
        self.assertEqual((room["temperature"], room["setpoint"]), (21, 20))
        states[0]["attributes"]["temperature"] = "invalid"
        room = current_readings(cfg, states, now, {})["rooms"][0]
        self.assertEqual(room["temperature"], 21)
        self.assertIsNone(room["setpoint"])
        self.assertIsNotNone(room["thermostat_error"])
        self.assertIsNone(room["error"])
        states[0]["attributes"].pop("temperature")
        room = current_readings(cfg, states, now, {})["rooms"][0]
        self.assertIsNone(room["setpoint"])
        self.assertIsNone(room["thermostat_error"])
        states[0]["last_updated"] = entity("climate.living", "heat", now - 86400)["last_updated"]
        room = current_readings(cfg, states, now, {})["rooms"][0]
        self.assertIsNone(room["temperature"])
        self.assertIsNotNone(room["thermostat_error"])

    def test_comfort_only_links_unique_same_device_thermostat(self):
        now = time.time()
        cfg = config(room_sensors=["sensor.living"])
        states = [entity("sensor.living", 21, now, unit_of_measurement="°C"),
                  entity("climate.living", "off", now, current_temperature=21, temperature=20, temperature_unit="°C")]
        room = current_readings(cfg, states, now, {})["rooms"][0]
        self.assertIsNone(room["thermostat"])
        peers = {"sensor.living": ["climate.living"]}
        room = current_readings(cfg, states, now, peers)["rooms"][0]
        self.assertEqual((room["setpoint"], room["thermostat_mode"]), (20, "off"))
        peers["sensor.living"].append("climate.other")
        room = current_readings(cfg, states, now, peers)["rooms"][0]
        self.assertIsNone(room["setpoint"])
        self.assertIn("Flera", room["thermostat_error"])
        peers["sensor.living"].pop()
        states[1]["state"] = "unavailable"
        room = current_readings(cfg, states, now, peers)["rooms"][0]
        self.assertIsNone(room["setpoint"])
        self.assertIsNotNone(room["thermostat_error"])
        self.assertEqual(room["temperature"], 21)

    def test_reports_requested_offset_before_safety_limit(self):
        cfg = config(base_offset=3, target_temperature=22, priority_sensors=["sensor.living"])
        states = fixtures()
        states[0] = entity("sensor.living", 21.1, unit_of_measurement="°C")
        states[-1] = entity("number.offset", 3, min=-10, max=10, step=1)
        result = evaluate(cfg, states, time.time())
        self.assertEqual(result["control_limit"], {"direction": "upper", "value": 3, "requested": 4.35})
        self.assertEqual(result["next_offset"], 3)
        cfg["max_offset"] = 4
        self.assertEqual(evaluate(cfg, states, time.time())["next_offset"], 4)
        cfg.update(base_offset=-3, target_temperature=20)
        states[-1]["state"] = "-3"
        result = evaluate(cfg, states, time.time())
        self.assertEqual(result["control_limit"]["direction"], "lower")
        self.assertEqual(result["next_offset"], -3)

    def test_priority_average_controls_instead_of_house_average(self):
        cfg = config(priority_sensors=["sensor.living"], target_temperature=22)
        result = evaluate(cfg, fixtures(), time.time())
        self.assertEqual(result["indoor"], 20)
        self.assertEqual(result["contributions"]["room"], 3)
        self.assertEqual(result["next_offset"], 1)
        cfg["priority_sensors"] = ["sensor.living", "sensor.bedroom"]
        self.assertEqual(evaluate(cfg, fixtures(), time.time())["indoor"], 21)
        cfg["priority_sensors"] = []
        self.assertEqual(evaluate(cfg, fixtures(), time.time())["indoor"], 21)

    def test_priority_validation(self):
        for priority in ("sensor.living", ["sensor.other"], ["sensor.living"] * 2,
                         ["sensor.living", "sensor.bedroom", "sensor.other"], [None]):
            with self.subTest(priority=priority), self.assertRaises(ControlError):
                config(priority_sensors=priority)

    def setUp(self):
        self.now = time.time()
        self.entities = fixtures(self.now)

    def evaluate(self, **overrides):
        return evaluate(config(**overrides), self.entities, self.now)

    def test_equal_weight_average_and_deadband(self):
        result = self.evaluate()
        self.assertEqual(result["indoor"], 21)
        self.assertEqual(result["desired_offset"], 0)
        self.assertEqual(result["next_offset"], 0)
        self.assertEqual(result["contributions"]["room"], 0)

    def test_climate_uses_measured_temperature_not_setpoint_or_mode(self):
        self.entities.append(entity(
            "climate.lk_allrum_thermostat", "heat", self.now,
            current_temperature=22.2, temperature=30, temperature_unit="°C",
        ))
        result = self.evaluate(room_sensors=["climate.lk_allrum_thermostat"])
        self.assertEqual(result["indoor"], 22.2)
        self.assertEqual(result["next_offset"], -1)

    def test_climate_and_sensor_average_and_fahrenheit(self):
        self.entities.append(entity(
            "climate.lk_allrum_thermostat", "off", self.now,
            current_temperature=71.6, temperature=85, temperature_unit="°F",
        ))
        result = self.evaluate(room_sensors=["climate.lk_allrum_thermostat", "sensor.living"])
        self.assertEqual(result["indoor"], 21)

    def test_climate_without_current_temperature_never_uses_setpoint(self):
        self.entities.append(entity(
            "climate.lk_allrum_thermostat", "heat", self.now,
            temperature=21, temperature_unit="°C",
        ))
        with self.assertRaises(ControlError):
            self.evaluate(room_sensors=["climate.lk_allrum_thermostat"])

    def test_heat_request_is_bounded_to_one_step(self):
        result = self.evaluate(target_temperature=25)
        self.assertEqual(result["desired_offset"], 3)
        self.assertEqual(result["next_offset"], 1)

    def test_cooling_request_is_bounded(self):
        result = self.evaluate(target_temperature=17)
        self.assertEqual(result["desired_offset"], -3)
        self.assertEqual(result["next_offset"], -1)

    def test_monitor_without_actuator(self):
        result = self.evaluate(offset_entity="", target_temperature=23)
        self.assertFalse(result["writable"])
        self.assertIsNone(result["next_offset"])
        self.assertEqual(result["desired_offset"], 3)

    def test_weather_uses_current_temperature(self):
        result = self.evaluate(outdoor_entity="sensor.outdoor", weather_gain=0.1)
        self.assertEqual(result["contributions"]["weather"], 1)
        self.assertEqual(result["next_offset"], 1)

    def test_weather_entity_and_fahrenheit_conversion(self):
        self.entities[2] = entity("weather.home", "cloudy", self.now, temperature=14, temperature_unit="°F")
        self.entities[0] = entity("sensor.living", 68, self.now, unit_of_measurement="°F")
        result = self.evaluate(outdoor_entity="weather.home", weather_gain=0.1)
        self.assertEqual(result["indoor"], 21)
        self.assertEqual(result["outdoor"], -10)

    def test_price_reduces_when_comfortable(self):
        result = self.evaluate(price_entity="sensor.price", expensive_reduction=1)
        self.assertEqual(result["contributions"]["price"], -1)
        self.assertEqual(result["next_offset"], -1)
        self.assertEqual(result["price_unit"], "SEK/kWh")

    def test_price_does_not_reduce_when_cold(self):
        result = self.evaluate(target_temperature=23, price_entity="sensor.price", expensive_reduction=2)
        self.assertEqual(result["contributions"]["price"], 0)
        self.assertGreater(result["next_offset"], 0)

    def test_price_does_not_boost_when_hot(self):
        self.entities[3]["state"] = "-1"
        result = self.evaluate(target_temperature=19, price_entity="sensor.price", cheap_boost=2)
        self.assertEqual(result["contributions"]["price"], 0)

    def test_cheap_price_boost_in_comfort_band(self):
        self.entities[3]["state"] = "0.5"
        result = self.evaluate(price_entity="sensor.price", cheap_boost=1)
        self.assertEqual(result["contributions"]["price"], 1)

    def test_middle_price_has_no_effect(self):
        self.entities[3]["state"] = "1"
        result = self.evaluate(price_entity="sensor.price", cheap_boost=1, expensive_reduction=1)
        self.assertEqual(result["contributions"]["price"], 0)

    def test_unused_entities_do_not_block_control(self):
        self.entities[2]["state"] = "unavailable"
        self.entities[3]["state"] = "unavailable"
        self.evaluate()

    def test_missing_unavailable_invalid_and_stale_rooms_block(self):
        for value in ("unavailable", "unknown", "nan", "inf", "abc", "4", "41"):
            with self.subTest(value=value), self.assertRaises(ControlError):
                self.entities[0]["state"] = value
                self.evaluate()
        self.entities = fixtures(self.now - 181 * 60)
        with self.assertRaisesRegex(ControlError, "gammalt"):
            self.evaluate()
        self.entities = fixtures(self.now)[1:]
        with self.assertRaisesRegex(ControlError, "saknas"):
            self.evaluate()

    def test_fresh_last_reported_overrides_old_update(self):
        self.entities = fixtures(self.now - 100000)
        for item in self.entities:
            item["last_reported"] = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        self.evaluate()

    def test_future_timestamp_blocks(self):
        self.entities = fixtures(self.now + 120)
        with self.assertRaises(ControlError):
            self.evaluate()

    def test_unchanged_offset_is_current_while_same_device_reports(self):
        # myUplink keeps the old timestamp for unchanged values; a fresh sibling proves the link is alive.
        self.entities[-1] = entity("number.offset", 0, self.now - 9 * 3600, min=-10, max=10, step=1)
        with self.assertRaisesRegex(ControlError, "gammalt"):
            self.evaluate()
        self.entities.append(entity("sensor.supply", 32, self.now - 60, unit_of_measurement="°C"))
        peers = {"number.offset": ["sensor.supply"]}
        result = evaluate(config(), self.entities, self.now, peers)
        self.assertEqual(result["current_offset"], 0)

    def test_offset_blocks_when_whole_device_is_silent_or_unavailable(self):
        self.entities[-1] = entity("number.offset", 0, self.now - 9 * 3600, min=-10, max=10, step=1)
        peers = {"number.offset": ["sensor.supply", "sensor.missing"]}
        self.entities.append(entity("sensor.supply", 32, self.now - 4 * 3600))
        with self.assertRaisesRegex(ControlError, "samma enhet"):
            evaluate(config(), self.entities, self.now, peers)
        self.entities[-1] = entity("sensor.supply", "unavailable", self.now)
        with self.assertRaisesRegex(ControlError, "samma enhet"):
            evaluate(config(), self.entities, self.now, peers)
        self.entities[-1] = entity("sensor.supply", 32, self.now)
        self.entities[-2]["state"] = "unavailable"
        with self.assertRaisesRegex(ControlError, "otillgänglig"):
            evaluate(config(), self.entities, self.now, peers)

    def test_device_peers_groups_by_device_and_platform(self):
        registry = [
            {"entity_id": "number.offset", "device_id": "pump", "platform": "myuplink"},
            {"entity_id": "sensor.supply", "device_id": "pump", "platform": "myuplink"},
            {"entity_id": "sensor.other", "device_id": "other", "platform": "myuplink"},
            {"entity_id": "sensor.loose", "device_id": None, "platform": "myuplink"},
        ]
        peers = device_peers(registry)
        self.assertEqual(peers["number.offset"], ["sensor.supply"])
        self.assertEqual(peers["sensor.other"], [])
        self.assertNotIn("sensor.loose", peers)

    def test_unchanged_climate_uses_fresh_report_from_same_device(self):
        cfg = config(room_sensors=["climate.store"])
        states = [
            entity("climate.store", "heat", self.now - 4 * 3600,
                   current_temperature=21.9, temperature_unit="°C"),
            entity("sensor.store_temperature", 21.9, self.now, unit_of_measurement="°C"),
            entity("number.offset", 0, self.now, min=-10, max=10, step=1),
        ]
        registry = [
            {"entity_id": key, "device_id": "store", "platform": "climate_device"}
            for key in ("climate.store", "sensor.store_temperature")
        ]
        peers = device_peers(registry)
        self.assertEqual(evaluate(cfg, states, self.now, peers)["indoor"], 21.9)
        states[1]["last_updated"] = states[0]["last_updated"]
        with self.assertRaisesRegex(ControlError, "samma enhet"):
            evaluate(cfg, states, self.now, peers)
        states[1] = entity("sensor.store_temperature", 21.9, self.now, unit_of_measurement="°C")
        registry[1]["device_id"] = "different"
        with self.assertRaises(ControlError):
            evaluate(cfg, states, self.now, device_peers(registry))
        states[0]["state"] = "unavailable"
        with self.assertRaisesRegex(ControlError, "otillgänglig"):
            evaluate(cfg, states, self.now, peers)

    def test_unknown_temperature_unit_blocks(self):
        self.entities[0]["attributes"]["unit_of_measurement"] = "K"
        with self.assertRaises(ControlError):
            self.evaluate()

    def test_stale_price_blocks_when_used(self):
        self.entities[3] = entity("sensor.price", 3, self.now - 20000)
        with self.assertRaises(ControlError):
            self.evaluate(price_entity="sensor.price", cheap_boost=1)

    def test_actuator_bounds_step_and_current_are_checked(self):
        for attrs in ({"step": 0}, {"step": 2}, {"min": None}, {"min": -1}, {"max": -11}):
            with self.subTest(attrs=attrs), self.assertRaises(ControlError):
                self.entities = fixtures(self.now)
                self.entities[-1]["attributes"].update(attrs)
                self.evaluate()
        self.entities = fixtures(self.now)
        self.entities[-1]["state"] = "4"
        with self.assertRaisesRegex(ControlError, "utanför säkerhetsgränserna"):
            self.evaluate()

    def test_fractional_steps_respect_all_constraints(self):
        for step in (0.1, 0.5, 1):
            for current in (-2, 0, 2):
                for target in (10, 20.8, 21, 23, 30):
                    with self.subTest(step=step, current=current, target=target):
                        self.entities[-1] = entity("number.offset", current, self.now, min=-10, max=10, step=step)
                        result = self.evaluate(target_temperature=target, min_offset=-2.3, max_offset=2.3)
                        next_value = result["next_offset"]
                        self.assertLessEqual(abs(next_value - current), 1 + 1e-8)
                        self.assertTrue(-2.3 <= next_value <= 2.3)
                        self.assertAlmostEqual((next_value + 10) / step, round((next_value + 10) / step))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.states.side_effect = lambda: fixtures()
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)

    def enable(self):
        self.controller.save(config(enabled=True, target_temperature=23))

    def allow_write(self):
        self.controller.last_write = time.time() - 3600

    def test_monitor_never_writes(self):
        self.controller.save(config(target_temperature=23))
        self.allow_write()
        self.controller.tick()
        self.ha.set_offset.assert_not_called()

    def test_bound_explains_why_no_write_when_cold(self):
        self.controller.save(config(enabled=True, base_offset=3, target_temperature=22, priority_sensors=["sensor.living"]))
        self.ha.states.side_effect = lambda: [*fixtures()[:-1], entity("number.offset", 3, min=-10, max=10, step=1)]
        self.allow_write()
        self.controller.tick()
        self.assertIn("Övre säkerhetsgränsen", self.controller.snapshot()["message"])
        self.ha.set_offset.assert_not_called()

    def test_warm_room_warning_threshold_does_not_block_priority_control(self):
        self.controller.save(config(enabled=True, target_temperature=22, priority_sensors=["sensor.living"]))
        self.allow_write()
        for value, expected in ((23.9, []), (24, ["sensor.bedroom"]), (25, ["sensor.bedroom"]),
                                ("unavailable", [])):
            with self.subTest(value=value):
                self.ha.states.side_effect = lambda: [
                    fixtures()[0], entity("sensor.bedroom", value, unit_of_measurement="°C"), *fixtures()[2:],
                ]
                self.controller.tick()
                status = self.controller.snapshot()
                self.assertEqual(status["warm_room_limit"], 24)
                self.assertEqual(status["warm_rooms"], expected)
                self.assertEqual(status["next_offset"], 1)
        self.ha.set_offset.assert_called_once_with("number.offset", 1)
        self.controller.save(config(target_temperature=20, priority_sensors=["sensor.bedroom"]))
        self.ha.states.side_effect = lambda: [
            entity("sensor.living", 22, unit_of_measurement="°C"),
            entity("sensor.bedroom", 25, unit_of_measurement="°C"), *fixtures()[2:],
        ]
        self.controller.tick()
        self.assertEqual(self.controller.snapshot()["warm_rooms"], ["sensor.living"])
        self.assertEqual(self.controller.snapshot()["warm_room_limit"], 22)
        self.controller.save(config(target_temperature=20))
        self.controller.tick()
        self.assertEqual(self.controller.snapshot()["warm_rooms"], [])

    def test_priority_status_shows_all_rooms_and_distinct_averages(self):
        self.controller.save(config(priority_sensors=["sensor.living"]))
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertEqual((status["indoor"], status["house_indoor"]), (20, 21))
        self.assertEqual([r["temperature"] for r in status["rooms"]], [20, 22])
        self.assertEqual([r["priority"] for r in status["rooms"]], [True, False])

    def test_monitoring_room_failure_does_not_block_priority_control(self):
        self.controller.save(config(enabled=True, priority_sensors=["sensor.living"], target_temperature=22))
        self.allow_write()
        self.ha.states.side_effect = lambda: [
            fixtures()[0], entity("sensor.bedroom", "unavailable"), *fixtures()[2:],
        ]
        self.controller.tick()
        status = self.controller.snapshot()
        self.assertEqual(status["indoor"], 20)
        self.assertIsNone(status["house_indoor"])
        self.assertIsNotNone(status["rooms"][1]["error"])
        self.ha.set_offset.assert_called_once_with("number.offset", 1)
        self.ha.set_offset.reset_mock()
        self.ha.states.side_effect = lambda: [
            entity("sensor.living", "unavailable"), *fixtures()[1:],
        ]
        with self.assertRaises(ControlError):
            self.controller.tick()
        self.assertIsNone(self.controller.snapshot()["indoor"])
        self.ha.set_offset.assert_not_called()

    def test_priority_migration_preserves_previous_control(self):
        old = config(enabled=True, target_temperature=22)
        del old["priority_sensors"]
        self.controller.path.write_text(json.dumps(old))
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.settings()["priority_sensors"], [])
        self.assertTrue(restored.settings()["enabled"])
        restored.tick()
        self.assertEqual(restored.snapshot()["indoor"], 21)

    def test_unavailable_offset_keeps_fresh_temperatures_and_recovers(self):
        self.controller.save(config(enabled=True, target_temperature=23, outdoor_entity="sensor.outdoor"))
        self.allow_write()
        self.ha.states.side_effect = lambda: [
            *fixtures()[:-1], entity("number.offset", "unavailable", min=-10, max=10, step=1),
        ]
        with self.assertRaises(ControlError):
            self.controller.tick()
        status = self.controller.snapshot()
        self.assertEqual((status["indoor"], status["outdoor"]), (21, -10))
        self.assertIsNone(status["current_offset"])
        self.assertIn("current_offset", status["reading_errors"])
        self.assertNotIn("next_offset", status)
        self.ha.set_offset.assert_not_called()
        self.ha.states.side_effect = lambda: fixtures()
        self.controller.tick()
        self.assertIsNone(self.controller.snapshot()["error_message"])
        self.assertEqual(self.controller.snapshot()["current_offset"], 0)

    def test_unavailable_room_does_not_use_partial_average(self):
        self.enable()
        self.allow_write()
        self.ha.states.side_effect = lambda: [
            entity("sensor.living", "unavailable", unit_of_measurement="°C"), *fixtures()[1:],
        ]
        with self.assertRaises(ControlError):
            self.controller.tick()
        status = self.controller.snapshot()
        self.assertIsNone(status["indoor"])
        self.assertEqual(status["current_offset"], 0)
        self.assertIn("indoor", status["reading_errors"])
        self.ha.set_offset.assert_not_called()

    def test_stale_readings_are_not_displayed_as_current(self):
        self.controller.save(config(outdoor_entity="sensor.outdoor"))
        self.ha.states.side_effect = lambda: fixtures(time.time() - 86400)
        with self.assertRaises(ControlError):
            self.controller.tick()
        status = self.controller.snapshot()
        for key in ("indoor", "outdoor", "current_offset"):
            self.assertIsNone(status[key])
            self.assertIn(key, status["reading_errors"])

    def test_ha_failure_does_not_keep_old_readings(self):
        self.controller.save(config())
        self.controller.tick()
        self.ha.states.side_effect = ControlError("Ingen kontakt")
        with self.assertRaises(ControlError):
            self.controller.tick()
        self.assertNotIn("indoor", self.controller.snapshot())
        self.assertNotIn("current_offset", self.controller.snapshot())

    def test_startup_reads_then_writes_without_extra_wait(self):
        self.enable()
        self.controller.tick()
        self.ha.set_offset.assert_called_once_with("number.offset", 1)
        self.controller.tick()
        self.ha.set_offset.assert_called_once()

    def test_restart_preserves_remaining_interval_without_resetting_it(self):
        self.enable()
        now = time.time()
        self.controller.state_path.write_text(json.dumps({"last_attempt": now - 1700, "pending": None}))
        restored = Controller(self.ha, self.temp.name)
        restored.tick()
        self.assertTrue(95 <= restored.snapshot()["wait_seconds"] <= 100)
        self.ha.set_offset.assert_not_called()
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.last_write, now - 1700)
        with patch("controller.time.time", return_value=now + 99):
            restored.tick()
        self.ha.set_offset.assert_not_called()
        with patch("controller.time.time", return_value=now + 100):
            restored.tick()
        self.ha.set_offset.assert_called_once_with("number.offset", 1)

    def test_invalid_saved_attempt_stops_startup(self):
        for timestamp in (-1, "NaN", "Infinity"):
            with self.subTest(timestamp=timestamp):
                self.controller.state_path.write_text(json.dumps({"last_attempt": timestamp, "pending": None}))
                with self.assertRaises(ControlError):
                    Controller(self.ha, self.temp.name)
        self.ha.set_offset.assert_not_called()

    def test_restart_old_attempt_does_not_wait_again(self):
        self.enable()
        self.controller.state_path.write_text(json.dumps({"last_attempt": time.time() - 3600, "pending": None}))
        restored = Controller(self.ha, self.temp.name)
        restored.tick()
        self.ha.set_offset.assert_called_once_with("number.offset", 1)

    def test_restart_after_timeout_retains_interval_even_if_value_not_received(self):
        self.enable()
        self.ha.set_offset.side_effect = ControlError("Timeout")
        with self.assertRaises(ControlError):
            self.controller.tick()
        attempted_at = self.controller.last_write
        restored = Controller(self.ha, self.temp.name)
        restored.tick()
        self.assertEqual(restored.last_write, attempted_at)
        self.assertIsNotNone(restored.pending)
        self.ha.set_offset.assert_called_once()

    def test_restart_confirms_pending_without_shortening_interval(self):
        self.enable()
        self.controller.tick()
        before = self.controller.last_write
        restored = Controller(self.ha, self.temp.name)
        self.ha.states.side_effect = lambda: [*fixtures()[:-1], entity("number.offset", 1, min=-10, max=10, step=1)]
        restored.tick()
        self.assertIsNone(restored.pending)
        self.assertEqual(restored.last_write, before)
        self.ha.set_offset.assert_called_once()
        self.assertIn("write_observed", [e["kind"] for e in restored.events.recent()])

    def test_failed_write_is_rate_limited_and_visible(self):
        self.enable()
        self.allow_write()
        self.ha.set_offset.side_effect = ControlError("Timeout")
        with self.assertRaisesRegex(ControlError, "Timeout"):
            self.controller.tick()
        self.assertEqual(self.controller.snapshot()["error_message"], "Timeout")
        self.controller.tick()
        self.ha.set_offset.assert_called_once()
        state = json.loads((Path(self.temp.name) / "control-state.json").read_text())
        self.assertGreater(state["last_attempt"], time.time() - 10)

    def test_tick_resumes_with_unchanged_offset_when_pump_still_reports(self):
        old = time.time() - 9 * 3600

        def states():
            items = fixtures()
            items[-1] = entity("number.offset", 0, old, min=-10, max=10, step=1)
            items.append(entity("sensor.supply", 32))
            return items

        self.ha.states.side_effect = states
        self.ha.integration_registry.return_value = [
            {"entity_id": "number.offset", "device_id": "pump", "platform": "myuplink"},
            {"entity_id": "sensor.supply", "device_id": "pump", "platform": "myuplink"},
        ]
        self.enable()
        self.allow_write()
        self.controller.tick()
        self.ha.set_offset.assert_called_once_with("number.offset", 1)
        self.controller.tick()
        self.assertEqual(self.ha.integration_registry.call_count, 1, "registry is cached between ticks")

    def test_invalid_input_never_writes(self):
        self.enable()
        self.allow_write()
        self.ha.states.return_value = []
        self.ha.states.side_effect = None
        with self.assertRaises(ControlError):
            self.controller.tick()
        self.ha.set_offset.assert_not_called()

    def test_preflight_rejects_activation_and_preserves_settings(self):
        self.ha.states.side_effect = ControlError("Frånkopplad")
        with self.assertRaises(ControlError):
            self.enable()
        self.assertFalse(self.controller.settings()["enabled"])

    def test_disable_does_not_require_ha(self):
        self.enable()
        self.ha.states.side_effect = ControlError("Frånkopplad")
        self.controller.save(config(enabled=False))
        self.assertFalse(self.controller.settings()["enabled"])

    def test_settings_persist_without_mutable_aliases(self):
        self.controller.save(config())
        snapshot = self.controller.settings()
        snapshot["room_sensors"].append("sensor.extra")
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.settings()["room_sensors"], ["sensor.living", "sensor.bedroom"])
        self.assertFalse(restored.settings()["enabled"])

    def test_invalid_saved_file_stops_startup(self):
        (Path(self.temp.name) / "settings.json").write_text("{}")
        with self.assertRaises(ControlError):
            Controller(self.ha, self.temp.name)

    def test_disk_failure_prevents_write(self):
        self.enable()
        self.allow_write()
        with patch("controller.atomic_json", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.controller.tick()
        self.ha.set_offset.assert_not_called()

    def test_ha_http_failure_is_actionable_without_response_body(self):
        from urllib.error import HTTPError
        with patch("controller.urlopen", side_effect=HTTPError("url", 403, "forbidden", {}, None)):
            with self.assertRaisesRegex(ControlError, "HTTP 403"):
                HomeAssistant("secret").states()

    def test_climate_unit_comes_from_ha_config_and_is_cached(self):
        ha = HomeAssistant("token")
        thermostat = entity("climate.lk_allrum", "heat", current_temperature=22.2)
        ha.request = Mock(side_effect=[[thermostat], {"unit_system": {"temperature": "°C"}}, [thermostat]])
        self.assertEqual(ha.states()[0]["attributes"]["temperature_unit"], "°C")
        ha.states()
        self.assertEqual(ha.request.call_count, 3)

    def test_configuration_log_has_before_and_after(self):
        self.enable()
        saved = next(e for e in self.controller.events.recent() if e["kind"] == "settings_saved")
        self.assertEqual(saved["details"]["changes"]["enabled"], {"from": False, "to": True})
        self.assertEqual(saved["details"]["changes"]["target_temperature"], {"from": 21, "to": 23})

    def test_attempt_sent_and_readback_are_separate_durable_events(self):
        self.enable()
        self.allow_write()
        self.controller.tick()
        kinds = [e["kind"] for e in self.controller.events.recent()]
        self.assertIn("write_attempt", kinds)
        self.assertIn("write_sent", kinds)
        self.assertNotIn("write_observed", kinds)
        self.ha.states.side_effect = lambda: [*fixtures()[:-1], entity("number.offset", 1, min=-10, max=10, step=1)]
        self.controller.tick()
        kinds = [e["kind"] for e in self.controller.events.recent()]
        self.assertIn("write_observed", kinds)
        restored = Controller(self.ha, self.temp.name)
        self.assertIn("write_observed", [e["kind"] for e in restored.events.recent()])
        self.assertIsNone(restored.pending)

    def test_pending_request_survives_restart(self):
        self.enable()
        self.allow_write()
        self.controller.tick()
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.pending["value"], 1)
        self.assertEqual(restored.pending["entity_id"], "number.offset")

    def test_timeout_is_logged_and_can_still_be_confirmed(self):
        self.enable()
        self.allow_write()
        self.ha.set_offset.side_effect = ControlError("Timeout")
        with self.assertRaises(ControlError):
            self.controller.tick()
        self.assertIn("write_failed", [e["kind"] for e in self.controller.events.recent()])
        self.ha.states.side_effect = lambda: [*fixtures()[:-1], entity("number.offset", 1, min=-10, max=10, step=1)]
        self.controller.tick()
        self.assertIn("write_observed", [e["kind"] for e in self.controller.events.recent()])
        self.ha.set_offset.assert_called_once()

    def test_unconfirmed_request_is_not_logged_as_success(self):
        self.enable()
        self.allow_write()
        self.controller.tick()
        self.controller.pending["timestamp"] -= 4000
        self.controller.tick()
        kinds = [e["kind"] for e in self.controller.events.recent()]
        self.assertIn("write_unconfirmed", kinds)
        self.assertNotIn("write_observed", kinds)

    def test_manual_offset_change_is_logged_without_claiming_ownership(self):
        self.controller.save(config())
        self.controller.tick()
        self.ha.states.side_effect = lambda: [*fixtures()[:-1], entity("number.offset", 2, min=-10, max=10, step=1)]
        self.controller.tick()
        observed = next(e for e in self.controller.events.recent() if e["kind"] == "offset_observed")
        self.assertEqual(observed["details"]["value"], 2)
        self.ha.set_offset.assert_not_called()

    def test_log_failure_prevents_automatic_write(self):
        import sqlite3
        self.enable()
        self.allow_write()
        with patch.object(self.controller.events, "add", side_effect=sqlite3.OperationalError("Disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.controller.tick()
        self.ha.set_offset.assert_not_called()

    def test_migrating_011_settings_preserves_active_configuration(self):
        old = config(enabled=True, target_temperature=22)
        del old["forecast_entity"]
        (Path(self.temp.name) / "settings.json").write_text(json.dumps(old))
        restored = Controller(self.ha, self.temp.name)
        self.assertTrue(restored.settings()["enabled"])
        self.assertEqual(restored.settings()["target_temperature"], 22)
        self.assertEqual(restored.settings()["forecast_entity"], "")

    def test_events_paginate_without_losing_records(self):
        for i in range(110):
            self.controller.events.add("test", str(i))
        page = self.controller.events.recent()
        older = self.controller.events.recent(page[-1]["id"])
        self.assertEqual(len(page), 100)
        self.assertEqual(len(older), 11)
        self.assertFalse(set(e["id"] for e in page) & set(e["id"] for e in older))


if __name__ == "__main__":
    unittest.main()
