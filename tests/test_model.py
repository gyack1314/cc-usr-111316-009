"""模型口径测试：时间格非等长、间隙自放电、需量峰值、效率与模式互斥。

每个测试都用一只手算得出答案的合成情景，避免依赖求解器内部实现。
"""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from fractions import Fraction as F
from pathlib import Path

from analysis_data.numeric import text
from analysis_data.scenario import Scenario, Battery, Period, SolverLimits, read_scenario
from analysis_data.model import Model, gap_hours, period_energy, _pow
from analysis_data.optimizer import solve_cap, Budget
from analysis_data.verify import verify


def period(pid, hour, hours, load, pv=0, import_limit=10, charge_limit=2,
           discharge_limit=2, price=1, emission_factor=1, billing="a"):
    return Period(
        id=pid,
        start=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hour),
        hours=F(hours), load=F(load), pv=F(pv), import_limit=F(import_limit),
        charge_limit=F(charge_limit), discharge_limit=F(discharge_limit),
        price=F(price), emission_factor=F(emission_factor), billing_period=billing,
    )


def battery(**changes):
    base = dict(capacity=F(2), initial_soc=F(0), terminal_soc=F(0),
                charge_efficiency=F(1), discharge_efficiency=F(1),
                hourly_retention=F(1), throughput_fee=F(0))
    base.update(changes)
    return Battery(**base)


def scenario(periods, battery_value, fees=None, caps=("100",)):
    return Scenario(
        scenario_id="test", periods=tuple(periods), battery=battery_value,
        demand_fees=fees or {"a": F(0)}, emission_caps=tuple(F(c) for c in caps),
        solver=SolverLimits(20000, F(0), 30),
    )


class TimeGridTests(unittest.TestCase):
    def test_unequal_hours(self):
        hours = [F(2), F(1, 2), F(3, 2)]
        p = period_energy(period("x", 0, "3/2", "2"))
        self.assertEqual(p["load"], F(3))
        self.assertEqual(p["pv"], F(0))
        self.assertEqual(p["hours"], F(3, 2))

    def test_gap_is_unscheduled_downtime(self):
        first = period("a", 0, "1", "1")
        second = period("b", 100, "1", "1")
        self.assertEqual(gap_hours((first, second), 1), F(99))

    def test_compound_retention(self):
        p = period("a", 0, "2", "1")
        b = battery(hourly_retention=F(1, 2))
        s = scenario([p], b, caps=("4",))
        model = Model(s)
        self.assertEqual(model.decay[0]["within"], F(1, 4))
        self.assertEqual(
            model.constraints(None)[0][1][model.var(0, 4)], F(1)
        )

    def test_soc_advance_with_gap(self):
        # 初始 SOC 1，保留系数 1/2。第一格 1h -> 1/2；停机 1h 再 1h -> 1/8。
        # 末态要求 1/4，因此第二格需充 1/8。负荷为 0，使这一算术可手算。
        first = period("a", 0, "1", "0")
        second = period("b", 2, "1", "0")
        s = scenario([first, second],
                     battery(capacity=F(1), initial_soc=F(1), terminal_soc=F(1, 4),
                             hourly_retention=F(1, 2)),
                     fees={"a": F(0)}, caps=("100",))
        model = Model(s)
        result = solve_cap(model, F(100))
        self.assertEqual(result.status, "feasible")
        verify_result = verify(s, result.schedule)
        self.assertTrue(verify_result.ok, verify_result.failures())
        self.assertEqual(result.schedule.periods[0].soc_end, F(1, 2))
        self.assertEqual(result.schedule.periods[1].soc_end, F(1, 4))


class DemandFeeTests(unittest.TestCase):
    def test_peak_is_power_not_energy(self):
        # 一格 2 小时、购电 4 kWh：功率峰值 = 2 kW，需量费 = 2 * 费率
        p = period("a", 0, "2", "2", import_limit=2, price=1, billing="jan")
        s = scenario([p], battery(), fees={"jan": F(3)}, caps=("100",))
        model = Model(s)
        result = solve_cap(model, F(100))
        self.assertEqual(result.schedule.demand_peaks["jan"], F(2))
        self.assertTrue(verify(s, result.schedule).ok)

    def test_peak_split_across_billing_periods(self):
        first = period("a", 0, "1", "1", price=1, billing="jan")
        second = period("b", 1, "1", "1", price=2, billing="feb")
        s = scenario([first, second], battery(), fees={"jan": F(1), "feb": F(1)}, caps=("100",))
        model = Model(s)
        peaks = model.peak_index
        self.assertEqual(model.peak_labels, ["feb", "jan"])
        result = solve_cap(model, F(100))
        self.assertTrue(verify(s, result.schedule).ok)


