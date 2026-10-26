import copy
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError
from insights import (Insights, charge_periods, draw_offs, hot_water, actual_changes, curve_points, discover_tibber, fit_curve, history_series, overview,
                      planned_offsets, supply_entity, tibber_prices)
from test_controller import config, entity


class TibberTests(unittest.TestCase):
    def test_unique_tibber_price_is_found_without_changing_controller(self):
        states = [entity("sensor.electricity_price_home", 1.4, unit_of_measurement="SEK/kWh")]
        registry = [{"entity_id": states[0]["entity_id"], "device_id": "home", "platform": "tibber"}]
        self.assertEqual(discover_tibber(states, registry)["price_entity"], states[0]["entity_id"])
        registry[0]["platform"] = "other"
        self.assertEqual(discover_tibber(states, registry)["price_entity"], "")

    def test_multiple_homes_are_not_guessed_even_if_unavailable(self):
        states = [entity("sensor.price_a", 1, unit_of_measurement="SEK/kWh"), entity("sensor.price_b", "unavailable", unit_of_measurement="SEK/kWh")]
        registry = [{"entity_id": s["entity_id"], "platform": "tibber"} for s in states]
        self.assertEqual(discover_tibber(states, registry)["price_entity"], "")

    def test_cost_and_consumption_are_not_mistaken_for_price(self):
        states = [entity("sensor.monthly_cost", 200, unit_of_measurement="SEK"), entity("sensor.consumption", 100, unit_of_measurement="kWh")]
        registry = [{"entity_id": s["entity_id"], "platform": "tibber"} for s in states]
        self.assertEqual(discover_tibber(states, registry)["price_entity"], "")


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.config = config(offset_entity="number.pump_heating_offset_climate_system_1")
        self.states = [
            entity(self.config["offset_entity"], 3),
            entity("binary_sensor.pump_alarm", "off", device_class="problem"),
            entity("sensor.pump_status_compressor", "Operating"),
            entity("sensor.pump_current_compressor_frequency", 45, unit_of_measurement="Hz"),
            entity("sensor.pump_current_be1", 2, unit_of_measurement="A", device_class="current"),
            entity("sensor.power_home", 1200, unit_of_measurement="W", device_class="power"),
            entity("sensor.power_production_home", 600, unit_of_measurement="W", device_class="power"),
            entity("sensor.accumulated_consumption_home", 12, unit_of_measurement="kWh", device_class="energy"),
            entity("sensor.electricity_price_home", 1.4, unit_of_measurement="SEK/kWh"),
        ]
        self.registry = [
            {"entity_id": s["entity_id"], "device_id": "pump" if "pump" in s["entity_id"] else "home",
             "platform": "myuplink" if "pump" in s["entity_id"] else "tibber"}
            for s in self.states
        ]

    def test_alarm_compressor_and_house_power_are_distinguished(self):
        data = overview(self.states, self.registry, self.config, self.now)
        alarm = next(item for item in data["pump"] if item["key"] == "alarm")
        compressor = next(item for item in data["pump"] if item["key"] == "compressor")
        self.assertFalse(alarm["active"])
        self.assertEqual(compressor["value"], "I drift")
        self.assertEqual(data["house"][0]["value"], "1200")
        self.assertEqual(data["house"][1]["value"], "12")
        self.assertEqual(data["price"]["value"], "1.4")
        self.assertEqual(data["pump_electricity"], [])

    def test_active_alarm_is_visible_and_stale_alarm_is_not_reported_as_clear(self):
        self.states[1]["state"] = "on"
        data = overview(self.states, self.registry, self.config, self.now)
        alarm = next(item for item in data["pump"] if item["key"] == "alarm")
        self.assertTrue(alarm["active"])
        old = datetime.fromtimestamp(self.now - 20000, timezone.utc).isoformat()
        self.states[1]["last_updated"] = old
        data = overview(self.states, self.registry, self.config, self.now)
        alarm = next(item for item in data["pump"] if item["key"] == "alarm")
        self.assertTrue(alarm["active"], "unchanged alarm stays valid while the same pump reports other values")
        for state in self.states:
            if "pump" in state["entity_id"]:
                state["last_updated"] = old
        data = overview(self.states, self.registry, self.config, self.now)
        alarm = next(item for item in data["pump"] if item["key"] == "alarm")
        self.assertIsNone(alarm["value"])
        self.assertIsNone(alarm["active"])
        self.assertIn("samma enhet", alarm["error"])

    def test_other_device_never_used_for_selected_pump(self):
        self.registry[1]["device_id"] = "another-pump"
        data = overview(self.states, self.registry, self.config, self.now)
        self.assertIsNone(next(item for item in data["pump"] if item["key"] == "alarm")["value"])

    def test_missing_pump_identity_does_not_guess_from_friendly_name(self):
        data = overview(self.states, [], self.config, self.now)
        self.assertTrue(all(item["value"] is None for item in data["pump"]))

    def test_nordpool_overview_never_uses_tibber_meters_or_text(self):
        price_id = "sensor.nord_pool_se3_aktuellt_pris"
        self.states.append(entity(price_id, 0.5, unit_of_measurement="SEK/kWh"))
        self.config.update(price_provider="nordpool", price_entity=price_id)
        for registry in (self.registry, []):
            with self.subTest(tibber_installed=bool(registry)):
                data = overview(self.states, registry, self.config, self.now)
                self.assertEqual(data["price"]["entity_id"], price_id)
                self.assertEqual(data["price"]["value"], "0.5")
                self.assertIn("Nord Pool", data["price"]["label"])
                self.assertTrue(all(item["value"] is None for item in data["house"]))
                self.assertNotIn("Tibber", str(data))
                self.assertIn("hämtas inte automatiskt", data["electricity_note"])

    def test_overview_cache_follows_saved_provider_when_switching_back(self):
        ha = Mock()
        ha.states.return_value = self.states
        ha.integration_registry.return_value = self.registry
        insights = Insights(ha)
        tibber = insights.overview(self.config)
        nordpool = insights.overview({**self.config, "price_provider": "nordpool"})
        self.assertEqual(tibber["house"][0]["value"], "1200")
        self.assertIsNone(nordpool["house"][0]["value"])
        self.assertNotIn("Tibber", str(nordpool))
        self.assertEqual(insights.overview(self.config), tibber)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.config = config(room_sensors=["climate.lk_allrum_thermostat", "sensor.room"])
        self.states = [
            entity("climate.lk_allrum_thermostat", "heat", current_temperature=22, temperature=29),
            entity("sensor.room", 21, unit_of_measurement="°C"),
        ]

    def test_climate_history_uses_measured_temperature_and_preserves_gaps(self):
        history = [[
            entity("climate.lk_allrum_thermostat", "heat", self.now - 600, current_temperature=22, temperature=29),
            entity("climate.lk_allrum_thermostat", "unavailable", self.now - 300),
            entity("climate.lk_allrum_thermostat", "heat", self.now, current_temperature=23, temperature=30),
        ]]
        data = history_series(history, self.config, self.states, "°C", self.now - 86400, self.now)
        climate = next(line for line in data["series"] if line["entity_id"].startswith("climate."))
        self.assertEqual([p[1] for p in climate["points"]], [22, None, 23])
        self.assertEqual(data["target"], 21)

    def test_climate_history_includes_setpoint_changes_and_off_mode(self):
        history = [[
            entity("climate.lk_allrum_thermostat", "heat", self.now - 900, current_temperature=21.5, temperature=22),
            entity("climate.lk_allrum_thermostat", "heat", self.now - 600, current_temperature=21.8, temperature=22),
            entity("climate.lk_allrum_thermostat", "heat", self.now - 300, current_temperature=21.9, temperature=23),
            entity("climate.lk_allrum_thermostat", "off", self.now, current_temperature=22, temperature=23),
        ]]
        data = history_series(history, self.config, self.states, "°C", self.now - 86400, self.now)
        climate = next(line for line in data["series"] if line["kind"] == "climate")
        self.assertEqual([p[1] for p in climate["setpoints"]], [22, 23, None])
        sensor = next(line for line in data["series"] if line["kind"] == "sensor")
        self.assertNotIn("setpoints", sensor)

    def test_unrequested_entities_cannot_appear_in_graph(self):
        data = history_series([[entity("sensor.private", 99)]], self.config, self.states, "°C", self.now - 86400, self.now)
        self.assertEqual(len(data["series"]), 2)
        self.assertTrue(all(not series["points"] for series in data["series"]))

    def test_invalid_history_shape_and_timestamp_are_explicit_errors(self):
        for invalid in ({}, [None]):
            with self.assertRaises(ControlError):
                history_series(invalid, self.config, self.states, "°C", 0, self.now)
        broken = entity("sensor.room", 20, unit_of_measurement="°C")
        broken["last_updated"] = "not a time"
        with self.assertRaises(ControlError):
            history_series([[broken]], self.config, self.states, "°C", 0, self.now)

    def test_history_query_is_limited_to_saved_ids_and_24_hours(self):
        ha = Mock()
        ha._temperature_unit = "°C"
        ha.request.return_value = []
        ha.states.return_value = self.states
        result = Insights(ha).history(self.config)
        self.assertEqual(result["end"] - result["start"], 86400)
        path = ha.request.call_args.args[0]
        self.assertTrue(unquote(path).startswith("/history/period/"))
        self.assertEqual(set(parse_qs(urlsplit(path).query)["filter_entity_id"][0].split(",")), set(self.config["room_sensors"]))

    def test_cache_does_not_make_repeated_ha_requests_or_mutate_result(self):
        ha = Mock()
        ha._temperature_unit = "°C"
        ha.request.return_value = []
        ha.states.return_value = self.states
        insights = Insights(ha)
        first = insights.history(self.config)
        first["series"].clear()
        second = insights.history(self.config)
        self.assertEqual(len(second["series"]), 2)
        ha.request.assert_called_once()


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.ha = Mock()
        self.weather = entity("weather.smhi_home", "cloudy", supported_features=3, temperature_unit="°C")
        self.ha.states.return_value = [self.weather, entity("weather.other", "sunny", supported_features=3, temperature_unit="°C")]
        self.ha.integration_registry.return_value = [{"entity_id": "weather.smhi_home", "platform": "smhi"}]
        self.response = {"service_response": {"weather.smhi_home": {"forecast": [
            {"datetime": datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat(), "temperature": 10, "condition": "cloudy"},
        ]}}}
        self.ha.request.return_value = self.response
        self.insights = Insights(self.ha)

    def test_auto_forecast_prefers_unique_smhi_and_does_not_control_pump(self):
        result = self.insights.forecast("")
        self.assertEqual(result["entity_id"], "weather.smhi_home")
        self.assertEqual(result["points"][0]["temperature"], 10)
        self.assertEqual(self.ha.request.call_args.args, (
            "/services/weather/get_forecasts?return_response", {"entity_id": "weather.smhi_home", "type": "hourly"},
        ))
        self.ha.set_offset.assert_not_called()

    def test_multiple_unidentified_sources_require_choice(self):
        self.ha.integration_registry.return_value = []
        with self.assertRaisesRegex(ControlError, "Välj"):
            self.insights.forecast("")

    def test_explicit_choice_and_fahrenheit_forecast(self):
        self.weather["attributes"]["temperature_unit"] = "°F"
        self.response["service_response"]["weather.smhi_home"]["forecast"][0]["temperature"] = 50
        result = self.insights.forecast("weather.smhi_home")
        self.assertEqual(result["points"][0]["temperature"], 10)

    def test_unavailable_and_empty_forecast_are_not_success(self):
        self.weather["state"] = "unavailable"
        with self.assertRaises(ControlError):
            self.insights.forecast("")
        self.weather["state"] = "cloudy"
        self.response["service_response"]["weather.smhi_home"]["forecast"] = []
        with self.assertRaises(ControlError):
            self.insights.forecast("")

    def test_expired_forecast_is_rejected(self):
        row = self.response["service_response"]["weather.smhi_home"]["forecast"][0]
        row["datetime"] = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
        with self.assertRaisesRegex(ControlError, "gamla"):
            self.insights.forecast("")

    def test_bad_response_is_explicit_error_and_is_not_cached(self):
        self.ha.request.return_value = {}
        with self.assertRaises(ControlError):
            self.insights.forecast("")
        self.ha.request.return_value = self.response
        self.assertEqual(self.insights.forecast("")["points"][0]["temperature"], 10)


