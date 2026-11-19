import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError, Controller, validate
from energy import change_comparisons, consumption_report, discover_meter, meter_error, price_intervals
from insights import Insights
from savings import action_periods, energy_periods, period_bounds
from test_controller import config, entity


METER = "sensor.pump_energy_consumed"
PRICE = "sensor.price"
START = 1793404800 // 3600 * 3600
END = START + 86400


def row(eid, value, stamp, **attrs):
    return entity(eid, value, stamp, **attrs)


def meter_rows(unit="kWh", scale=1):
    return [row(METER, (100 + i) * scale, START + i * 3600,
                device_class="energy", state_class="total_increasing", unit_of_measurement=unit)
            for i in range(25)]


def price_rows(unit="SEK/kWh", value=2):
    return [row(PRICE, value, START + i * 3600, unit_of_measurement=unit) for i in range(24)]


POWER = "sensor.pump_el_current_power_consumption"


def power_rows(unit="kW", scale=1):
    return [row(POWER, 2 * scale, START + i * 300,
                device_class="power", state_class="measurement", unit_of_measurement=unit)
            for i in range(289)]


def report(meter=None, prices=None):
    return consumption_report(meter_rows() if meter is None else meter,
                              price_rows() if prices is None else prices, METER, PRICE, START, END, 7200)


