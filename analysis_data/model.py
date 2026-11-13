"""储能调度的费用-排放前沿模型（离线、精确有理数）。

模型口径（与 docs/data-contract.md 一致）
---------------------------------------
时间格 i 的功率（kW）乘以声明时长 ``hours`` 得到能量（kWh）。变量为
每格购电能量 ``import``、充电能量 ``charge``、放电能量 ``discharge``、
光伏使用能量 ``pv_used``，以及格末状态 ``soc``。

* 平衡：``import + pv_used + discharge = load*hours + charge``，逐格成立。
* 光伏：``0 <= pv_used <= pv*hours``；购电非负，禁止外送。
* 状态：格末 ``z_i = (z_{i-1} * r^{gap_i}) * r^{hours_i}
  + eta_c*charge - discharge/eta_d``，其中 ``gap_i`` 是上一格结束到本格
  开始的停机小时数（``i=0`` 时用初始 SOC）。复合保留系数是
  ``hourly_retention^{hours}``。
* 边界：``0 <= z_i <= capacity``，首末状态固定。
* 费用：购电费 + ``throughput_fee*(charge+discharge)`` + 各计费周期
  ``需量费率 * 该周期最大购电功率``。
* 排放：``sum(emission_factor_i * import_i)``，不使用平均因子。
* 每格最多充电或放电一种模式。这是唯一的整数决策；本模块对
  ``{闲, 充, 放}`` 做完整枚举（小日历）或精确分支定界（大日历），
  绝不因电价通常为正而删除该条件。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Iterable, Sequence

from .lp import solve_lp, OPTIMAL, INFEASIBLE, UNBOUNDED, STEP_LIMIT
from .numeric import text
from .scenario import Scenario, Period

# 每个时间格的连续变量：购电、充电、放电、光伏使用、格末 SOC
IMPORT, CHARGE, DISCHARGE, PV_USED, SOC = range(5)

MODE_IDLE = 0
MODE_CHARGE = 1
MODE_DISCHARGE = 2
MODE_NAMES = {MODE_IDLE: "idle", MODE_CHARGE: "charge", MODE_DISCHARGE: "discharge"}


def period_energy(period: Period) -> dict[str, Fraction]:
    h = period.hours
    return {
        "load": period.load * h,
        "pv": period.pv * h,
        "import_limit": period.import_limit * h,
        "charge_limit": period.charge_limit * h,
        "discharge_limit": period.discharge_limit * h,
        "hours": h,
    }


def gap_hours(periods: Sequence[Period], index: int) -> Fraction:
    """上一格结束到本格开始之间的小时数；首格为 0。

    相邻时刻的差值精确到秒；间隙不是无损瞬间跳转，自放电按这些小
    时数推进。
    """
    if index == 0:
        return Fraction(0)
    previous_end = periods[index - 1].start + _hours_delta(periods[index - 1].hours)
    delta = periods[index].start - previous_end
    seconds = delta.days * 86400 + delta.seconds
    if delta.microseconds or seconds < 0:
        raise ValueError("相邻时间格重叠或边界不在整秒上")
    return Fraction(seconds, 3600)


def _hours_delta(hours: Fraction):
    from datetime import timedelta
    if (hours * 3600).denominator != 1:
        raise ValueError("时间格时长必须能表示为整秒")
    return timedelta(seconds=int(hours * 3600))


def _exact_root(value: int, k: int) -> int | None:
    """非负整数的精确 k 次方根；不存在时返回 None。"""
    if value < 0:
        return None
    if value in (0, 1):
        return value
    low, high = 1, 1
    while high ** k <= value:
        high *= 2
    while low <= high:
        mid = (low + high) // 2
        if mid ** k == value:
            return mid
        if mid ** k < value:
            low = mid + 1
        else:
            high = mid - 1
    return None


def _pow(base: Fraction, exponent: Fraction) -> Fraction:
    """``base^exponent``，仅在结果为精确有理数时接受。

    保留系数的复合语义是 ``hourly_retention^hours``。若指数为分数而幂
    不落在有理数上，精确 LP 无法表示该常数，此处明确报错，而不是用
    浮点近似悄悄改变模型。资料中的保留系数与时长均为整数或可开方值
    时不受影响。
    """
    if exponent.denominator == 1:
        return base ** exponent.numerator
    root = exponent.denominator
    numerator = _exact_root(base.numerator, root)
    denominator = _exact_root(base.denominator, root)
    if numerator is None or denominator is None:
        raise ValueError(f"保留系数 {base} 的 {root} 次方根不是精确有理数")
    return Fraction(numerator, denominator) ** exponent.numerator


def decay_factors(scenario: Scenario) -> list[dict[str, Fraction]]:
    """每格的保留系数：格内 ``r^hours``，格前停机间隙 ``r^gap``。"""
    b = scenario.battery
    factors = []
    for i, p in enumerate(scenario.periods):
        within = _pow(b.hourly_retention, p.hours)
        before = _pow(b.hourly_retention, gap_hours(scenario.periods, i))
        factors.append({"within": within, "before": before})
    return factors


class Model:
    """一个冻结情景的线性模型构造器（不含模式整数决策）。"""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.periods = scenario.periods
        self.n = len(self.periods)
        self.energy = [period_energy(p) for p in self.periods]
        self.decay = decay_factors(scenario)
        self.peak_labels = sorted(scenario.demand_fees)
        self.peak_index = {label: 5 * self.n + k for k, label in enumerate(self.peak_labels)}
        self.nv = 5 * self.n + len(self.peak_labels)
        self._base_eq: list[list[Fraction]] = []
        self._base_rhs: list[Fraction] = []
        self._base_le: list[list[Fraction]] = []
        self._base_rhs_le: list[Fraction] = []
        self._build_base()

    # ------------------------------------------------------------ 构造
    def var(self, period: int, kind: int) -> int:
        return 5 * period + kind

    def zero(self) -> list[Fraction]:
        return [Fraction(0)] * self.nv

    def _build_base(self) -> None:
        b = self.scenario.battery
        for i, p in enumerate(self.periods):
            e = self.energy[i]
            # 电力平衡
            row = self.zero()
            row[self.var(i, IMPORT)] = Fraction(1)
            row[self.var(i, PV_USED)] = Fraction(1)
            row[self.var(i, DISCHARGE)] = Fraction(1)
            row[self.var(i, CHARGE)] = Fraction(-1)
            self._base_eq.append(row)
            self._base_rhs.append(e["load"])

            # 状态推进：z_i - A_i z_{i-1} - eta_c h charge + h/eta_d discharge
            #            = (i==0 ? initial * A_0 : 0)
            row = self.zero()
            factor = self.decay[i]["within"] * self.decay[i]["before"]
            row[self.var(i, SOC)] = Fraction(1)
            if i == 0:
                constant = b.initial_soc * factor
            else:
                row[self.var(i - 1, SOC)] = -factor
                constant = Fraction(0)
            row[self.var(i, CHARGE)] = -b.charge_efficiency * e["hours"]
            row[self.var(i, DISCHARGE)] = e["hours"] / b.discharge_efficiency
            self._base_eq.append(row)
            self._base_rhs.append(constant)

            # 上限（功率上限乘以小时 -> 能量上限）
            def add_le(index: int, bound: Fraction) -> None:
                row = self.zero()
                row[index] = Fraction(1)
                self._base_le.append(row)
                self._base_rhs_le.append(bound)

            add_le(self.var(i, IMPORT), e["import_limit"])
            add_le(self.var(i, PV_USED), e["pv"])
            add_le(self.var(i, CHARGE), e["charge_limit"])
            add_le(self.var(i, DISCHARGE), e["discharge_limit"])
            add_le(self.var(i, SOC), b.capacity)

            # 需量峰值：import_i - hours_i * peak_label <= 0
            for label in self.peak_labels:
                if p.billing_period != label:
                    continue
                row = self.zero()
                row[self.var(i, IMPORT)] = Fraction(1)
                row[self.peak_index[label]] = -p.hours
                self._base_le.append(row)
                self._base_rhs_le.append(Fraction(0))

        # 末状态固定
        row = self.zero()
        row[self.var(self.n - 1, SOC)] = Fraction(1)
        self._base_eq.append(row)
        self._base_rhs.append(b.terminal_soc)

    # ------------------------------------------------------------ 目标
    def cost_vector(self, throughput: bool = True) -> list[Fraction]:
        c = self.zero()
        fee = self.scenario.battery.throughput_fee
        for i, p in enumerate(self.periods):
            c[self.var(i, IMPORT)] = p.price
            if throughput and fee:
                c[self.var(i, CHARGE)] = fee
                c[self.var(i, DISCHARGE)] = fee
        for label in self.peak_labels:
            c[self.peak_index[label]] = self.scenario.demand_fees[label]
        return c

    def emission_vector(self) -> list[Fraction]:
        c = self.zero()
        for i, p in enumerate(self.periods):
            c[self.var(i, IMPORT)] = p.emission_factor
        return c

    def throughput_vector(self) -> list[Fraction]:
        c = self.zero()
        for i in range(self.n):
            c[self.var(i, CHARGE)] = Fraction(1)
            c[self.var(i, DISCHARGE)] = Fraction(1)
        return c

    # ------------------------------------------------------------ 约束集合
    def constraints(
        self,
        cap: Fraction | None,
        modes: Sequence[int] | None = None,
        extra_eq: Iterable[tuple[list[Fraction], Fraction]] = (),
        extra_le: Iterable[tuple[list[Fraction], Fraction]] = (),
    ) -> tuple[list[list[Fraction]], list[Fraction], list[list[Fraction]], list[Fraction]]:
        eq = [row[:] for row in self._base_eq]
        rhs_eq = list(self._base_rhs)
        le = [row[:] for row in self._base_le]
        rhs_le = list(self._base_rhs_le)
        if cap is not None:
            row = self.emission_vector()
            le.append(row)
            rhs_le.append(cap)
        if modes is not None:
            for i, mode in enumerate(modes):
                if mode == MODE_CHARGE:
                    row = self.zero(); row[self.var(i, DISCHARGE)] = Fraction(1)
                    le.append(row); rhs_le.append(Fraction(0))
                elif mode == MODE_DISCHARGE:
                    row = self.zero(); row[self.var(i, CHARGE)] = Fraction(1)
                    le.append(row); rhs_le.append(Fraction(0))
                elif mode == MODE_IDLE:
                    row = self.zero(); row[self.var(i, DISCHARGE)] = Fraction(1)
                    le.append(row); rhs_le.append(Fraction(0))
                    row = self.zero(); row[self.var(i, CHARGE)] = Fraction(1)
                    le.append(row); rhs_le.append(Fraction(0))
                else:
                    raise ValueError("未知模式")
        for row, value in extra_eq:
            eq.append(row[:]); rhs_eq.append(value)
        for row, value in extra_le:
            le.append(row[:]); rhs_le.append(value)
        return eq, rhs_eq, le, rhs_le

    def minimize(
        self,
        objective: Sequence[Fraction],
        cap: Fraction | None,
        modes: Sequence[int] | None = None,
        extra_eq: Iterable[tuple[list[Fraction], Fraction]] = (),
        pivot_limit: int = 20000,
    ):
        eq, rhs_eq, le, rhs_le = self.constraints(cap, modes, extra_eq)
        return solve_lp(objective, eq, rhs_eq, le, rhs_le, pivot_limit=pivot_limit)

    # ------------------------------------------------------------ 解映射
    def unpack(self, vector: Sequence[Fraction], mode: Sequence[int] | None = None) -> "Schedule":
        periods = []
        for i, p in enumerate(self.periods):
            charge = vector[self.var(i, CHARGE)]
            discharge = vector[self.var(i, DISCHARGE)]
            if mode is not None:
                label = MODE_NAMES[mode[i]]
            elif charge > 0 and discharge > 0:
                # 连续松弛解可能同时充放；显式标注，避免被误读为可行调度
                label = "simultaneous"
            elif charge > 0:
                label = MODE_NAMES[MODE_CHARGE]
            elif discharge > 0:
                label = MODE_NAMES[MODE_DISCHARGE]
            else:
                label = MODE_NAMES[MODE_IDLE]
            periods.append(SchedulePeriod(
                id=p.id,
                hours=p.hours,
                import_energy=vector[self.var(i, IMPORT)],
                charge_energy=charge,
                discharge_energy=discharge,
                pv_used_energy=vector[self.var(i, PV_USED)],
                soc_end=vector[self.var(i, SOC)],
                mode=label,
            ))
        peaks = {label: vector[self.peak_index[label]] for label in self.peak_labels}
        return Schedule(
            scenario=self.scenario,
            periods=tuple(periods),
            demand_peaks=peaks,
            cap=None,
        )


@dataclass(frozen=True)
class SchedulePeriod:
    id: str
    hours: Fraction
    import_energy: Fraction
    charge_energy: Fraction
    discharge_energy: Fraction
    pv_used_energy: Fraction
    soc_end: Fraction
    mode: str | None


@dataclass(frozen=True)
class Schedule:
    scenario: Scenario
    periods: tuple[SchedulePeriod, ...]
    demand_peaks: dict[str, Fraction]
    cap: Fraction | None = None

    def cost(self) -> Fraction:
        b = self.scenario.battery
        total = Fraction(0)
        for p, sp in zip(self.scenario.periods, self.periods):
            total += p.price * sp.import_energy
            total += b.throughput_fee * (sp.charge_energy + sp.discharge_energy)
        for label, fee in self.scenario.demand_fees.items():
            total += fee * self.demand_peaks[label]
        return total

    def emission(self) -> Fraction:
        return sum(
            (p.emission_factor * sp.import_energy for p, sp in zip(self.scenario.periods, self.periods)),
            start=Fraction(0),
        )

    def throughput(self) -> Fraction:
        return sum((sp.charge_energy + sp.discharge_energy for sp in self.periods), start=Fraction(0))

    def modes(self) -> tuple[int, ...]:
        result = []
        for sp in self.periods:
            if sp.charge_energy > 0 and sp.discharge_energy > 0:
                result.append(-1)
            elif sp.charge_energy > 0:
                result.append(MODE_CHARGE)
            elif sp.discharge_energy > 0:
                result.append(MODE_DISCHARGE)
            else:
                result.append(MODE_IDLE)
        return tuple(result)

    def emission_by_period(self) -> list[Fraction]:
        return [p.emission_factor * sp.import_energy for p, sp in zip(self.scenario.periods, self.periods)]

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario.scenario_id,
            "cap": None if self.cap is None else text(self.cap),
            "cost": text(self.cost()),
            "emission": text(self.emission()),
            "throughput": text(self.throughput()),
            "demand_peaks": {k: text(v) for k, v in sorted(self.demand_peaks.items())},
            "periods": [
                {
                    "id": sp.id,
                    "hours": text(sp.hours),
                    "import": text(sp.import_energy),
                    "charge": text(sp.charge_energy),
                    "discharge": text(sp.discharge_energy),
                    "pv_used": text(sp.pv_used_energy),
                    "soc_end": text(sp.soc_end),
                    "mode": sp.mode,
                    "emission": text(e),
                }
                for sp, e in zip(self.periods, self.emission_by_period())
            ],
        }


def schedule_order_key(schedule: Schedule) -> tuple:
    """费用、排放、吞吐量、按时间顺序的决策字典序。"""
    seq = []
    for sp in schedule.periods:
        seq.extend((sp.import_energy, sp.charge_energy, sp.discharge_energy,
                    sp.pv_used_energy, sp.soc_end))
    return (schedule.cost(), schedule.emission(), schedule.throughput(), tuple(seq))
