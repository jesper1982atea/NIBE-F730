import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from test_controller import config, entity, fixtures
from controller import ControlError, Controller, HomeAssistant, evaluate
from insights import Insights, discover_prices, nordpool_prices, planned_offsets, price_end


NOW = datetime(2026, 10, 26, 10, tzinfo=timezone.utc).timestamp()
PRICE = "sensor.nord_pool_se3_aktuellt_pris"
MIDNIGHT = datetime(2026, 10, 26, tzinfo=timezone.utc)


def intervals(start=MIDNIGHT, count=96, seconds=900, key="value", factor=1):
    return [{"start": (start + timedelta(seconds=i * seconds)).isoformat(),
             "end": (start + timedelta(seconds=(i + 1) * seconds)).isoformat(),
             key: factor * (3 if 68 <= i < 76 else 1)}
            for i in range(count)]


class NordPoolDiscoveryTests(unittest.TestCase):
    def test_richards_sensor_is_found_without_tibber_or_registry_entry(self):
        result = discover_prices([entity(PRICE, 0.3, unit_of_measurement="SEK/kWh")], [])
        self.assertEqual(result["price_entity"], PRICE)
        self.assertIn("Nord Pool", result["price_message"])

    def test_hacs_price_sensor_is_found_but_other_price_statistics_are_not(self):
        price = entity("sensor.nordpool_kwh_se3_sek", 1, unit_of_measurement="SEK/kWh", raw_today=[])
        average = entity("sensor.nord_pool_se3_daily_average", 1, unit_of_measurement="SEK/kWh")
        enegic = entity("sensor.enegic_power", 1000, unit_of_measurement="W")
        self.assertEqual(discover_prices([price, average, enegic], [])["price_entity"], price["entity_id"])
        self.assertEqual(discover_prices([average, enegic], [])["price_entity"], "")

    def test_multiple_areas_or_mixed_tibber_and_nordpool_are_not_guessed(self):
        states = [entity(PRICE, 1, unit_of_measurement="SEK/kWh"),
                  entity("sensor.nord_pool_se4_aktuellt_pris", "unavailable", unit_of_measurement="SEK/kWh")]
        self.assertEqual(discover_prices(states, [])["price_entity"], "")
        states[1] = entity("sensor.electricity_price_home", 1, unit_of_measurement="SEK/kWh")
        registry = [{"entity_id": states[1]["entity_id"], "platform": "tibber"}]
        self.assertEqual(discover_prices(states, registry)["price_entity"], "")

    def test_integration_registry_requests_nordpool_and_config_entry_mapping(self):
        ha = HomeAssistant("test")
        ha.request = Mock(return_value=[])
        ha.integration_registry()
        template = ha.request.call_args.args[1]["template"]
        self.assertIn("'nordpool'", template)
        self.assertIn("config_entry_id(eid)", template)