class EnergyTests(unittest.TestCase):
    def test_power_integration_units_and_cost(self):
        for unit, scale in (("W", 1000), ("kW", 1)):
            data = consumption_report(power_rows(unit, scale), price_rows(), POWER, PRICE, START, END, 7200, power=True)
            self.assertEqual(data["method"], "power")
            self.assertAlmostEqual(data["totals"]["kwh"], 48)
            self.assertAlmostEqual(data["totals"]["cost"], 96)
            self.assertTrue(all(abs(h["kwh"] - 2) < 1e-8 for h in data["hours"]))

    def test_power_holds_previous_sample_including_decreases_and_zero(self):
        rows = power_rows()[:4]
        for r, value in zip(rows, (6, 3, 0, 100)):
            r["state"] = str(value)
        data = consumption_report(rows, price_rows(), POWER, PRICE, START, END, 7200, power=True)
        self.assertAlmostEqual(data["totals"]["measured_kwh"], 0.75)
        self.assertAlmostEqual(data["totals"]["partial_cost"], 1.5)
        self.assertEqual(data["totals"]["coverage_seconds"], 900)
        self.assertIsNone(data["totals"]["kwh"], "No extrapolation after the last sample")

    def test_power_invalid_values_and_large_gaps_are_not_covered(self):
        for state in ("unknown", "unavailable", "-1", "invalid"):
            rows = power_rows()[:4]
            rows[1]["state"] = state
            data = consumption_report(rows, price_rows(), POWER, PRICE, START, END, 7200, power=True)
            self.assertEqual(data["totals"]["coverage_seconds"], 900, "A short myUplink outage keeps the last value")
            self.assertEqual(data["bridged_seconds"], 300)
            self.assertEqual(data["skipped_intervals"], 0)
            rows = power_rows()[:6]
            for index in (1, 2, 3):
                rows[index]["state"] = state
            data = consumption_report(rows, price_rows(), POWER, PRICE, START, END, 7200, power=True)
            self.assertEqual(data["totals"]["coverage_seconds"], 600,
                             "A longer outage is not filled; the value before it counts up to the outage")
            self.assertEqual(data["bridged_seconds"], 0)
            self.assertEqual(data["skipped_intervals"], 3)
        rows = power_rows()
        data = consumption_report([rows[0], rows[3]], price_rows(), POWER, PRICE, START, END, 7200, power=True)
        self.assertEqual(data["totals"]["coverage_seconds"], 900)
        data = consumption_report([rows[0], rows[4]], price_rows(), POWER, PRICE, START, END, 7200, power=True)
        self.assertEqual(data["totals"]["coverage_seconds"], 0)
        rows[1]["attributes"]["unit_of_measurement"] = "kWh"
        rows[1]["attributes"]["device_class"] = "energy"
        rows[1]["attributes"]["state_class"] = "total_increasing"
        with self.assertRaisesRegex(ControlError, "mätmetod"):
            consumption_report(rows, price_rows(), POWER, PRICE, START, END, 7200, power=True)

    def test_year_summaries_and_past_hour_counters_are_not_live_meters(self):
        for eid in ("sensor.pump_1_years_consumption", "sensor.pump_10_years_consumption",
                    "sensor.pump_el_energy_used_for_heating_past_hour"):
            self.assertIsNotNone(meter_error({**meter_rows()[0], "entity_id": eid}))

    def test_consumption_cost_and_hourly_coverage(self):
        data = report()
        self.assertEqual(len(data["hours"]), 24)
        self.assertEqual(data["totals"]["kwh"], 24)
        self.assertEqual(data["totals"]["cost"], 48)
        self.assertEqual(data["totals"]["coverage_seconds"], 86400)
        self.assertEqual(data["currency"], "SEK")
        self.assertEqual(data["hours"][0]["kwh"], 1)

    def test_price_shift_flat_price_is_zero(self):
        shift = report()["price_shift"]
        self.assertEqual(shift["hours"], 24)
        self.assertTrue(shift["complete"])
        self.assertAlmostEqual(shift["average_price"], 2)
        self.assertAlmostEqual(shift["difference"], 0)

    def test_price_shift_rewards_use_in_cheap_hours(self):
        # 2 kWh in the 12 cheap hours (1 kr), 0 kWh in the 12 dear hours (3 kr): average 2 kr.
        meter = [row(METER, 100 + 2 * min(i, 12), START + i * 3600, device_class="energy",
                     state_class="total_increasing", unit_of_measurement="kWh") for i in range(25)]
        prices = [row(PRICE, 1 if i < 12 else 3, START + i * 3600, unit_of_measurement="SEK/kWh") for i in range(24)]
        shift = report(meter, prices)["price_shift"]
        self.assertAlmostEqual(shift["kwh"], 24)
        self.assertAlmostEqual(shift["actual_cost"], 24)
        self.assertAlmostEqual(shift["average_cost"], 48)
        self.assertAlmostEqual(shift["difference"], 24)
        self.assertAlmostEqual(shift["actual_price"], 1)
        # The other way round costs more than the average.
        prices = [row(PRICE, 3 if i < 12 else 1, START + i * 3600, unit_of_measurement="SEK/kWh") for i in range(24)]
        self.assertAlmostEqual(report(meter, prices)["price_shift"]["difference"], -24)

    def test_price_shift_only_counts_priced_complete_hours(self):
        prices = price_rows()[:12]
        shift = report(prices=prices)["price_shift"]
        self.assertFalse(shift["complete"])
        self.assertLess(shift["hours"], 24)
        self.assertIsNone(report(prices=[])["price_shift"])

    def test_units_and_negative_prices(self):
        for unit, scale in (("Wh", 1000), ("kWh", 1), ("MWh", 0.001)):
            for price_unit, value in (("SEK/MWh", 2000), ("öre/kWh", 200), ("kr/kWh", 2)):
                with self.subTest(unit=unit, price_unit=price_unit):
                    self.assertAlmostEqual(report(meter_rows(unit, scale), price_rows(price_unit, value))["totals"]["cost"], 48)
        self.assertEqual(report(prices=price_rows(value=-1))["totals"]["cost"], -24)

    def test_quarter_prices_split_one_meter_interval(self):
        prices = [row(PRICE, value, START + i * 900, unit_of_measurement="SEK/kWh")
                  for i, value in enumerate((1, 2, 3, 4))]
        data = report(meter_rows()[:2], prices)
        self.assertEqual(data["hours"][0]["cost"], 2.5)
        self.assertIsNone(data["totals"]["cost"])

    def test_resets_gaps_unavailable_and_invalid_numbers_are_not_zero(self):
        for value in ("unavailable", "unknown", "nan", "-1", "3"):
            meter = meter_rows()
            meter[1]["state"] = value
            data = report(meter)
            self.assertIsNone(data["totals"]["kwh"])
            self.assertIsNone(data["hours"][0]["kwh"])
            self.assertGreater(data["skipped_intervals"], 0)
        data = report(meter_rows()[::3])
        self.assertEqual(data["totals"]["coverage_seconds"], 0)
        self.assertIsNone(data["totals"]["kwh"])
        self.assertIsNone(data["totals"]["cost"])
        data = report(meter_rows()[::2])
        self.assertEqual(data["totals"]["kwh"], 24, "Two-hour reports can be distributed with an explicit assumption")
        meter = meter_rows()
        for item in meter[5:]:
            item["attributes"]["last_reset"] = datetime.fromtimestamp(START + 4 * 3600).isoformat()
        self.assertIsNone(report(meter)["hours"][4]["kwh"], "A reported reset is unsafe even when the counter increased")

    def test_zero_is_valid_but_missing_data_is_not(self):
        meter = meter_rows()
        for item in meter:
            item["state"] = "100"
        self.assertEqual(report(meter)["totals"]["cost"], 0)
        self.assertIsNone(report([])["totals"]["kwh"])
        self.assertIsNone(report(prices=[])["totals"]["cost"])
        self.assertIn("Prishistorik saknas", report(prices=[])["price_error"])

    def test_missing_prices_leave_energy_and_partial_cost_explicit(self):
        data = report(prices=price_rows()[1:])
        self.assertEqual(data["totals"]["kwh"], 24)
        self.assertIsNone(data["totals"]["cost"])
        self.assertEqual(data["totals"]["partial_cost"], 46)
        self.assertEqual(data["totals"]["priced_seconds"], 23 * 3600)
        self.assertIn("Historiska priser saknas", data["price_error"])
        data = report(prices=price_rows()[:1])
        self.assertEqual(data["totals"]["priced_seconds"], 7200, "Never carry a stale price across the day")
        prices = price_rows()
        prices[0]["state"] = "unavailable"
        self.assertIsNone(report(prices=prices)["hours"][0]["cost"])

    def test_bad_price_unit_and_currency_change_disable_cost_not_energy(self):
        for unit in ("W", "", None):
            data = report(prices=price_rows(unit))
            self.assertEqual(data["totals"]["kwh"], 24)
            self.assertIsNone(data["totals"]["cost"])
            self.assertIn("Kostnad kräver", data["price_error"])
        prices = price_rows()
        prices[-1]["attributes"]["unit_of_measurement"] = "EUR/kWh"
        self.assertIn("valuta", report(prices=prices)["price_error"])
        meter = meter_rows()[:2]
        meter[0]["state"] = "0"
        meter[1]["state"] = "1e308"
        with self.assertRaisesRegex(ControlError, "för stora"):
            report(meter)

    def test_meter_selection_rejects_produced_heat_and_period_counters(self):
        meter = meter_rows()[0]
        self.assertIsNone(meter_error(meter))
        for eid in ("sensor.energy_produced_for_heating", "sensor.energy_consumed_current_hour", "sensor.energy_today",
                    "sensor.energy_consumed_current_month"):
            with self.subTest(eid=eid):
                meter["entity_id"] = eid
                self.assertIsNotNone(meter_error(meter))
        meter = meter_rows()[0]
        meter["attributes"]["state_class"] = "measurement"
        self.assertIsNotNone(meter_error(meter))
        meter = meter_rows()
        meter[-1]["attributes"]["unit_of_measurement"] = "W"
        with self.assertRaises(ControlError):
            report(meter)

    def test_window_clipping_and_no_extrapolation_past_last_report(self):
        meter = meter_rows()
        meter.insert(0, row(METER, 99, START - 3600, **meter[0]["attributes"]))
        meter.append(row(METER, 125, END + 3600, **meter[0]["attributes"]))
        self.assertEqual(report(meter)["totals"]["kwh"], 24)
        self.assertIsNone(report(meter_rows()[:-1])["hours"][-1]["kwh"])
        for item in meter:
            item["last_updated"] = "invalid"
        with self.assertRaisesRegex(ControlError, "tidsstämpel"):
            report(meter)

    def test_comparison_uses_full_hours_around_change_not_incomplete_change_hour(self):
        data = report()
        changes = [{"timestamp": START + 3700, "kind": "offset", "status": "confirmed",
                    "previous": 1, "value": 2, "unit": "steg",
                    "contributions": {"room": -1, "price": 0.5}},
                   {"timestamp": START + 4000, "kind": "offset", "status": "failed", "value": 3}]
        comparisons = change_comparisons(data["hours"], changes)
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]["before"]["start"], START)
        self.assertEqual(comparisons[0]["after"]["start"], START + 7200)
        self.assertEqual(comparisons[0]["previous"], 1)
        self.assertEqual(comparisons[0]["contributions"], {"room": -1, "price": 0.5})
        self.assertEqual(comparisons[0]["difference_kwh"], 0)
        self.assertEqual(comparisons[0]["difference_cost"], 0)
        data["hours"][2]["kwh"] = None
        data["hours"][2]["cost"] = None
        partial = change_comparisons(data["hours"], changes)[0]
        self.assertIsNone(partial["difference_kwh"])
        self.assertIsNone(partial["difference_cost"])
        self.assertNotIn("savings", data)
        self.assertNotIn("savings", comparisons[0])

    def test_calendar_period_summaries_and_change_estimates_are_explicit(self):
        data = report()
        segments, _, _ = price_intervals(price_rows(), PRICE, END, 7200)
        periods = energy_periods(data["hours"], segments)
        self.assertEqual(sum(item["kwh"] for item in periods["daily"]), 24)
        self.assertEqual(sum(item["cost"] for item in periods["daily"]), 48)
        self.assertTrue(all(item["price_shift"]["difference"] == 0 for item in periods["daily"]))
        comparisons = [
            {"kind": "offset", "timestamp": START + 3600, "before": {"cost": 4, "kwh": 2},
             "after": {"cost": 3, "kwh": 1}},
            {"kind": "thermostat", "timestamp": START + 7200, "before": {"cost": 3, "kwh": 1},
             "after": {"cost": 5, "kwh": 2}},
            {"kind": "settings", "timestamp": START + 7200, "before": {"cost": 3}, "after": {"cost": 0}},
        ]
        actions = action_periods(comparisons)
        self.assertEqual(actions["observations"], 2)
        self.assertEqual(actions["by_kind"][0]["kind"], "offset")
        self.assertEqual(actions["by_kind"][0]["estimated_savings"], 1)
        self.assertEqual(actions["by_kind"][1]["estimated_savings"], -2)
        self.assertEqual(actions["daily"][0]["estimated_savings"], -1)
        self.assertEqual(len(actions["items"]), 2)
        self.assertEqual(actions["items"][0]["estimated_savings"], 1)
        self.assertEqual(actions["items"][0]["before"]["cost"], 4)

    def test_calendar_period_bounds_use_local_midnight_and_reject_arbitrary_ranges(self):
        now = END + 3600
        start, end = period_bounds(now, 30)
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(end, midnight.timestamp())
        self.assertEqual(start, (midnight - timedelta(days=30)).timestamp())
        for invalid in (True, 1, 14, "30"):
            with self.subTest(invalid=invalid), self.assertRaises(ControlError):
                period_bounds(now, invalid)


