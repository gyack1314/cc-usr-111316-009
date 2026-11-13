"""精确 LP 内核与分支定界的测试。

包括：与完整模式枚举的交叉核对、负电价下同时充放的处理、
超时/预算耗尽时不得把"没找到解"当作无解、以及破同分规则。
"""
import itertools
import unittest
from fractions import Fraction as F

from analysis_data.lp import solve_lp, OPTIMAL, INFEASIBLE, STEP_LIMIT
from analysis_data.model import Model, MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE
from analysis_data.optimizer import (
    solve_cap, enumerate_modes, minimum_emission, max_emission, Budget,
)
from analysis_data.scenario import Scenario, Battery, Period, SolverLimits
from analysis_data.verify import verify

from tests.test_model import period, battery, scenario


class LPTests(unittest.TestCase):
    def test_basic_optimum(self):
        # min x+y, x+y=2, x<=1, y<=2  -> x=1,y=1
        result = solve_lp(
            [F(1), F(1)], [[F(1), F(1)]], [F(2)],
            [[F(1), F(0)], [F(0), F(1)]], [F(1), F(2)],
        )
        self.assertEqual(result.status, OPTIMAL)
        self.assertEqual(result.objective, F(2))
        self.assertEqual(result.vector, [F(1), F(1)])
        certificate = result.certificate
        self.assertTrue(certificate["primal_feasible"])
        self.assertTrue(certificate["dual_feasible"])

    def test_infeasible(self):
        # x+y=1 且 x+y=2 不可能同时成立
        result = solve_lp([F(1), F(1)], [[F(1), F(1)], [F(1), F(1)]], [F(1), F(2)])
        self.assertEqual(result.status, INFEASIBLE)

    def test_unbounded(self):
        # min -x，x 只受 x>=0 约束 -> 无界
        result = solve_lp([F(-1)], [[F(0)]], [F(0)])
        self.assertEqual(result.status, "unbounded")

    def test_negative_rhs(self):
        # -x <= -2 等价于 x >= 2；min x -> x=2
        result = solve_lp([F(1)], [], [], [[F(-1)], [F(-1)]], [F(-2), F(-2)])
        self.assertEqual(result.status, OPTIMAL)
        self.assertEqual(result.vector[0], F(2))