if __name__ == "__main__":
    unittest.main()


def row(entity_id, value, stamp):
    return {"entity_id": entity_id, "state": str(value), "last_changed": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.offset = "number.pump_heating_offset_climate_system_1"
        self.supply = "sensor.pump_calculated_supply_climate_system_1"
        self.outdoor = "sensor.pump_current_outd_temp_bt1"
        self.registry = [
            {"entity_id": self.offset, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": self.supply, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": "sensor.other_calculated_supply_climate_system_1", "device_id": "other", "platform": "myuplink"},
            {"entity_id": "sensor.electricity_price_hem", "device_id": "home", "platform": "tibber"},
        ]
        self.status = {"error_message": None, "error": 0.0, "current_offset": 2,
                       "contributions": {"room": 0, "weather": 0, "price": 0}}

    def test_supply_entity_requires_same_pump_and_climate_system(self):
        self.assertEqual(supply_entity(self.registry, self.offset), self.supply)
        self.assertIsNone(supply_entity(self.registry, "number.pump_heating_offset_climate_system_2"))
        self.assertIsNone(supply_entity(self.registry[1:], self.offset))

    def test_tibber_prices_require_one_home_and_numbers(self):
        response = {"service_response": {"prices": {"hem": [
            {"start_time": "2026-10-23T00:15:00.000+02:00", "price": 1.1},
            {"start_time": "2026-10-23T00:00:00.000+02:00", "price": 1.2},
        ]}}}
        prices = tibber_prices(response)
        self.assertEqual([p[1] for p in prices], [1.2, 1.1])
        self.assertEqual(prices[1][0] - prices[0][0], 900)
        for bad in ({"service_response": {"prices": {"a": [], "b": []}}}, {"service_response": {}},
                    {"service_response": {"prices": {"hem": [{"start_time": "x", "price": 1}]}}},
                    {"service_response": {"prices": {"hem": [{"start_time": "2026-10-23T00:00:00+02:00", "price": "nan"}]}}}):
            with self.assertRaises(ControlError):
                tibber_prices(bad)

    def test_curve_pairs_values_at_same_time_and_fits_current_offset_only(self):
        start = self.now // 1800 * 1800 - 7 * 86400
        outdoor = [[start + i * 3600, 10 - i] for i in range(12)]
        supply = [[start + i * 3600 + 60, 30 + (i) * 1.0] for i in range(12)]
        offset = [[start, 1], [start + 6 * 3600, 2]]
        points = curve_points(supply, outdoor, offset)
        self.assertTrue(all(len(point) == 3 for point in points))
        self.assertEqual(points[0], [10, 30, 1])
        fit = fit_curve(points, 2)
        self.assertEqual(fit["count"], 6)
        self.assertAlmostEqual(fit["slope"], -1)
        self.assertIsNone(fit_curve(points, 3))
        self.assertIsNone(fit_curve(points[:4], 1))

    def test_plan_without_weather_or_price_effect_only_follows_room(self):
        cfg = config(offset_entity=self.offset)
        status = {**self.status, "contributions": {"room": 1.5, "weather": 0, "price": 0}, "offset_limits": {"min": -10, "max": 10, "step": 1}}
        plan = planned_offsets(cfg, status, [], [[self.now + 3600, -5]], self.now, self.now + 86400, True)
        self.assertEqual([p[1] for p in plan["desired"]], [min(cfg["max_offset"], cfg["base_offset"] + 1.5)])
        self.assertEqual(plan["planned"], [[self.now, 2]])
        self.assertTrue(any("rumstemperaturen" in note for note in plan["notes"]))

    def test_plan_simulates_price_rule_step_grid_and_write_interval(self):
        cfg = config(offset_entity=self.offset, price_entity="sensor.electricity_price_hem", cheap_boost=1,
                     expensive_reduction=1, price_low=1, price_high=2, base_offset=0, min_offset=-3, max_offset=3,
                     max_step=1, min_interval_minutes=60)
        status = {**self.status, "current_offset": 0, "offset_limits": {"min": -10, "max": 10, "step": 1}, "wait_seconds": 0}
        prices = [[self.now - 300, 1.5], [self.now + 3600, 0.5], [self.now + 3900, 2.5], [self.now + 7200, 1.5]]
        plan = planned_offsets(cfg, status, [], prices, self.now, self.now + 86400, True)
        self.assertEqual([p[1] for p in plan["desired"]], [0, 1, -1, 0])
        # The cheap slot is used at once; the expensive slot must wait for the minimum interval.
        self.assertEqual([(round(c["timestamp"] - self.now), c["previous"], c["offset"]) for c in plan["changes"]],
                         [(3600, 0, 1), (7200, 1, 0)])
        self.assertIn("billigt elpris", plan["changes"][0]["reasons"][0])
        cold = {**status, "error": 2}
        plan = planned_offsets(cfg, cold, [], prices, self.now, self.now + 86400, True)
        self.assertEqual([p[1] for p in plan["desired"]], [0, 1, 0])
        plan = planned_offsets(cfg, status, [], prices, self.now, self.now + 86400, False)
        self.assertTrue(any("Tibber" in note for note in plan["notes"]))
        self.assertEqual(plan["changes"], [])
        cfg["price_provider"] = "nordpool"
        plan = planned_offsets(cfg, status, [], [], self.now, self.now + 86400, False)
        self.assertTrue(any("Nord Pool" in note for note in plan["notes"]))
        self.assertNotIn("Tibber", str(plan))

    def test_plan_respects_wait_time_and_max_step(self):
        cfg = config(offset_entity=self.offset, base_offset=3, min_offset=-3, max_offset=3, max_step=1, min_interval_minutes=30)
        status = {**self.status, "current_offset": 0, "offset_limits": {"min": -10, "max": 10, "step": 1}, "wait_seconds": 600}
        plan = planned_offsets(cfg, status, [], [], self.now, self.now + 86400, True)
        self.assertEqual([(round(c["timestamp"] - self.now), c["offset"]) for c in plan["changes"]], [(600, 1), (2400, 2), (4200, 3)])
        self.assertEqual(plan["changes"][1]["reasons"], ["fortsätter mot målet"])

    def test_plan_weather_uses_forecast_and_paused_controller_gives_no_plan(self):
        cfg = config(offset_entity=self.offset, outdoor_entity=self.outdoor, weather_gain=0.1, weather_reference=10,
                     base_offset=0, min_offset=-3, max_offset=3, enabled=False)
        forecast = [[self.now + 3600, 0], [self.now + 7200, -10]]
        plan = planned_offsets(cfg, self.status, forecast, [], self.now, self.now + 86400, True)
        self.assertEqual([p[1] for p in plan["desired"]], [1, 2])
        self.assertTrue(any("avstängd" in note for note in plan["notes"]))
        plan = planned_offsets(cfg, {"error_message": "fel"}, forecast, [], self.now, self.now + 86400, True)
        self.assertEqual((plan["planned"], plan["changes"]), ([], []))
        self.assertIn("Ingen plan", plan["notes"][0])

    def test_actual_changes_are_attributed_only_with_matching_logged_write(self):
        history = [[self.now - 86400, 3], [self.now - 7200, None], [self.now - 7100, 3], [self.now - 3600, 2], [self.now - 600, 1]]
        events = [{"kind": "write_attempt", "timestamp": self.now - 3700,
                   "details": {"value": 2, "contributions": {"room": -1.5, "weather": 0, "price": 0}}}]
        changes = actual_changes(history, events, self.now - 86400)
        self.assertEqual([(c["previous"], c["offset"], c["source"]) for c in changes],
                         [(3, 2, "automatik"), (2, 1, "manuell eller annan automation")])
        self.assertEqual(changes[0]["reasons"], ["rum -1.5"])

    def test_plan_endpoint_reads_history_prices_and_never_writes(self):
        cfg = config(offset_entity=self.offset, outdoor_entity=self.outdoor, price_entity="sensor.electricity_price_hem")
        start = self.now // 1800 * 1800 - 3 * 86400
        history = [
            [row(self.outdoor, 5, start)] + [{"state": str(5 - i), "last_changed": datetime.fromtimestamp(start + i * 3600, timezone.utc).isoformat()} for i in range(1, 12)],
            [row(self.supply, 30, start + 60)] + [{"state": str(30 + i), "last_changed": datetime.fromtimestamp(start + i * 3600 + 60, timezone.utc).isoformat()} for i in range(1, 12)],
            [row(self.offset, 2, start)],
        ]
        tibber = {"service_response": {"prices": {"hem": [
            {"start_time": datetime.fromtimestamp(self.now - 600, timezone.utc).isoformat(), "price": 1.2}]}}}

        def request(path, data=None):
            if path.startswith("/history/period/"):
                return history
            if path.startswith("/services/tibber/get_prices"):
                self.assertIn("start", data)
                return tibber
            raise AssertionError(path)
        ha = Mock()
        ha.request.side_effect = request
        ha.integration_registry.return_value = self.registry
        ha.states.return_value = [entity(self.outdoor, 0, unit_of_measurement="°C"),
                                  entity("sensor.electricity_price_hem", 1.2, unit_of_measurement="SEK/kWh")]
        events = [{"kind": "write_attempt", "timestamp": self.now - 100, "details": {"value": 2}}]
        plan = Insights(ha).plan(cfg, self.status, events)
        self.assertEqual(plan["offset"]["done"], [])
        query = parse_qs(urlsplit(next(c.args[0] for c in ha.request.call_args_list if c.args[0].startswith("/history"))).query, keep_blank_values=True)
        self.assertEqual(set(query["filter_entity_id"][0].split(",")), {self.outdoor, self.offset, self.supply})
        self.assertIn("no_attributes", query)
        self.assertEqual(plan["prices"]["points"][0][1], 1.2)
        self.assertEqual(plan["prices"]["unit"], "SEK/kWh")
        self.assertEqual(plan["curve"]["fit"]["slope"], -1)
        self.assertEqual(plan["curve"]["current"]["offset"], 2)
        self.assertEqual(plan["offset"]["history"][-1][1], 2)
        self.assertIn("Välj en väderentitet", plan["outdoor"]["error"])
        ha.set_offset.assert_not_called()


class HotWaterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.config = config(offset_entity="number.pump_heating_offset_climate_system_1")
        self.states = [
            entity(self.config["offset_entity"], 0, self.now),
            entity("sensor.pump_hot_water_top_bt7", 50.4, self.now, unit_of_measurement="°C"),
            entity("sensor.pump_hot_water_charging_bt6", 50, self.now, unit_of_measurement="°C"),
            entity("sensor.pump_hot_water_amount", 12, self.now, unit_of_measurement="min"),
            entity("select.pump_hot_water_amount", "Normal", self.now),
            entity("sensor.pump_status", "Hot water", self.now),
            entity("sensor.other_hot_water_top_bt7", 10, self.now, unit_of_measurement="°C"),
        ]
        self.registry = [{"entity_id": s["entity_id"], "platform": "myuplink",
                          "device_id": "other" if "other" in s["entity_id"] else "pump"} for s in self.states]

    def row(self, entity_id, value, ago):
        return {"entity_id": entity_id, "state": str(value),
                "last_changed": datetime.fromtimestamp(self.now - ago, timezone.utc).isoformat()}

    def test_shower_is_found_but_standby_loss_is_not(self):
        t = self.now - 86400
        points = [[t, 50.0], [t + 7200, 49.5], [t + 14400, 49.0],  # slow standby loss
                  [t + 20000, 45.0], [t + 20300, 38.0], [t + 20600, 33.0],  # shower
                  [t + 30000, 50.0], [t + 50000, 48.0]]  # long gap but single 2 degree step
        events = draw_offs(points, self.now)
        self.assertEqual([e["size"] for e in events], ["stor", "liten"])
        self.assertEqual(events[0]["drop"], 16.0)
        self.assertEqual(events[0]["start"], t + 20000 - 900)

    def test_charge_periods_join_and_run_until_now(self):
        rows = [[0, "Heating"], [100, "Hot water"], [400, "Heating"], [900, "Hot water"]]
        self.assertEqual(charge_periods(rows, 1000), [{"start": 100, "end": 400}, {"start": 900, "end": 1000}])

    def test_level_uses_pump_minutes_and_ignores_other_device(self):
        groups = {
            "sensor.pump_hot_water_amount": [self.row("sensor.pump_hot_water_amount", 24, 3600),
                                             self.row("sensor.pump_hot_water_amount", 12, 600)],
            "sensor.pump_status": [self.row("sensor.pump_status", "Heating", 3600),
                                   self.row("sensor.pump_status", "Hot water", 300)],
        }
        data = hot_water(self.states, self.registry, self.config, groups, self.now)
        self.assertIsNone(data["error"])
        self.assertEqual(data["top"], 50.4)
        self.assertEqual(data["level_source"], "minutes")
        self.assertAlmostEqual(data["level"], 0.5)
        self.assertTrue(data["charging"])
        self.assertEqual(data["demand"], "Normal")
        self.assertEqual(len(data["charges"]), 1)
        self.assertNotIn("other", json_dump(data["entities"]))

    def test_level_falls_back_to_charge_sensor(self):
        states = [s for s in self.states if s["entity_id"] != "sensor.pump_hot_water_amount"]
        groups = {"sensor.pump_hot_water_charging_bt6": [self.row("sensor.pump_hot_water_charging_bt6", 55, 3600),
                                                        self.row("sensor.pump_hot_water_charging_bt6", 50, 600)]}
        data = hot_water(states, self.registry, self.config, groups, self.now)
        self.assertEqual(data["level_source"], "charge")
        self.assertAlmostEqual(data["level"], 25 / 30)

    def test_stale_hot_water_values_are_not_shown(self):
        old = self.now - 86400
        states = [entity(s["entity_id"], s["state"], old, **s["attributes"]) for s in self.states]
        data = hot_water(states, self.registry, self.config, {}, self.now)
        self.assertIsNone(data["top"])
        self.assertIsNone(data["charge"])
        self.assertIsNone(data["charging"])

    def test_missing_pump_gives_swedish_error(self):
        data = hot_water(self.states, self.registry, config(offset_entity="number.unknown"), {}, self.now)
        self.assertIn("varmvattengivare", data["error"])


def json_dump(value):
    import json
    return json.dumps(value)
