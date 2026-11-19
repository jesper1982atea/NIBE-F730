import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fireplace_savings import AFTER_SECONDS, HOUR, MIN_BASELINE_HOURS, baseline, fireplace_savings

START = 1_793_000_000 - 1_793_000_000 % HOUR


def house(days=14, fires=(), saving=1.0, noise=0.3, seed=3, price=2.0):
    """Pump uses 2 - 0.2 * outdoor kWh per hour; during a fire and six hours after it ``saving`` kWh less."""
    rng = random.Random(seed)
    hours, outdoor = [], {}
    for i in range(days * 24):
        start = START + i * HOUR
        out = 3 + 4 * ((i % 24) / 24 - 0.5) + (i // 24 % 5) - 2
        kwh = 2 - 0.2 * out + rng.uniform(-noise, noise)
        if any(f[0] - HOUR <= start <= f[1] + AFTER_SECONDS for f in fires):
            kwh -= saving
        outdoor[start] = out
        hours.append({"start": start, "end": start + HOUR, "kwh": max(0.0, kwh), "cost": max(0.0, kwh) * price})
    return hours, outdoor


class FireplaceSavingsTests(unittest.TestCase):
    def test_recovers_the_saving_of_a_long_fire(self):
        fire = (START + 5 * 86400 + 8 * HOUR, START + 5 * 86400 + 20 * HOUR)
        hours, outdoor = house(fires=[fire])
        result = fireplace_savings(hours, outdoor, [{"start": fire[0], "end": fire[1]}], START + 14 * 86400)
        row = result["episodes"][0]
        window = 1 + 12 + 6 + 1  # hour before, fire, six hours after, inclusive end hour
        self.assertEqual(row["window_hours"], window)
        self.assertTrue(row["complete"])
        self.assertAlmostEqual(row["saved_kwh"], window * 1.0, delta=3)
        self.assertTrue(row["clear"])
        self.assertAlmostEqual(row["saved_cost"], row["saved_kwh"] * 2.0, delta=0.5)
        self.assertEqual(result["totals"]["fires"], 1)
        self.assertAlmostEqual(result["model"]["slope"], -0.2, delta=0.03)

    def test_no_saving_is_shown_as_within_the_uncertainty(self):
        fire = (START + 3 * 86400, START + 3 * 86400 + 2 * HOUR)
        hours, outdoor = house(fires=[fire], saving=0.0, noise=0.8)
        row = fireplace_savings(hours, outdoor, [{"start": fire[0], "end": fire[1]}], START + 14 * 86400)["episodes"][0]
        self.assertFalse(row["clear"])
        self.assertLess(abs(row["saved_kwh"]), row["margin_kwh"])

    def test_ongoing_and_recent_fires_wait_until_the_house_has_cooled(self):
        hours, outdoor = house()
        now = START + 14 * 86400
        episodes = [{"start": now - 3 * HOUR, "end": None}, {"start": now - 5 * HOUR, "end": now - 2 * HOUR}]
        result = fireplace_savings(hours, outdoor, episodes, now)
        self.assertEqual(result["episodes"], [])
        self.assertEqual(result["ongoing"], 2)

    def test_fire_hours_are_left_out_of_the_baseline(self):
        fires = [(START + d * 86400 + 8 * HOUR, START + d * 86400 + 20 * HOUR) for d in (2, 6, 10)]
        hours, outdoor = house(fires=fires, saving=1.5)
        result = fireplace_savings(hours, outdoor, [{"start": s, "end": e} for s, e in fires], START + 14 * 86400)
        self.assertAlmostEqual(result["model"]["intercept"], 2.0, delta=0.15, msg="The baseline is not pulled down by fires")
        self.assertEqual(result["totals"]["fires"], 3)

    def test_missing_hours_are_reported_and_too_little_baseline_gives_no_model(self):
        fire = (START + 5 * 86400, START + 5 * 86400 + 4 * HOUR)
        hours, outdoor = house(fires=[fire])
        for hour in hours:
            if fire[0] <= hour["start"] <= fire[0] + 2 * HOUR:
                hour["kwh"] = None
        row = fireplace_savings(hours, outdoor, [{"start": fire[0], "end": fire[1]}], START + 14 * 86400)["episodes"][0]
        self.assertFalse(row["complete"])
        self.assertEqual(row["window_hours"] - row["covered_hours"], 3)
        short_hours, short_outdoor = house(days=2)
        self.assertIsNone(fireplace_savings(short_hours, short_outdoor, [], START + 3 * 86400)["model"])
        self.assertIsNone(baseline([(0.0, 1.0)] * (MIN_BASELINE_HOURS - 1)))

    def test_constant_outdoor_temperature_uses_the_mean(self):
        model = baseline([(2.0, 1.0), (2.0, 3.0)] * MIN_BASELINE_HOURS)
        self.assertEqual(model["slope"], 0.0)
        self.assertAlmostEqual(model["intercept"], 2.0)


if __name__ == "__main__":
    unittest.main()
