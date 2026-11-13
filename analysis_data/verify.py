"""独立逐格重放校验。

校验器不复用求解器的约束矩阵，而是从原始情景和调度向量重新计算：
逐格电力平衡、光伏使用、购电上限、充放功率上限、模式互斥、状态推进
（含停机间隙自放电）、容量、首末状态、逐格排放，以及计费周期需量峰值
和三类费用。任何一项不符都会报出具体格与差额。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .model import Schedule, period_energy
from .numeric import text
from .scenario import Scenario


@dataclass
class Check:
    name: str
    period_id: str | None
    ok: bool
    detail: str


@dataclass
class Verification:
    schedule: Schedule
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "period_id": c.period_id, "ok": c.ok, "detail": c.detail}
                for c in self.checks
            ],
            "failures": [
                {"name": c.name, "period_id": c.period_id, "detail": c.detail}
                for c in self.failures()
            ],
        }


def _retention(scenario: Scenario, index: int) -> Fraction:
    """独立重算第 index 格的复合保留系数（含格前停机间隙）。

    刻意不复用求解器的衰减表：这里直接按"小时保留系数的幂"定义计算，
    以便校验器能发现求解器在间隙或幂运算上的错误。
    """
    period = scenario.periods[index]
    if index == 0:
        before = Fraction(1)
    else:
        previous = scenario.periods[index - 1]
        end = previous.start + _delta(previous.hours)
        delta = period.start - end
        gap = Fraction(delta.days * 86400 + delta.seconds, 3600)
        if delta.microseconds or gap < 0:
            raise ValueError("时间格边界非整秒或发生重叠")
        before = _power(scenario.battery.hourly_retention, gap)
    return before * _power(scenario.battery.hourly_retention, period.hours)


def _delta(hours: Fraction):
    from datetime import timedelta
    if (hours * 3600).denominator != 1:
        raise ValueError("时长不能表示为整秒")
    return timedelta(seconds=int(hours * 3600))


def _power(base: Fraction, exponent: Fraction) -> Fraction:
    """精确分数次幂；分数指数必须在有理数上可开方，否则明确报错。"""
    if exponent.denominator == 1:
        return base ** exponent.numerator
    root = exponent.denominator

    def exact(value: int) -> int | None:
        if value < 0:
            return None
        if value in (0, 1):
            return value
        low, high = 1, 1
        while high ** root <= value:
            high *= 2
        while low <= high:
            mid = (low + high) // 2
            if mid ** root == value:
                return mid
            if mid ** root < value:
                low = mid + 1
            else:
                high = mid - 1
        return None

    numerator = exact(base.numerator)
    denominator = exact(base.denominator)
    if numerator is None or denominator is None:
        raise ValueError("保留系数的分数次幂不是精确有理数")
    return Fraction(numerator, denominator) ** exponent.numerator


def verify(scenario: Scenario, schedule: Schedule) -> Verification:
    b = scenario.battery
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str, period_id: str | None = None) -> None:
        checks.append(Check(name=name, period_id=period_id, ok=ok, detail=detail))

    if len(schedule.periods) != len(scenario.periods):
        add("长度", False, f"调度有 {len(schedule.periods)} 格，情景有 {len(scenario.periods)} 格")
        return Verification(schedule, checks)

    recomputed_peaks = {label: Fraction(0) for label in scenario.demand_fees}
    recomputed_cost = Fraction(0)
    recomputed_emission = Fraction(0)
    soc = b.initial_soc
    retention = [_retention(scenario, i) for i in range(len(scenario.periods))]
    for i, (p, sp) in enumerate(zip(scenario.periods, schedule.periods)):
        if sp.id != p.id:
            add("格身份", False, f"{sp.id} 与 {p.id} 不符", p.id)
        e = period_energy(p)
        pid = p.id

        # 非负
        for field_name, value in (
            ("购电", sp.import_energy), ("充电", sp.charge_energy),
            ("放电", sp.discharge_energy), ("光伏使用", sp.pv_used_energy),
        ):
            add(f"非负/{field_name}", value >= 0, f"{text(value)}", pid)

        # 光伏
        add("光伏上限", sp.pv_used_energy <= e["pv"], f"使用 {text(sp.pv_used_energy)}，可用 {text(e['pv'])}", pid)
        add("禁止外送", sp.import_energy <= e["import_limit"], f"购电 {text(sp.import_energy)}，上限 {text(e['import_limit'])}", pid)
        add("充电上限", sp.charge_energy <= e["charge_limit"], f"{text(sp.charge_energy)} <= {text(e['charge_limit'])}", pid)
        add("放电上限", sp.discharge_energy <= e["discharge_limit"], f"{text(sp.discharge_energy)} <= {text(e['discharge_limit'])}", pid)

        # 模式互斥（唯一整数决策）
        add("充放互斥", not (sp.charge_energy > 0 and sp.discharge_energy > 0),
            f"充电 {text(sp.charge_energy)}，放电 {text(sp.discharge_energy)}", pid)

        # 平衡：import + pv_used + discharge == load + charge
        lhs = sp.import_energy + sp.pv_used_energy + sp.discharge_energy
        rhs = e["load"] + sp.charge_energy
        add("电力平衡", lhs == rhs, f"左 {text(lhs)}，右 {text(rhs)}", pid)

        # 状态推进
        expected = (soc * retention[i]
                    + b.charge_efficiency * sp.charge_energy
                    - sp.discharge_energy / b.discharge_efficiency)
        add("状态推进", expected == sp.soc_end,
            f"重算 {text(expected)}，调度 {text(sp.soc_end)}", pid)
        add("容量下界", sp.soc_end >= 0, text(sp.soc_end), pid)
        add("容量上界", sp.soc_end <= b.capacity, f"{text(sp.soc_end)} <= {text(b.capacity)}", pid)
        soc = sp.soc_end

        # 计费周期峰值（功率 = 能量/时长）
        power = sp.import_energy / p.hours
        if power > recomputed_peaks[p.billing_period]:
            recomputed_peaks[p.billing_period] = power

        recomputed_cost += p.price * sp.import_energy
        recomputed_cost += b.throughput_fee * (sp.charge_energy + sp.discharge_energy)
        recomputed_emission += p.emission_factor * sp.import_energy

    add("末状态", soc == b.terminal_soc, f"末态 {text(soc)}，要求 {text(b.terminal_soc)}")
    for label, peak in recomputed_peaks.items():
        declared = schedule.demand_peaks.get(label)
        add(f"需量峰值/{label}", declared == peak, f"调度 {None if declared is None else text(declared)}，重算 {text(peak)}")
        recomputed_cost += scenario.demand_fees[label] * peak

    add("费用", recomputed_cost == schedule.cost(), f"重算 {text(recomputed_cost)}，调度 {text(schedule.cost())}")
    add("排放", recomputed_emission == schedule.emission(), f"重算 {text(recomputed_emission)}，调度 {text(schedule.emission())}")

    # 独立核对声明的模式标签与能量一致
    for p, sp in zip(scenario.periods, schedule.periods):
        if sp.mode is None:
            continue
        if sp.mode == "charge":
            ok = sp.charge_energy >= 0 and sp.discharge_energy == 0
        elif sp.mode == "discharge":
            ok = sp.discharge_energy >= 0 and sp.charge_energy == 0
        else:
            ok = sp.charge_energy == 0 and sp.discharge_energy == 0
        add("模式标签", ok, f"标签 {sp.mode}，充 {text(sp.charge_energy)}，放 {text(sp.discharge_energy)}", sp.id)

    return Verification(schedule, checks)
