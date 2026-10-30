import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auxiliary import context_at, context_history, heater_entities, heater_report
from controller import ControlError
from insights import Insights, pump_device_entities
from test_controller import config
from test_energy import START, END, PRICE, price_rows, row


POWER = "sensor.pump_power_internal_add_heat"
OFFSET = "number.pump_heating_offset_climate_system_1"
MODE = "sensor.pump_status"


def heater_rows(values=(0, 3, 6, 0, 3, 0, 0), unit="kW"):
    return [row(POWER, value, START + i * 300, device_class="power",
                state_class="measurement", unit_of_measurement=unit) for i, value in enumerate(values)]


def registry():
    return [
        {"entity_id": OFFSET, "platform": "myuplink", "device_id": "pump"},
        {"entity_id": POWER, "platform": "myuplink", "device_id": "pump"},
        {"entity_id": MODE, "platform": "myuplink", "device_id": "pump"},
        {"entity_id": PRICE, "platform": "tibber", "device_id": "house"},
    ]


def discover(states, reg=None, offset=OFFSET):
    return heater_entities(states, pump_device_entities(registry() if reg is None else reg, offset))


def report(rows=None, prices=None, contexts=None):
    return heater_report(heater_rows() if rows is None else rows,
                         price_rows() if prices is None else prices, POWER, PRICE,
                         contexts or {}, START, END, 7200)


class HeaterReportTests(unittest.TestCase):
    def test_runs_merge_steps_and_measure_starts_runtime_energy_and_cost(self):
        data = report()
        self.assertEqual(len(data["runs"]), 2)
        self.assertEqual(data["observed_starts"], 2)
        self.assertEqual(data["runtime_seconds"], 900)
        self.assertEqual(data["totals"]["coverage_seconds"], 1800)
        self.assertEqual(data["totals"]["measured_kwh"], 1)
        self.assertEqual(data["totals"]["partial_cost"], 2)
        self.assertIsNone(data["totals"]["kwh"], "Not a complete day")
        first, second = data["runs"]
        self.assertEqual(first["seconds"], 600)
        self.assertEqual(first["max_kw"], 6)
        self.assertEqual(first["kwh"], 0.75)
        self.assertEqual(first["cost"], 1.5)
        self.assertEqual(second["cost"], 0.5)
        self.assertTrue(all(r["start_observed"] and r["end_observed"] for r in data["runs"]))

    def test_watts_convert_to_kwh_and_negative_prices_are_allowed(self):
        data = report(heater_rows((0, 3000, 0, 0), "W"), price_rows(value=-2))
        self.assertEqual(data["runs"][0]["kwh"], 0.25)
        self.assertEqual(data["runs"][0]["cost"], -0.5)

    def test_gaps_do_not_invent_stops_starts_or_join_runs(self):
        rows = heater_rows((3, 3, "unavailable", 3, 3))
        data = report(rows)
        self.assertEqual(len(data["runs"]), 2)
        self.assertEqual(data["observed_starts"], 0)
        self.assertEqual(data["runtime_seconds"], 600)
        self.assertFalse(any(r["start_observed"] or r["end_observed"] for r in data["runs"]))
        rows = heater_rows((3, 3))
        rows[-1]["last_updated"] = row(POWER, 3, START + 901)["last_updated"]
        self.assertEqual(report(rows)["runtime_seconds"], 0)

    def test_window_boundaries_and_last_sample_are_not_extrapolated(self):
        data = report(heater_rows((3, 0, 0)))
        self.assertFalse(data["runs"][0]["start_observed"])
        self.assertTrue(data["runs"][0]["end_observed"])
        self.assertEqual(data["runtime_seconds"], 300)
        self.assertEqual(report(heater_rows((0, 3)))["runs"], [])
        self.assertEqual(report([])["totals"]["coverage_seconds"], 0)

    def test_cost_uses_historical_price_segments_and_marks_partial_pricing(self):
        prices = [row(PRICE, 1, START, unit_of_measurement="SEK/kWh"),
                  row(PRICE, 3, START + 600, unit_of_measurement="SEK/kWh")]
        first = report(prices=prices)["runs"][0]
        self.assertEqual(first["cost"], 1.75)
        prices[1]["state"] = "unavailable"
        data = report(prices=prices)
        first = data["runs"][0]
        self.assertIsNone(first["cost"])
        self.assertEqual(first["partial_cost"], 0.25)
        self.assertEqual(first["priced_seconds"], 300)
        self.assertIn("Historiska priser saknas", data["price_error"])
        missing = report(prices=[])["runs"][0]
        self.assertIsNone(missing["cost"])
        self.assertEqual(missing["priced_seconds"], 0)

    def test_context_is_observation_at_start_without_future_or_stale_inference(self):
        points = context_history([row(MODE, "Heating", START), row(MODE, "Hot water", START + 300),
                                  row(MODE, "unavailable", START + 900)])
        self.assertIsNone(context_at(points, START - 1))
        self.assertEqual(context_at(points, START + 300)["value"], "Hot water")
        self.assertEqual(context_at(points, START + 120)["value"], "Heating")
        self.assertIsNone(context_at(points, START + 1000))
        self.assertIsNone(context_at(points[:1], START + 901))
        data = report(contexts={"operation": [row(MODE, "Hot water", START + 299)]})
        self.assertEqual(data["runs"][0]["context"]["operation"]["value"], "Hot water")
        self.assertIsNone(data["runs"][1]["context"]["operation"], "Too old to explain a later run")

    def test_discovery_only_uses_same_pump_power_not_total_or_energy(self):
        states = heater_rows()[:1] + [row(MODE, "Heating", START)]
        power, contexts = discover(states)
        self.assertEqual(power["entity_id"], POWER)
        self.assertEqual(contexts["operation"]["entity_id"], MODE)
        for platform, device in (("tibber", "pump"), ("myuplink", "other")):
            reg = registry()
            reg[1].update(platform=platform, device_id=device)
            with self.assertRaisesRegex(ControlError, "Ingen entydig"):
                discover(states, reg)
        with self.assertRaisesRegex(ControlError, "myUplink"):
            discover(states, offset="number.other")
        with self.assertRaisesRegex(ControlError, "W eller kW"):
            discover(heater_rows(unit="kWh")[:1])
        duplicate = {**states[0], "entity_id": "sensor.other_power_internal_add_heat"}
        reg = registry() + [{"entity_id": duplicate["entity_id"], "platform": "myuplink", "device_id": "pump"}]
        with self.assertRaisesRegex(ControlError, "Ingen entydig"):
            discover([*states, duplicate], reg)


class HeaterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.ha = Mock()
        self.ha.states.return_value = heater_rows()[-1:] + [row(MODE, "Heating", START)]
        self.ha.integration_registry.return_value = registry()
        self.ha.request.return_value = [heater_rows(), price_rows(), [row(MODE, "Hot water", START + 300)]]
        self.insights = Insights(self.ha)
        self.cfg = config(offset_entity=OFFSET, price_entity=PRICE)

    def test_report_is_cached_bounded_filtered_and_read_only(self):
        self.ha.states.return_value[0].update(row(POWER, 0, END + 60, device_class="power",
                                                unit_of_measurement="kW", state_class="measurement"))
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.auxiliary(self.cfg)
            self.insights.auxiliary(self.cfg)
        self.assertFalse(data["current"]["active"])
        self.assertEqual(data["current"]["kw"], 0)
        self.assertEqual(data["meter"]["entity_id"], POWER)
        self.assertEqual(data["runtime_seconds"], 900)
        self.ha.request.assert_called_once()
        query = parse_qs(urlsplit(self.ha.request.call_args.args[0]).query)
        self.assertEqual(set(query["filter_entity_id"][0].split(",")), {POWER, MODE, PRICE})
        self.assertEqual(query["significant_changes_only"], ["0"])
        self.assertNotIn("no_attributes", query)
        self.ha.set_offset.assert_not_called()
        self.ha.set_thermostat.assert_not_called()

    def test_stale_current_is_unknown_not_off_but_history_survives(self):
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.auxiliary(self.cfg)
        self.assertIsNone(data["current"]["active"])
        self.assertIsNotNone(data["current"]["error"])
        self.assertEqual(len(data["runs"]), 2)

    def test_unchanged_power_is_current_when_same_pump_is_still_reporting(self):
        self.ha.states.return_value[-1] = row(MODE, "Heating", END + 60)
        with patch("insights.time.time", return_value=END + 60):
            data = self.insights.auxiliary(self.cfg)
        self.assertFalse(data["current"]["active"])
        self.assertEqual(data["current"]["kw"], 0)
        self.assertIsNone(data["current"]["error"])

    def test_provider_and_pump_switch_do_not_reuse_reports_or_wrong_prices(self):
        with patch("insights.time.time", return_value=END + 60):
            self.insights.auxiliary(self.cfg)
            data = self.insights.auxiliary({**self.cfg, "price_provider": "nordpool"})
            self.assertIn("sparade elpriskälla", data["price_error"])
            self.assertTrue(all(r["cost"] is None for r in data["runs"]))
            query = parse_qs(urlsplit(self.ha.request.call_args.args[0]).query)
            self.assertNotIn(PRICE, query["filter_entity_id"][0])
            with self.assertRaises(ControlError):
                self.insights.auxiliary({**self.cfg, "offset_entity": "number.other"})


if __name__ == "__main__":
    unittest.main()
