"""独立重放校验、前沿组织层、报告与缓存的测试。"""
import json
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction as F
from pathlib import Path

from analysis_data.cache import ResultCache, scenario_cache_key, read_asset_bytes
from analysis_data.frontier import run_frontier, split_comparison
from analysis_data.model import Model, Schedule, SchedulePeriod
from analysis_data.optimizer import solve_cap
from analysis_data.report import compare_baseline, impossible_targets
from analysis_data.scenario import read_scenario
from analysis_data.verify import verify

from tests.test_model import period, battery, scenario


def _copy(schedule, **changes):
    return replace(schedule, **changes)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.periods = [
            period("t1", 0, "1", "1", price=1, emission_factor=3),
            period("t2", 1, "1", "1", price=2, emission_factor=1, billing="b"),
        ]
        self.scenario = scenario(self.periods,
                                 battery(capacity=F(2), charge_efficiency=F(1, 1),
                                         discharge_efficiency=F(1, 1)),
                                 fees={"a": F(2), "b": F(1)}, caps=("100",))
        self.model = Model(self.scenario)
        self.result = solve_cap(self.model, F(100))
        self.valid = verify(self.scenario, self.result.schedule)
        self.assertTrue(self.valid.ok, self.valid.failures())

    def _tamper(self, index, field, value):
        rows = list(self.result.schedule.periods)
        rows[index] = replace(rows[index], **{field: value})
        return _copy(self.result.schedule, periods=tuple(rows))

    def test_balance_violation_detected(self):
        bad = self._tamper(0, "import_energy", F(9))
        result = verify(self.scenario, bad)
        self.assertFalse(result.ok)
        self.assertTrue(any(c.name == "电力平衡" for c in result.failures()))

    def test_mode_violation_detected(self):
        rows = list(self.result.schedule.periods)
        rows[0] = replace(rows[0], charge_energy=F(1), discharge_energy=F(1))
        bad = _copy(self.result.schedule, periods=tuple(rows))
        result = verify(self.scenario, bad)
        self.assertFalse(result.ok)
        self.assertTrue(any(c.name == "充放互斥" for c in result.failures()))

    def test_soc_violation_detected(self):
        bad = self._tamper(0, "soc_end", F(7))
        result = verify(self.scenario, bad)
        self.assertFalse(result.ok)
        self.assertTrue(any(c.name == "状态推进" for c in result.failures()))

    def test_terminal_violation_detected(self):
        rows = list(self.result.schedule.periods)
        rows[-1] = replace(rows[-1], soc_end=F(1))
        bad = _copy(self.result.schedule, periods=tuple(rows))
        result = verify(self.scenario, bad)
        self.assertFalse(result.ok)
        self.assertTrue(any(c.name == "末状态" for c in result.failures()))

    def test_demand_peak_violation_detected(self):
        bad = _copy(self.result.schedule, demand_peaks={"a": F(99), "b": F(0)})
        result = verify(self.scenario, bad)
        self.assertFalse(result.ok)
        self.assertTrue(any("需量峰值" in c.name for c in result.failures()))

    def test_cost_and_emission_recomputed(self):
        cost = sum(p.price * sp.import_energy for p, sp in
                   zip(self.scenario.periods, self.result.schedule.periods))
        for label, fee in self.scenario.demand_fees.items():
            cost += fee * self.result.schedule.demand_peaks[label]
        self.assertEqual(self.result.schedule.cost(), cost)

    def test_wrong_period_id_detected(self):
        rows = list(self.result.schedule.periods)
        rows[0] = replace(rows[0], id="nope")
        bad = _copy(self.result.schedule, periods=tuple(rows))
        self.assertFalse(verify(self.scenario, bad).ok)

    def test_export_violation_detected(self):
        rows = list(self.result.schedule.periods)
        rows[0] = replace(rows[0], import_energy=F(99))
        bad = _copy(self.result.schedule, periods=tuple(rows))
        result = verify(self.scenario, bad)
        self.assertFalse(result.ok)


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.scenario = read_scenario("demo_data/small-calendar.json")
        self.frontier = run_frontier(self.scenario)

    def test_every_point_verified(self):
        for point in self.frontier.points:
            if point.result.schedule is None:
                continue
            self.assertIsNotNone(point.verification)
            self.assertTrue(point.verification.ok, point.verification.failures())

    def test_caps_are_isolated(self):
        caps = [p.cap for p in self.frontier.points]
        self.assertEqual(caps, list(self.scenario.emission_caps))

    def test_nondominated_flags(self):
        feasible = [p for p in self.frontier.points if p.result.status == "feasible"]
        self.assertTrue(feasible)
        for a in feasible:
            for b in feasible:
                if a is b:
                    continue
                if (b.result.cost <= a.result.cost and b.result.emission <= a.result.emission
                        and (b.result.cost < a.result.cost or b.result.emission < a.result.emission)):
                    self.assertTrue(a.dominated)

    def test_infeasible_caps_are_not_reported_feasible(self):
        for point in self.frontier.points:
            if point.result.status == "infeasible":
                self.assertIsNone(point.result.schedule)
                self.assertTrue(point.result.optimality_proven)

    def test_minimum_emission_reported(self):
        minimum = self.frontier.min_emission
        self.assertIsNotNone(minimum)
        self.assertTrue(minimum.optimality_proven)
        for point in self.frontier.points:
            if point.result.status == "feasible":
                self.assertGreaterEqual(point.result.emission, minimum.emission)

    def test_unreachable_caps_identified(self):
        impossible = impossible_targets(self.frontier)
        for cap in impossible["proven_infeasible_caps"]:
            self.assertIn(cap, [str(p.cap) for p in self.frontier.points])

    def test_baseline_is_reference_only(self):
        comparison = compare_baseline(self.frontier)
        self.assertTrue(comparison)
        for row in comparison:
            if row["status"] != "feasible":
                continue
            self.assertIn("cost_delta", row)
            self.assertIn("emission_delta", row)

    def test_frontier_serializes(self):
        payload = self.frontier.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("allowed_mode_invariant", text)
        self.assertIn("points", text)