class EfficiencyTests(unittest.TestCase):
    def test_roundtrip_loss(self):
        # 充电 1 kWh 需要购电 2 kWh（eta_c=1/2），放电 1 kWh 只能取回 1/2
        first = period("a", 0, "1", "0", price=0, charge_limit=4)
        second = period("b", 1, "1", "1", price=4, discharge_limit=2)
        s = scenario([first, second],
                     battery(capacity=F(4), charge_efficiency=F(1, 2),
                             discharge_efficiency=F(1, 2)), caps=("100",))
        model = Model(s)
        # 口径：charge/discharge 是电池侧能量。eta_c=1/2 时充 4 kWh 只增加 2 kWh SOC；
        # eta_d=1/2 时放 1 kWh 会扣掉 2 kWh SOC，但只向负荷交付 1 kWh。
        # 因此为第二格供 1 kWh 需充 4、放 1，且末态回到 0。
        result = solve_cap(model, F(100))
        self.assertTrue(verify(s, result.schedule).ok)
        self.assertEqual(result.schedule.periods[0].charge_energy, F(4))
        self.assertEqual(result.schedule.periods[1].discharge_energy, F(1))
        self.assertEqual(result.schedule.periods[0].soc_end, F(2))
        self.assertEqual(result.schedule.periods[1].soc_end, F(0))
        self.assertEqual(result.schedule.cost(), F(0))


class ModeExclusionTests(unittest.TestCase):
    def test_negative_price_cannot_charge_and_discharge(self):
        cheap = period("a", 0, "1", "1", price=-3, emission_factor=1,
                       charge_limit=2, discharge_limit=2)
        s = scenario([cheap],
                     battery(capacity=F(2), charge_efficiency=F(1, 2),
                             discharge_efficiency=F(1, 2)), caps=("4",))
        model = Model(s)
        result = solve_cap(model, F(4))
        self.assertTrue(verify(s, result.schedule).ok)
        for row in result.schedule.periods:
            self.assertFalse(row.charge_energy > 0 and row.discharge_energy > 0)
        relaxed = result.detail["relaxed_simultaneous_charge_discharge"]
        self.assertTrue(relaxed)
        upper = result.detail["relaxed_cost"]
        self.assertLess(F(upper), result.cost)

    def test_mode_label_matches_energy(self):
        p = period("a", 0, "1", "1")
        s = scenario([p], battery(), caps=("100",))
        result = solve_cap(Model(s), F(100))
        self.assertEqual(result.schedule.periods[0].mode, "idle")


class PvAndExportTests(unittest.TestCase):
    def test_pv_curtailment_and_no_export(self):
        p = period("a", 0, "1", "1", pv=5)
        s = scenario([p], battery(), caps=("100",))
        model = Model(s)
        result = solve_cap(model, F(100))
        self.assertLessEqual(result.schedule.periods[0].pv_used_energy, F(5))
        self.assertGreaterEqual(result.schedule.periods[0].import_energy, F(0))
        self.assertTrue(verify(s, result.schedule).ok)


class TerminalBoundaryTests(unittest.TestCase):
    def test_terminal_must_be_reached(self):
        p = period("a", 0, "1", "1")
        s = scenario([p], battery(terminal_soc=F(2)), caps=("100",))
        result = solve_cap(Model(s), F(100))
        self.assertTrue(verify(s, result.schedule).ok)
        self.assertEqual(result.schedule.periods[-1].soc_end, F(2))

    def test_terminal_beyond_capacity_is_rejected(self):
        from analysis_data.scenario import load_battery, ShapeError
        with self.assertRaises(ShapeError):
            load_battery({"capacity": "1", "initial_soc": "0", "terminal_soc": "2",
                          "charge_efficiency": "1", "discharge_efficiency": "1",
                          "hourly_retention": "1", "throughput_fee": "0"})


class DeadPeriodTests(unittest.TestCase):
    def test_import_limit_binds(self):
        p = period("a", 0, "1", "3", import_limit=1)
        s = scenario([p], battery(initial_soc=F(2)), caps=("100",))
        model = Model(s)
        result = solve_cap(model, F(100))
        # 需从电池放 2 kWh 补足负荷 3，购电被压到 1
        self.assertTrue(verify(s, result.schedule).ok)
        self.assertEqual(result.schedule.periods[0].import_energy, F(1))
        self.assertEqual(result.schedule.periods[0].discharge_energy, F(2))


if __name__ == "__main__":
    unittest.main()