class BranchAndBoundTests(unittest.TestCase):
    def test_matches_full_enumeration(self):
        # 三格，包含负电价、效率损失和容量约束；B&B 与全枚举必须同费用
        periods = [
            period("t1", 0, "1", "1", price=-2, emission_factor=2, charge_limit=2),
            period("t2", 1, "1", "2", price=1, emission_factor=1, discharge_limit=2),
            period("t3", 2, "1", "1", price=3, emission_factor=1),
        ]
        s = scenario(periods, battery(capacity=F(2), charge_efficiency=F(1, 2),
                                      discharge_efficiency=F(1, 2)), caps=("2", "4", "6"))
        model = Model(s)
        for cap in s.emission_caps:
            with self.subTest(cap=cap):
                bb = solve_cap(model, cap)
                enumeration = enumerate_modes(model, cap)
                feasible = [F(e["cost"]) for e in enumeration["entries"] if e["status"] == "optimal"]
                if bb.status == "infeasible":
                    self.assertFalse(feasible)
                else:
                    self.assertEqual(bb.cost, min(feasible))

    def test_negative_price_forces_branching(self):
        # 单格负电价：连续松弛同时充放，整数模式禁止 -> 必然分支
        s = scenario([period("t1", 0, "1", "1", price=-3, charge_limit=2,
                             discharge_limit=2)],
                     battery(capacity=F(2), charge_efficiency=F(1, 2),
                             discharge_efficiency=F(1, 2)), caps=("4",))
        model = Model(s)
        result = solve_cap(model, F(4))
        self.assertEqual(result.status, "feasible")
        self.assertTrue(result.optimality_proven)
        self.assertGreater(result.detail["nodes"], 1)
        self.assertTrue(result.detail["relaxed_simultaneous_charge_discharge"])
        self.assertLessEqual(result.cost, F(0))

    def test_infeasible_is_proven(self):
        s = scenario([period("t1", 0, "1", "1")],
                     battery(initial_soc=F(2)), caps=("0",))
        model = Model(s)
        result = solve_cap(model, F(0))
        self.assertEqual(result.status, "infeasible")
        self.assertTrue(result.optimality_proven)
        self.assertEqual(result.detail["reason"], "连续松弛已证明不可行")

    def test_timeout_is_not_infeasible(self):
        # max_steps=1 迫使预算在第一个节点耗尽；结果必须是 timeout 而不是 infeasible
        s = Scenario(
            scenario_id="timeout", periods=tuple([
                period("t1", 0, "1", "1", price=-2),
                period("t2", 1, "1", "1", price=1),
            ]), battery=battery(charge_efficiency=F(1, 2), discharge_efficiency=F(1, 2)),
            demand_fees={"a": F(0)}, emission_caps=(F(10),),
            solver=SolverLimits(1, F(0), 1),
        )
        result = solve_cap(Model(s), F(10))
        self.assertIn(result.status, ("timeout", "feasible"))
        if result.status == "timeout":
            self.assertFalse(result.optimality_proven)
            if result.schedule is not None:
                self.assertIsNotNone(result.lower_bound)
                self.assertIsNotNone(result.gap)

    def test_cap_below_minimum_emission(self):
        periods = [period("t1", 0, "1", "1", emission_factor=2),
                   period("t2", 1, "1", "1", emission_factor=1)]
        s = scenario(periods, battery(), caps=("2",))
        model = Model(s)
        result = solve_cap(model, F(2))
        self.assertEqual(result.status, "infeasible")
        self.assertTrue(result.optimality_proven)

    def test_objective_minimum_emission(self):
        periods = [period("t1", 0, "1", "1", emission_factor=2),
                   period("t2", 1, "1", "1", emission_factor=1)]
        s = scenario(periods, battery(), caps=("100",))
        model = Model(s)
        result = minimum_emission(model)
        self.assertEqual(result.emission, F(3))
        self.assertTrue(result.optimality_proven)


class TieBreakTests(unittest.TestCase):
    def test_equal_cost_uses_lower_emission(self):
        # 两格同一价格，电池可把购电从高排放格移到低排放格，费用相同
        periods = [period("t1", 0, "1", "1", price=1, emission_factor=5),
                   period("t2", 1, "1", "1", price=1, emission_factor=1)]
        s = scenario(periods, battery(), caps=("6",))
        model = Model(s)
        result = solve_cap(model, F(6))
        self.assertEqual(result.cost, F(2))
        self.assertEqual(result.emission, F(6))

    def test_equal_everything_uses_lexicographic_decisions(self):
        periods = [period("t1", 0, "1", "1", price=0),
                   period("t2", 1, "1", "1", price=0)]
        s = scenario(periods, battery(), caps=("100",))
        model = Model(s)
        first = solve_cap(model, F(100))
        second = solve_cap(model, F(100))
        self.assertEqual(first.schedule.to_dict(), second.schedule.to_dict())


class EnumerationTests(unittest.TestCase):
    def test_all_patterns_reported(self):
        s = scenario([period("t1", 0, "1", "1")], battery(), caps=("100",))
        model = Model(s)
        result = enumerate_modes(model, F(100))
        self.assertEqual(result["modes_evaluated"], 3)
        self.assertIsNotNone(result["best"])

    def test_enumeration_consistency_with_bnb_for_all_patterns(self):
        # 5 格以内直接穷举可行
        periods = [period(f"t{i}", i, "1", "1", price=(-1) ** i * (i + 1),
                          emission_factor=i + 1) for i in range(4)]
        s = scenario(periods, battery(capacity=F(2), charge_efficiency=F(1, 2),
                                      discharge_efficiency=F(1, 2)), caps=("8",))
        model = Model(s)
        bnb = solve_cap(model, F(8))
        enum = enumerate_modes(model, F(8))
        feasible = [F(e["cost"]) for e in enum["entries"] if e["status"] == "optimal"]
        if bnb.status == "infeasible":
            self.assertFalse(feasible)
        else:
            self.assertEqual(bnb.cost, min(feasible))


if __name__ == "__main__":
    unittest.main()
