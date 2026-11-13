"""演示资料与“小日历穷举”报告的测试。"""
import json
import unittest
from fractions import Fraction as F

from analysis_data.demo import build, build_and_report, hull_report, EXPLANATIONS
from analysis_data.frontier import split_comparison
from analysis_data.model import Model
from analysis_data.optimizer import enumerate_modes, solve_cap
from analysis_data.scenario import read_scenario


class DemoBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = build()
        cls.report = build_and_report()

    def test_all_named(self):
        for name in ("small-calendar", "negative-price-simultaneous", "cross-billing",
                     "roundtrip-loss", "terminal-soc-one", "terminal-soc-zero",
                     "nonconvex-midpoint", "combined", "split-one", "split-two"):
            self.assertIn(name, self.paths)
            self.assertIn(name, self.report)

    def test_each_scenario_readable(self):
        for name, path in self.paths.items():
            with self.subTest(name=name):
                scenario = read_scenario(path)
                self.assertTrue(scenario.emission_caps)
                self.assertEqual(len(scenario.periods) >= 1, True)

    def test_explanations_present(self):
        for name in self.paths:
            self.assertTrue(EXPLANATIONS.get(name))

    def test_small_calendar_enumeration_exhaustive(self):
        scenario = read_scenario(self.paths["small-calendar"])
        model = Model(scenario)
        self.assertLessEqual(3 ** len(scenario.periods), 3000)
        result = enumerate_modes(model, scenario.emission_caps[-1])
        self.assertEqual(result["modes_evaluated"], 3 ** len(scenario.periods))


class DemoNarrativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = build()
        cls.report = build_and_report()

    def test_negative_price_gap_shown(self):
        report = self.report["negative-price-simultaneous"]
        gaps = {cap: entry["relaxation_gap"] for cap, entry in report["per_cap"].items()}
        self.assertIn("3/2", gaps.values())
        for cap, entry in report["per_cap"].items():
            if entry["relaxation_gap"] is not None and entry["relaxation_gap"] != "0":
                self.assertTrue(entry["relaxed_simultaneous_charge_discharge"])

    def test_cross_billing_shift(self):
        scenario = read_scenario(self.paths["cross-billing"])
        result = solve_cap(Model(scenario), scenario.emission_caps[-1])
        self.assertTrue(result.optimality_proven)
        self.assertEqual(result.status, "feasible")
        # 月末低价充电，次月放电；跨期后次月需量峰值应低于无储能参照
        peaks = result.schedule.demand_peaks
        self.assertLessEqual(peaks["feb"], F(2))

    def test_terminal_soc_changes_minimum_emission(self):
        one = self.report["terminal-soc-one"]["minimum_emission"]
        zero = self.report["terminal-soc-zero"]["minimum_emission"]
        self.assertNotEqual(one, zero)
        self.assertEqual(F(one), F(6))
        self.assertEqual(F(zero), F(5))

    def test_roundtrip_loss_scales_throughput(self):
        report = self.report["roundtrip-loss"]
        entries = [report["per_cap"][cap] for cap in ("6", "8", "10")]
        emissions = sorted(F(e["emission"]) for e in entries)
        self.assertEqual(len(set(emissions)), 3)

    def test_nonconvex_midpoint_documented(self):
        report = self.report["nonconvex-midpoint"]
        self.assertIn("非凸", report["explanation"])
        # 两端方案可行，混合调度不可行（由 verify 测试覆盖）；这里核对登记上限的数值
        self.assertEqual(F(report["per_cap"]["2"]["cost"]), F(10))
        self.assertEqual(F(report["per_cap"]["4"]["cost"]), F(4))

    def test_split_equivalence(self):
        one = Model(read_scenario(self.paths["split-one"]))
        two = Model(read_scenario(self.paths["split-two"]))
        for cap in one.scenario.emission_caps:
            result = split_comparison(one, two, cap)
            self.assertTrue(result["comparable"])
            self.assertEqual(result["whole"]["cost"],
                             result["split_bound_to_one_mode"]["cost"])

    def test_report_json_serializable(self):
        json.dumps(self.report, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
