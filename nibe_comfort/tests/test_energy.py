import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError, Controller, validate
from energy import change_comparisons, consumption_report, discover_meter, meter_error
from insights import Insights
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


def report(meter=None, prices=None):
    return consumption_report(meter_rows() if meter is None else meter,
                              price_rows() if prices is None else prices, METER, PRICE, START, END, 7200)


class EnergyTests(unittest.TestCase):
    def test_consumption_cost_and_hourly_coverage(self):
        data = report()
        self.assertEqual(len(data["hours"]), 24)
        self.assertEqual(data["totals"]["kwh"], 24)
        self.assertEqual(data["totals"]["cost"], 48)
        self.assertEqual(data["totals"]["coverage_seconds"], 86400)
        self.assertEqual(data["currency"], "SEK")
        self.assertEqual(data["hours"][0]["kwh"], 1)

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
        changes = [{"timestamp": START + 3700, "kind": "offset", "status": "confirmed", "value": 2},
                   {"timestamp": START + 4000, "kind": "offset", "status": "failed", "value": 3}]
        comparisons = change_comparisons(data["hours"], changes)
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]["before"]["start"], START)
        self.assertEqual(comparisons[0]["after"]["start"], START + 7200)
        self.assertEqual(comparisons[0]["difference_kwh"], 0)
        self.assertEqual(comparisons[0]["difference_cost"], 0)
        data["hours"][2]["kwh"] = None
        data["hours"][2]["cost"] = None
        partial = change_comparisons(data["hours"], changes)[0]
        self.assertIsNone(partial["difference_kwh"])
        self.assertIsNone(partial["difference_cost"])
        self.assertNotIn("savings", data)
        self.assertNotIn("savings", comparisons[0])


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

    def test_automatic_cache_tracks_pump_connection_and_never_falls_back_from_manual_selection(self):
        self.ha.integration_registry.return_value = self.pump_registry()
        with patch("insights.time.time", return_value=END + 60):
            automatic = {**self.cfg, "energy_entity": ""}
            self.insights.energy(automatic)
            with self.assertRaisesRegex(ControlError, "pumpanslutning"):
                self.insights.energy({**automatic, "offset_entity": "number.other"})
            with self.assertRaisesRegex(ControlError, "saknas"):
                self.insights.energy({**self.cfg, "energy_entity": "sensor.missing"})

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