COMPRESSOR = "sensor.f370_status_compressor"
HEATER = "sensor.f370_power_internal_add_heat_43084"
HOUSE = "sensor.inverter_load_power"


def compressor_rows(changes=((0, "Operating"), (6, "Off"), (7, "Operating"))):
    return [row(COMPRESSOR, state, START + hours * 3600, device_class="enum") for hours, state in changes]


def heater_rows(changes=((0, 0), (2, 3), (3, 0))):
    return [row(HEATER, kw, START + hours * 3600, device_class="power", unit_of_measurement="kW")
            for hours, kw in changes]


def estimate_report(compressor=None, heater=None):
    from energy import ESTIMATE_ID, estimated_power_rows
    rows = estimated_power_rows(compressor_rows() if compressor is None else compressor,
                                heater_rows() if heater is None else heater, HEATER, END)
    return consumption_report(rows, price_rows(), ESTIMATE_ID, PRICE, START, END, 7200, power=True)


class EstimateTests(unittest.TestCase):
    def test_compressor_aux_and_base_add_up_over_held_states(self):
        # 23 h compressor at 0.5 kW + 1 h aux at 3 kW + 24 h base at 0.05 kW.
        data = estimate_report()
        self.assertAlmostEqual(data["totals"]["kwh"], 23 * 0.5 + 3 + 24 * 0.05)
        hours = data["hours"]
        self.assertAlmostEqual(hours[0]["kwh"], 0.55)
        self.assertAlmostEqual(hours[2]["kwh"], 3.55, msg="Aux heater on top of the compressor")
        self.assertAlmostEqual(hours[6]["kwh"], 0.05, msg="Compressor off: only fans and pump")
        self.assertAlmostEqual(data["totals"]["cost"], data["totals"]["kwh"] * 2)

    def calibration_data(self, compressor_kw=0.47, cycles=8):
        """Compressor off 30 min every 3 h; house 1.0 kW plus the compressor; a kettle at start 3, aux at start 5."""
        changes = []
        for k in range(cycles):
            changes += [(k * 3, "Off"), (k * 3 + 0.5, "Operating")]
        house = []
        for stamp in range(START, START + cycles * 3 * 3600, 60):
            offset = (stamp - START) % (3 * 3600)
            cycle = (stamp - START) // (3 * 3600)
            kw = 1.0 + (compressor_kw if offset >= 1800 else 0) + (0.02 if stamp // 60 % 2 else -0.02)
            if cycle == 3 and 1800 <= offset < 1800 + 1500:
                kw += 2.0
            house.append(row(HOUSE, round(kw, 3), stamp, device_class="power", unit_of_measurement="kW"))
        heater = heater_rows(((0, 0), (15, 3), (15.75, 0)))
        return compressor_rows(changes), heater, house

    def test_calibration_measures_the_compressor_step_and_drops_outliers(self):
        from energy import calibrate_compressor
        compressor, heater, house = self.calibration_data()
        result = calibrate_compressor(compressor, heater, HEATER, house, HOUSE)
        self.assertEqual(result["starts"], 8)
        self.assertEqual(result["samples"], 6, "The kettle start is implausible, the aux start is skipped")
        self.assertAlmostEqual(result["kw"], 0.47, places=2)
        # Watts work the same way.
        watts = [{**r, "state": str(float(r["state"]) * 1000), "attributes": {**r["attributes"], "unit_of_measurement": "W"}}
                 for r in house]
        self.assertAlmostEqual(calibrate_compressor(compressor, heater, HEATER, watts, HOUSE)["kw"], 0.47, places=2)

    def test_calibration_needs_enough_starts(self):
        from energy import calibrate_compressor
        compressor, heater, house = self.calibration_data(cycles=4)
        result = calibrate_compressor(compressor, heater, HEATER, house, HOUSE)
        self.assertIsNone(result["kw"])
        self.assertEqual(result["starts"], 4)
        self.assertIsNone(calibrate_compressor(compressor, heater, HEATER, [], HOUSE))

    def test_calibrated_power_replaces_the_default(self):
        from energy import ESTIMATE_ID, estimated_power_rows
        rows = estimated_power_rows(compressor_rows(), heater_rows(), HEATER, END, compressor_kw=0.4)
        data = consumption_report(rows, price_rows(), ESTIMATE_ID, PRICE, START, END, 7200, power=True)
        self.assertAlmostEqual(data["totals"]["kwh"], 23 * 0.4 + 3 + 24 * 0.05)

    def test_unknown_state_is_a_gap_not_zero(self):
        data = estimate_report(compressor_rows(((0, "Operating"), (5, "unavailable"), (8, "Operating"))))
        self.assertIsNone(data["totals"]["kwh"], "A period with a gap is not complete")
        self.assertIsNone(data["hours"][6]["kwh"])
        self.assertAlmostEqual(data["hours"][9]["kwh"], 0.55)

    def test_off_states_and_discovery_needs_both_sensors(self):
        from energy import compressor_running, discover_estimate
        for state in ("Off", "standby", "Stopped", "Blocked"):
            self.assertFalse(compressor_running(state))
        self.assertTrue(compressor_running("Operating"))
        self.assertIsNone(compressor_running("unavailable"))
        states = [compressor_rows()[0], heater_rows()[0], {**heater_rows()[0], "entity_id": "sensor.other_power_internal_add_heat"}]
        found = discover_estimate(states, {COMPRESSOR, HEATER})
        self.assertEqual([s["entity_id"] for s in found], [COMPRESSOR, HEATER])
        self.assertIsNone(discover_estimate(states, {COMPRESSOR}), "No aux power on the pump: no estimate")
        self.assertIsNone(discover_estimate(states, {HEATER}))


class EnergyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ha = Mock()
        self.ha.states.return_value = [meter_rows()[-1], price_rows()[-1]]
        self.ha.integration_registry.return_value = [{"entity_id": PRICE, "platform": "tibber"}]
        self.ha.request.return_value = [meter_rows(), price_rows()]
        self.insights = Insights(self.ha, self.temp.name)
        self.cfg = config(energy_entity=METER, price_entity=PRICE)

    def test_bounded_serialized_cached_history_request_and_only_selected_entities(self):
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.energy(self.cfg)
            self.insights.energy(self.cfg)
        self.assertEqual(data["totals"]["cost"], 48)
        self.ha.request.assert_called_once()
        path = self.ha.request.call_args.args[0]
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        self.assertEqual(query["filter_entity_id"], [METER + "," + PRICE])
        stamp = datetime.fromisoformat(unquote(parts.path.split("/")[-1])).timestamp()
        self.assertEqual(stamp, START - 3600)
        self.assertNotIn("no_attributes", parts.query)
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_no_selection_or_ineligible_meter_never_loads_history(self):
        with self.assertRaisesRegex(ControlError, "Välj"):
            self.insights.energy({**self.cfg, "energy_entity": ""})
        self.ha.states.return_value[0]["attributes"]["friendly_name"] = "Producerad värme"
        with self.assertRaisesRegex(ControlError, "Producerad"):
            self.insights.energy(self.cfg)
        self.ha.request.assert_not_called()

    def pump_registry(self):
        return [
            {"entity_id": self.cfg["offset_entity"], "device_id": "pump", "platform": "myuplink"},
            {"entity_id": METER, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": PRICE, "device_id": "home", "platform": "tibber"},
        ]

    def test_automatic_meter_is_same_pump_and_never_saved_or_used_for_control(self):
        self.ha.integration_registry.return_value = self.pump_registry()
        cfg = {**self.cfg, "energy_entity": ""}
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.energy(cfg)
        self.assertTrue(data["meter"]["automatic"])
        self.assertEqual(data["meter"]["entity_id"], METER)
        self.assertEqual(data["totals"]["kwh"], 24)
        self.assertEqual(cfg["energy_entity"], "")
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_pump_without_meter_gets_an_estimate_labelled_as_such(self):
        self.ha.integration_registry.return_value = [
            {"entity_id": self.cfg["offset_entity"], "device_id": "pump", "platform": "myuplink"},
            {"entity_id": COMPRESSOR, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": HEATER, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": PRICE, "device_id": "home", "platform": "tibber"},
        ]
        self.ha.states.return_value = [compressor_rows()[-1], heater_rows()[-1], price_rows()[-1]]
        self.ha.request.return_value = [compressor_rows(), heater_rows(), price_rows()]
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.energy({**self.cfg, "energy_entity": ""})
        self.assertEqual(data["method"], "estimate")
        self.assertIn("Uppskattning", data["meter"]["name"])
        self.assertEqual(data["meter"]["estimate"]["compressor_entity"], COMPRESSOR)
        self.assertAlmostEqual(data["totals"]["kwh"], 23 * 0.5 + 3 + 24 * 0.05)
        query = parse_qs(urlsplit(self.ha.request.call_args.args[0]).query)
        self.assertEqual(query["filter_entity_id"], [f"{COMPRESSOR},{HEATER},{PRICE}"])
        self.ha.set_offset.assert_not_called()
        # Without the compressor status the original explanation remains.
        self.ha.states.return_value = [heater_rows()[-1], price_rows()[-1]]
        self.insights = Insights(self.ha, tempfile.mkdtemp(dir=self.temp.name))
        with self.assertRaisesRegex(ControlError, "Ingen entydig ackumulerad elmätare"):
            self.insights.energy({**self.cfg, "energy_entity": ""})

    def test_house_sensor_calibrates_the_estimate_and_failures_fall_back(self):
        compressor, heater, house = EstimateTests.calibration_data(None)
        self.ha.integration_registry.return_value = [
            {"entity_id": self.cfg["offset_entity"], "device_id": "pump", "platform": "myuplink"},
            {"entity_id": COMPRESSOR, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": HEATER, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": PRICE, "device_id": "home", "platform": "tibber"},
        ]
        house_state = row(HOUSE, 1.5, END, device_class="power", unit_of_measurement="kW", friendly_name="Husets last")
        self.ha.states.return_value = [compressor_rows()[-1], heater_rows()[-1], price_rows()[-1], house_state]
        self.ha.request.side_effect = [[compressor_rows(), heater_rows(), price_rows()], [compressor, heater, house]]
        cfg = {**self.cfg, "energy_entity": "", "house_power_entity": HOUSE}
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.energy(cfg)
        estimate = data["meter"]["estimate"]
        self.assertAlmostEqual(estimate["compressor_kw"], 0.47, places=2)
        self.assertEqual(estimate["calibration"]["samples"], 6)
        self.assertEqual(estimate["calibration"]["name"], "Husets last")
        self.assertAlmostEqual(data["totals"]["kwh"], 23 * estimate["compressor_kw"] + 3 + 24 * 0.05)
        calibration_query = parse_qs(urlsplit(self.ha.request.call_args_list[1].args[0]).query)
        self.assertEqual(calibration_query["filter_entity_id"], [f"{COMPRESSOR},{HEATER},{HOUSE}"])
        self.ha.set_offset.assert_not_called()
        # A missing or wrong house sensor keeps the default and says why.
        for states, message in (([compressor_rows()[-1], heater_rows()[-1], price_rows()[-1]], "saknas"),
                                ([compressor_rows()[-1], heater_rows()[-1], price_rows()[-1],
                                  row(HOUSE, 5, END, device_class="energy", unit_of_measurement="kWh")], "W eller kW")):
            with self.subTest(message=message):
                self.insights = Insights(self.ha, tempfile.mkdtemp(dir=self.temp.name))
                self.ha.states.return_value = states
                self.ha.request.side_effect = [[compressor_rows(), heater_rows(), price_rows()]]
                with patch("insights.time.time", return_value=END + 60):
                    data = self.insights.energy(cfg)
                self.assertEqual(data["meter"]["estimate"]["compressor_kw"], 0.5)
                self.assertIn(message, data["meter"]["estimate"]["calibration"]["error"])

    def test_discovery_excludes_other_pumps_production_period_counters_and_ambiguous_names(self):
        registry = self.pump_registry()
        states = [meter_rows()[0]]
        for eid, device, platform in (
            ("sensor.other_energy_consumed", "other", "myuplink"),
            ("sensor.house_energy_consumed", "pump", "tibber"),
            ("sensor.energy_produced_electrical", "pump", "myuplink"),
            ("sensor.energy_consumed_current_hour", "pump", "myuplink"),
            ("sensor.heat_energy", "pump", "myuplink"),
        ):
            states.append({**meter_rows()[0], "entity_id": eid})
            registry.append({"entity_id": eid, "device_id": device, "platform": platform})
        self.assertEqual(discover_meter(states, registry, self.cfg["offset_entity"])["entity_id"], METER)
        states[0]["state"] = "unavailable"
        self.assertEqual(discover_meter(states, registry, self.cfg["offset_entity"])["entity_id"], METER,
                         "Availability must not silently change which meter is used")
        for invalid in ("", "number.other"):
            with self.assertRaisesRegex(ControlError, "pumpanslutning"):
                discover_meter(states, registry, invalid)
        with self.assertRaisesRegex(ControlError, "Ingen entydig"):
            discover_meter(states[1:], registry, self.cfg["offset_entity"])

    def test_multiple_candidates_are_not_summed_even_if_one_is_unavailable(self):
        registry = self.pump_registry()
        extra = "sensor.energy_consumed_for_hot_water"
        registry.append({"entity_id": extra, "device_id": "pump", "platform": "myuplink"})
        self.ha.integration_registry.return_value = registry
        self.ha.states.return_value.append({**meter_rows()[0], "entity_id": extra, "state": "unavailable"})
        with self.assertRaisesRegex(ControlError, "Flera möjliga"):
            self.insights.energy({**self.cfg, "energy_entity": ""})
        self.ha.request.assert_not_called()
        with patch("insights.time.time", return_value=END + 60):
            manual = self.insights.energy(self.cfg)
        self.assertFalse(manual["meter"]["automatic"])
        self.assertEqual(manual["meter"]["entity_id"], METER)

    def test_power_fallback_excludes_year_summaries_and_components(self):
        registry = self.pump_registry()
        states = [power_rows()[0]]
        for eid in (POWER, "sensor.pump_1_years_consumption", "sensor.pump_10_years_consumption",
                    "sensor.pump_el_current_consumption_components_eb100", "sensor.pump_power_internal_add_heat"):
            registry.append({"entity_id": eid, "device_id": "pump", "platform": "myuplink"})
            if eid != POWER:
                template = meter_rows()[0] if "years" in eid else power_rows()[0]
                states.append({**template, "entity_id": eid})
        self.assertEqual(discover_meter(states, registry, self.cfg["offset_entity"])["entity_id"], POWER)
        states[0]["state"] = "unavailable"
        self.assertEqual(discover_meter(states, registry, self.cfg["offset_entity"])["entity_id"], POWER)
        self.assertEqual(discover_meter([*states, meter_rows()[0]], registry, self.cfg["offset_entity"])["entity_id"], METER)
        with self.assertRaisesRegex(ControlError, "Ingen entydig"):
            discover_meter(states[1:], registry, self.cfg["offset_entity"])
        registry[3]["device_id"] = "other"
        with self.assertRaisesRegex(ControlError, "Ingen entydig"):
            discover_meter(states, registry, self.cfg["offset_entity"])

    def test_power_auto_report_loads_only_total_and_price_history_without_writes(self):
        self.ha.integration_registry.return_value = [
            {"entity_id": self.cfg["offset_entity"], "device_id": "pump", "platform": "myuplink"},
            {"entity_id": POWER, "device_id": "pump", "platform": "myuplink"},
            {"entity_id": PRICE, "device_id": "home", "platform": "tibber"},
        ]
        self.ha.states.return_value = [power_rows()[0]]
        self.ha.request.return_value = [power_rows(), price_rows()]
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.energy({**self.cfg, "energy_entity": ""})
        self.assertEqual(data["method"], "power")
        self.assertEqual(data["meter"]["entity_id"], POWER)
        self.assertTrue(data["meter"]["automatic"])
        self.assertAlmostEqual(data["totals"]["kwh"], 48)
        self.assertIn(POWER, self.ha.request.call_args.args[0])
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_automatic_cache_tracks_pump_connection_and_never_falls_back_from_manual_selection(self):
        self.ha.integration_registry.return_value = self.pump_registry()
        with patch("insights.time.time", return_value=END + 60):
            automatic = {**self.cfg, "energy_entity": ""}
            self.insights.energy(automatic)
            with self.assertRaisesRegex(ControlError, "pumpanslutning"):
                self.insights.energy({**automatic, "offset_entity": "number.other"})
            with self.assertRaisesRegex(ControlError, "saknas"):
                self.insights.energy({**self.cfg, "energy_entity": "sensor.missing"})

    def test_long_period_report_uses_completed_local_days(self):
        now = END + 60
        with patch("insights.time.time", return_value=now):
            data = self.insights.energy(self.cfg, 7)
        expected_start, expected_end = period_bounds(now, 7)
        self.assertEqual((data["start"], data["end"]), (expected_start, expected_end))
        self.assertIn("daily", data)
        self.assertIn("monthly", data)
        stamp = datetime.fromisoformat(unquote(self.ha.request.call_args.args[0].split("/history/period/")[1].split("?")[0])).timestamp()
        self.assertEqual(stamp, expected_start - 3600)
        with self.assertRaisesRegex(ControlError, "7, 30 eller 90"):
            self.insights.energy(self.cfg, 14)

    def test_provider_switch_does_not_reuse_tibber_cost(self):
        with patch("insights.time.time", return_value=END + 60):
            self.assertEqual(self.insights.energy(self.cfg)["totals"]["cost"], 48)
            changed = self.insights.energy({**self.cfg, "price_provider": "nordpool"})
        self.assertIsNone(changed["totals"]["cost"])
        self.assertIn("sparade elpriskälla", changed["price_error"])
        self.assertEqual(changed["totals"]["kwh"], 24)
        self.assertEqual(self.ha.request.call_count, 2)

    def test_existing_settings_migrate_without_selecting_meter_or_changing_control(self):
        saved = config(enabled=True)
        del saved["energy_entity"]
        (Path(self.temp.name) / "settings.json").write_text(json.dumps(saved))
        restored = Controller(self.ha, self.temp.name).settings()
        self.assertEqual(restored, {**saved, "energy_entity": ""})
        with self.assertRaises(ControlError):
            validate({**restored, "energy_entity": "number.offset"})


if __name__ == "__main__":
    unittest.main()