class PriceFixture:
    def setUp(self):
        self.clock = patch("insights.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.ha = Mock()
        self.ha._temperature_unit = "°C"
        self.ha.integration_registry.return_value = []
        self.cfg = config(price_entity=PRICE, preheat_enabled=True)
        self.ha.states.return_value = fixtures(NOW) + [entity(
            PRICE, 1, NOW, unit_of_measurement="SEK/kWh",
            raw_today=intervals(), raw_tomorrow=intervals(MIDNIGHT + timedelta(days=1)),
            tomorrow_valid=True)]
        self.insights = Insights(self.ha)
        self.insights.house_model = Mock(return_value={"model": None})


class NordPoolPricesTests(PriceFixture, unittest.TestCase):
    def test_hacs_schedule_and_preheating_without_tibber(self):
        prices = self.insights.prices(self.cfg)
        self.assertEqual(len(prices), 192)
        self.assertEqual(prices[0][1], 1)
        schedule = self.insights.preheat(self.cfg)
        self.assertTrue(any(w["kind"] == "preheat" for w in schedule["windows"]))
        self.assertTrue(any(w["kind"] == "coast" for w in schedule["windows"]))
        self.ha.request.assert_not_called()
        self.assertEqual(self.insights.prices(self.cfg), prices)

    def test_selected_sensor_units_are_retained_including_ore_mwh_and_negative_prices(self):
        for unit in ("SEK/kWh", "öre/kWh", "SEK/MWh", "EUR/kWh"):
            with self.subTest(unit=unit):
                state = self.ha.states.return_value[-1]
                state["attributes"]["unit_of_measurement"] = unit
                state["attributes"]["raw_today"][0]["value"] = -25.5
                self.assertEqual(Insights(self.ha).prices(self.cfg)[0][1], -25.5)
        cfg = config(price_entity=PRICE, cheap_boost=1, expensive_reduction=1,
                     price_low=50, price_high=150)
        states = fixtures(NOW) + [entity(PRICE, 40, NOW, unit_of_measurement="öre/kWh")]
        result = evaluate(cfg, states, NOW)
        self.assertEqual(result["price"], 40)
        self.assertEqual(result["price_unit"], "öre/kWh")
        self.assertEqual(result["contributions"]["price"], 1)

    def test_unpublished_tomorrow_is_not_invented_or_read_from_tibber(self):
        attrs = self.ha.states.return_value[-1]["attributes"]
        attrs["tomorrow_valid"] = False
        attrs["raw_tomorrow"] = "not published"
        self.assertEqual(len(self.insights.prices(self.cfg)), 96)
        self.ha.request.assert_not_called()

    def test_current_only_sensor_supports_immediate_control_but_reports_missing_forecast(self):
        self.ha.states.return_value[-1]["attributes"] = {"unit_of_measurement": "SEK/kWh"}
        self.ha.integration_registry.return_value = [{"entity_id": "sensor.tibber", "platform": "tibber"}]
        with self.assertRaisesRegex(ControlError, "saknar kommande priser"):
            self.insights.preheat(self.cfg)
        self.ha.request.assert_not_called()
        self.insights.house_model.assert_not_called()

    def test_missing_selection_does_not_use_tibber_implicitly(self):
        self.ha.integration_registry.return_value = [{"entity_id": "sensor.tibber", "platform": "tibber"}]
        with self.assertRaisesRegex(ControlError, "Välj en elprisgivare"):
            self.insights.prices({**self.cfg, "price_entity": ""})
        self.ha.request.assert_not_called()

    def test_unavailable_and_stale_source_disable_preheat_even_with_cached_schedule(self):
        self.insights.preheat(self.cfg)
        for state in ("unavailable", "unknown", "abc", "nan", "inf"):
            self.ha.states.return_value[-1]["state"] = state
            with self.assertRaises(ControlError):
                self.insights.preheat(self.cfg)
        self.ha.states.return_value[-1] = entity(PRICE, 1, NOW - 100000, unit_of_measurement="SEK/kWh")
        with self.assertRaisesRegex(ControlError, "gammalt"):
            self.insights.preheat(self.cfg)

    def test_changing_source_invalidates_price_and_preheat_caches(self):
        first = self.insights.preheat(self.cfg)
        other = "sensor.nord_pool_se4_aktuellt_pris"
        other_state = copy.deepcopy(self.ha.states.return_value[-1])
        other_state["entity_id"] = other
        for row in other_state["attributes"]["raw_today"] + other_state["attributes"]["raw_tomorrow"]:
            row["value"] = 1
        self.ha.states.return_value.append(other_state)
        changed = {**self.cfg, "price_entity": other}
        second = self.insights.preheat(changed)
        self.assertTrue(first["windows"])
        self.assertEqual(second["windows"], [])
        self.assertEqual(self.insights.house_model.call_count, 2)

    def test_malformed_hacs_attributes_and_unit_raise_explicit_errors(self):
        for updates in ({"raw_today": {}}, {"raw_tomorrow": None}, {"unit_of_measurement": "W"}):
            with self.subTest(updates=updates):
                original = copy.deepcopy(self.ha.states.return_value[-1]["attributes"])
                self.ha.states.return_value[-1]["attributes"].update(updates)
                with self.assertRaises(ControlError):
                    Insights(self.ha).prices(self.cfg)
                self.ha.states.return_value[-1]["attributes"] = original

    def test_plan_uses_selected_nordpool_prices_and_units(self):
        self.cfg["outdoor_entity"] = ""
        self.ha.request.return_value = []
        status = evaluate(self.cfg, self.ha.states.return_value, NOW)
        plan = self.insights.plan(self.cfg, status)
        self.assertIsNone(plan["prices"]["error"])
        self.assertEqual(plan["prices"]["unit"], "SEK/kWh")
        self.assertTrue(plan["prices"]["points"])
        self.assertTrue(plan["offset"]["desired"])
        self.assertEqual(plan["prices"]["end"], (MIDNIGHT + timedelta(days=2)).timestamp())

    def test_controller_applies_nordpool_preheat_and_reverts_when_source_is_unavailable(self):
        now = (MIDNIGHT + timedelta(hours=15)).timestamp()
        self.ha.states.return_value = fixtures(now) + [
            {**self.ha.states.return_value[-1],
             "last_updated": datetime.fromtimestamp(now, timezone.utc).isoformat()}]
        with patch("insights.time.time", return_value=now), tempfile.TemporaryDirectory() as folder:
            controller = Controller(self.ha, folder)
            controller.save({**self.cfg, "enabled": True})
            controller.preheat_provider = self.insights.preheat
            controller.tick()
            self.assertEqual(controller.snapshot()["target_shift"], 0.5)
            self.ha.set_offset.assert_called_once_with("number.offset", 1.0)
            self.ha.states.return_value[-1]["state"] = "unavailable"
            controller.tick()
            self.assertEqual(controller.snapshot()["target_shift"], 0)
            self.assertIn("otillgänglig", controller.snapshot()["preheat"]["note"])
        self.ha.set_offset.assert_called_once_with("number.offset", 1.0)
        self.ha.set_thermostat.assert_not_called()


class OfficialNordPoolTests(PriceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ha.states.return_value[-1]["attributes"] = {"unit_of_measurement": "SEK/kWh"}
        self.availability = "binary_sensor.nord_pool_se3_morgondagens_priser"
        self.ha.states.return_value.append(entity(self.availability, "on", NOW))
        self.ha.integration_registry.return_value = [
            {"entity_id": PRICE, "platform": "nordpool", "device_id": "se3", "config_entry_id": "nordpool-entry"},
            {"entity_id": self.availability, "platform": "nordpool", "device_id": "se3"},
            {"entity_id": "sensor.tibber", "platform": "tibber", "device_id": "tibber"},
        ]

        def request(path, data=None):
            if path == "/template":
                return "2026-10-26"
            self.assertEqual(path, "/services/nordpool/get_prices_for_date?return_response")
            self.assertEqual(data["config_entry"], "nordpool-entry")
            self.assertEqual(data["areas"], ["SE3"])
            self.assertEqual(data["currency"], "SEK")
            day = datetime.fromisoformat(data["date"]).replace(tzinfo=timezone.utc)
            return {"service_response": {"SE3": intervals(day, key="price", factor=1000)}}

        self.ha.request.side_effect = request

    def test_official_service_converts_mwh_to_selected_kwh(self):
        prices = self.insights.prices(self.cfg)
        self.assertEqual(len(prices), 192)
        self.assertEqual(prices[0][1], 1)
        dates = [c.args[1]["date"] for c in self.ha.request.call_args_list if "/services/" in c.args[0]]
        self.assertEqual(dates, ["2026-10-26", "2026-10-27"])
        self.assertTrue(self.insights.preheat(self.cfg)["windows"])

    def test_service_does_not_convert_mwh_sensor_twice(self):
        self.ha.states.return_value[-2]["attributes"]["unit_of_measurement"] = "SEK/MWh"
        self.assertEqual(self.insights.prices(self.cfg)[0][1], 1000)

    def test_tomorrow_not_published_only_requests_today(self):
        self.ha.states.return_value[-1]["state"] = "off"
        self.assertEqual(len(self.insights.prices(self.cfg)), 96)
        self.assertEqual(self.ha.request.call_count, 2)

    def test_other_area_availability_cannot_trigger_tomorrow_request(self):
        self.ha.integration_registry.return_value[1]["device_id"] = "se4"
        self.assertEqual(len(self.insights.prices(self.cfg)), 96)
        self.assertEqual(self.ha.request.call_count, 2)

    def test_date_comes_from_home_assistant_not_addon_timezone(self):
        self.ha.request.side_effect = [
            "2026-10-27",
            {"service_response": {"SE3": intervals(MIDNIGHT + timedelta(days=1), key="price")}},
            {"service_response": {"SE3": intervals(MIDNIGHT + timedelta(days=2), key="price")}},
        ]
        prices = self.insights.prices(self.cfg)
        self.assertEqual(prices[0][0], (MIDNIGHT + timedelta(days=1)).timestamp())
        self.assertEqual(self.ha.request.call_args_list[1].args[1]["date"], "2026-10-27")

    def test_missing_entry_area_or_currency_requires_configuration_not_guessing(self):
        self.ha.integration_registry.return_value[0].pop("config_entry_id")
        with self.assertRaisesRegex(ControlError, "identifiera"):
            self.insights.prices(self.cfg)
        self.ha.request.assert_not_called()

    def test_unknown_area_currency_and_local_date_are_explicit_errors(self):
        original = copy.deepcopy(self.ha.states.return_value[-2])
        for changes in ({"entity_id": "sensor.nord_pool_current_price"},
                        {"attributes": {"unit_of_measurement": "öre/kWh"}}):
            with self.subTest(changes=changes):
                changed = {**original, **changes}
                self.ha.states.return_value[-2] = changed
                self.ha.integration_registry.return_value[0]["entity_id"] = changed["entity_id"]
                cfg = {**self.cfg, "price_entity": changed["entity_id"]}
                with self.assertRaisesRegex(ControlError, "identifiera"):
                    self.insights.prices(cfg)
        self.ha.states.return_value[-2] = original
        self.ha.integration_registry.return_value[0]["entity_id"] = PRICE
        self.ha.request.side_effect = ["invalid-date"]
        with self.assertRaisesRegex(ControlError, "lokalt datum"):
            self.insights.prices(self.cfg)

    def test_service_error_is_visible_and_not_replaced_with_tibber(self):
        self.ha.request.side_effect = ["2026-10-26", ControlError("HTTP 503")]
        with self.assertRaisesRegex(ControlError, "HTTP 503"):
            self.insights.prices(self.cfg)
        self.assertEqual(self.ha.request.call_count, 2)

    def test_malformed_official_response_is_an_explicit_error(self):
        self.ha.request.side_effect = ["2026-10-26", {"service_response": {"SE4": []}}]
        with self.assertRaisesRegex(ControlError, "saknar priser"):
            self.insights.prices(self.cfg)


class PriceIntervalsTests(unittest.TestCase):
    def test_mixed_hour_and_quarter_prices_are_normalized_without_inventing_prices(self):
        rows = intervals(count=1, seconds=3600) + intervals(MIDNIGHT + timedelta(hours=1), count=2)
        prices = nordpool_prices(rows)
        self.assertEqual(len(prices), 6)
        self.assertTrue(all(b[0] - a[0] == 900 for a, b in zip(prices, prices[1:])))
        self.assertEqual(price_end(prices), (MIDNIGHT + timedelta(minutes=90)).timestamp())

    def test_price_influence_stops_at_last_known_period(self):
        cfg = config(price_entity=PRICE, expensive_reduction=1, price_high=2)
        status = evaluate(cfg, fixtures(NOW) + [entity(PRICE, 3, NOW)], NOW)
        prices = [[NOW, 3], [NOW + 900, 3]]
        result = planned_offsets(cfg, status, [], prices, NOW, NOW + 7200, True)
        self.assertGreater(result["desired"][-1][1], result["desired"][0][1])
        self.assertEqual(result["desired"][-1][0], NOW + 1800)
        self.assertTrue(any("Efter sista prisperioden" in note for note in result["notes"]))

    def test_hourly_dst_fallback_and_quarter_hour_prices(self):
        start = datetime(2026, 10, 24, 22, tzinfo=timezone.utc)
        for seconds, count in ((3600, 25), (900, 100)):
            self.assertEqual(len(nordpool_prices(intervals(start, count, seconds))), count)

    def test_malformed_gap_overlap_naive_and_nonfinite_prices_are_rejected(self):
        good = intervals(count=3)
        bad_lists = [None, [None], good * 101]
        for value in (None, True, float("nan"), float("inf")):
            rows = copy.deepcopy(good)
            rows[0]["value"] = value
            bad_lists.append(rows)
        rows = copy.deepcopy(good)
        rows[0]["start"] = "2026-10-26T00:00:00"
        bad_lists.extend([rows, [good[0], good[2]], [good[0], good[0]], [{"value": 1}]])
        for rows in bad_lists:
            with self.subTest(rows=str(rows)[:80]), self.assertRaises(ControlError):
                nordpool_prices(rows)


if __name__ == "__main__":
    unittest.main()