class SplitComparisonTests(unittest.TestCase):
    def setUp(self):
        self.whole = Model(read_scenario("demo_data/split-one.json"))
        self.split = Model(read_scenario("demo_data/split-two.json"))

    def test_grouped_modes_match_whole(self):
        for cap in self.whole.scenario.emission_caps:
            result = split_comparison(self.whole, self.split, cap)
            self.assertTrue(result["comparable"])
            self.assertEqual(result["whole"]["cost"], result["split_bound_to_one_mode"]["cost"])

    def test_independent_modes_can_differ(self):
        result = split_comparison(self.whole, self.split, F(3))
        self.assertNotEqual(result["whole"]["cost"], result["split_independent_modes"]["cost"])

    def test_rejects_non_constant_split(self):
        from analysis_data.model import Model, Schedule
        other = read_scenario("demo_data/negative-price-simultaneous.json")
        with self.assertRaises(ValueError):
            split_comparison(self.whole, Model(other), F(2))


class CacheTests(unittest.TestCase):
    def test_key_binds_boundaries(self):
        raw, raw_periods = read_asset_bytes("data/normal.json")
        scenario_obj = read_scenario("data/normal.json")
        key = scenario_cache_key(scenario_obj, raw, raw_periods, "frontier")
        changed = replace(scenario_obj,
                          periods=(replace(scenario_obj.periods[0], start=scenario_obj.periods[0].start),
                                   ) + scenario_obj.periods[1:])
        self.assertEqual(key, scenario_cache_key(changed, raw, raw_periods, "frontier"))
        different_caps = replace(scenario_obj, emission_caps=(F(9),))
        self.assertNotEqual(key, scenario_cache_key(different_caps, raw, raw_periods, "frontier"))

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory)
            self.assertIsNone(cache.get("abc"))
            cache.put("abc", {"value": 1})
            self.assertEqual(cache.get("abc"), {"value": 1})


class CliTests(unittest.TestCase):
    def test_inspect(self):
        from analysis_data.cli import main
        with tempfile.TemporaryDirectory() as directory:
            import contextlib, io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["inspect", "data/normal.json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["summary"]["periods"], 2)

    def test_verify_round_trip(self):
        from analysis_data.cli import main
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO() if False else None
            schedule = solve_cap(Model(read_scenario("data/normal.json")), F(4)).schedule
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(schedule.to_dict()), encoding="utf8")
            import contextlib, io
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = main(["verify", "data/normal.json", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stream.getvalue())["ok"])

    def test_verify_rejects_tampered(self):
        from analysis_data.cli import main
        with tempfile.TemporaryDirectory() as directory:
            schedule = solve_cap(Model(read_scenario("data/normal.json")), F(4)).schedule
            payload = schedule.to_dict()
            payload["periods"][0]["import"] = "99"
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf8")
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["verify", "data/normal.json", str(path)])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
