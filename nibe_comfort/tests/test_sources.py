import copy
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import DEFAULTS, ControlError, validate
from energy import hourly_energy, pump_sources, split_sources
from insights import Insights

START = 1_800_000_000 - 1_800_000_000 % 3600


def row(entity_id, stamp, value, unit="kWh"):
    return {"entity_id": entity_id, "state": str(value), "attributes": {"unit_of_measurement": unit},
            "last_changed": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            "last_updated": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


def config(**overrides):
    return validate({**copy.deepcopy(DEFAULTS), "room_sensors": ["sensor.living"], "offset_entity": "number.offset", **overrides})


class HourlyEnergyTests(unittest.TestCase):
    def test_counter_steps_are_spread_over_quiet_hours(self):
        rows = [row("sensor.batt_in", START - 600, 10.0), row("sensor.batt_in", START + 3 * 3600, 10.3)]
        hours = hourly_energy(rows, "sensor.batt_in", START, START + 4 * 3600)
        self.assertTrue(all(value is not None for value in hours))
        self.assertAlmostEqual(sum(hours), 0.3 * (3 * 3600) / (3 * 3600 + 600), places=6)
        self.assertEqual(hours[3], 0)

    def test_unchanged_counter_is_zero_until_end(self):
        rows = [row("sensor.sol", START - 7200, 5.0)]
        self.assertEqual(hourly_energy(rows, "sensor.sol", START, START + 7200), [0, 0])

    def test_produced_energy_names_are_allowed_for_house_flows(self):
        rows = [row("sensor.total_yield", START, 1.0), row("sensor.total_yield", START + 3600, 2.0)]
        self.assertEqual(hourly_energy(rows, "sensor.total_yield", START, START + 3600), [1.0])

    def test_unavailable_breaks_coverage(self):
        rows = [row("sensor.nat", START, 1.0), row("sensor.nat", START + 1800, "unavailable"), row("sensor.nat", START + 3600, 2.0)]
        self.assertEqual(hourly_energy(rows, "sensor.nat", START, START + 3600), [None])

    def test_counter_reset_is_not_negative_use(self):
        rows = [row("sensor.nat", START, 9.0), row("sensor.nat", START + 3600, 1.0)]
        self.assertEqual(hourly_energy(rows, "sensor.nat", START, START + 3600), [None])

    def test_power_sensor_in_watts(self):
        rows = [row("sensor.p", START + i * 600, 1200, "W") for i in range(7)]
        self.assertAlmostEqual(hourly_energy(rows, "sensor.p", START, START + 3600, power=True)[0], 1.2)


class SplitTests(unittest.TestCase):
    def split(self, **values):
        flows = {key: [value] for key, value in values.items()}
        return split_sources(flows, START, START + 3600)[0]

    def test_solar_battery_and_grid(self):
        hour = self.split(grid_import=1.0, grid_export=0.5, solar=3.0, battery_discharge=0.5, battery_charge=1.0)
        self.assertAlmostEqual(hour["house_kwh"], 3.0)
        self.assertAlmostEqual(hour["grid_kwh"], 1.0)
        self.assertAlmostEqual(hour["battery_kwh"], 0.5)
        self.assertAlmostEqual(hour["solar_kwh"], 1.5)
        self.assertAlmostEqual(hour["grid_share"] + hour["solar_share"] + hour["battery_share"], 1.0)

    def test_grid_only_house(self):
        hour = self.split(grid_import=2.0)
        self.assertEqual((hour["grid_share"], hour["solar_kwh"], hour["battery_kwh"]), (1.0, 0.0, 0.0))

    def test_battery_charged_from_grid_is_not_house_use(self):
        hour = self.split(grid_import=3.0, battery_charge=2.0)
        self.assertAlmostEqual(hour["house_kwh"], 1.0)
        self.assertEqual(hour["grid_share"], 1.0)

    def test_missing_hour(self):
        self.assertIsNone(self.split(grid_import=None, solar=1.0)["house_kwh"])

    def test_no_use(self):
        hour = self.split(grid_import=0.0, solar=1.0, grid_export=1.0)
        self.assertEqual(hour["house_kwh"], 0.0)
        self.assertIsNone(hour["grid_share"])


class PumpSourceTests(unittest.TestCase):
    def test_pump_gets_house_mix_and_cost(self):
        house = split_sources({"grid_import": [1.0], "solar": [3.0]}, START, START + 3600)
        pump = [{"start": START, "end": START + 3600, "kwh": 2.0, "cost": 4.0}]
        rows, totals = pump_sources(pump, house)
        self.assertAlmostEqual(rows[0]["grid_kwh"], 0.5)
        self.assertAlmostEqual(rows[0]["solar_kwh"], 1.5)
        self.assertAlmostEqual(rows[0]["grid_cost"], 1.0)
        self.assertAlmostEqual(rows[0]["avoided_cost"], 3.0)
        self.assertTrue(totals["complete"])

    def test_unknown_pump_hour_is_not_attributed(self):
        house = split_sources({"grid_import": [1.0]}, START, START + 3600)
        rows, totals = pump_sources([{"start": START, "end": START + 3600, "kwh": None, "cost": None}], house)
        self.assertIsNone(rows[0]["grid_kwh"])
        self.assertFalse(totals["complete"])


class SettingsTests(unittest.TestCase):
    def test_grid_import_required_with_other_flows(self):
        with self.assertRaisesRegex(ControlError, "köpt el"):
            config(solar_entity="sensor.sol")
        with self.assertRaises(ControlError):
            config(grid_import_entity="sensor.a", solar_entity="sensor.a")
        with self.assertRaises(ControlError):
            config(grid_import_entity="number.a")
        self.assertEqual(config(grid_import_entity="sensor.a", solar_entity="sensor.b")["solar_entity"], "sensor.b")


class InsightsSourcesTests(unittest.TestCase):
    def test_sources_without_pump_meter(self):
        now = time.time()
        end = int(now // 3600) * 3600
        ha = Mock()
        ha.states.return_value = [
            {"entity_id": "sensor.nat", "state": "100", "attributes": {"unit_of_measurement": "kWh"}},
            {"entity_id": "sensor.sol", "state": "50", "attributes": {"unit_of_measurement": "kWh"}},
            {"entity_id": "number.offset", "state": "0", "attributes": {"min": -10, "max": 10, "step": 1}},
        ]
        history = [[row("sensor.nat", end - 90000 + i * 3600, 100 + i) for i in range(26)],
                   [row("sensor.sol", end - 90000 + i * 3600, 50 + i) for i in range(26)]]
        ha.request.side_effect = lambda path, *a, **k: history if path.startswith("/history/") else []
        ha.integration_registry.return_value = []
        with tempfile.TemporaryDirectory() as path:
            insights = Insights(ha, path)
            result = insights.sources(config(grid_import_entity="sensor.nat", solar_entity="sensor.sol"))
        self.assertTrue(result["house_complete"])
        self.assertAlmostEqual(result["house_totals"]["house_kwh"], 48, places=3)
        self.assertAlmostEqual(result["house_totals"]["solar_kwh"], 24, places=3)
        self.assertIsNone(result["pump"])
        self.assertTrue(result["pump_error"])

    def test_sources_needs_grid(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaises(ControlError):
                Insights(Mock(), path).sources(config())


if __name__ == "__main__":
    unittest.main()
