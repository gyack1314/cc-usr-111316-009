"""前沿分析与业务问题的组织层。

* ``run_frontier``：对每个业务登记的排放上限独立求解，返回可行性、最优
  费用、证明下界、间隙、非支配点与不可达上限。
* ``equal_split_check``：把同一恒定条件的时段拆成均匀小格，比较整格与
  拆格口径，并且只在声明允许每格独立选择模式时才把两者当作可比。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Sequence

from .model import Model, Schedule
from .numeric import text
from .optimizer import (
    CapResult, FEASIBLE, INFEASIBLE,
    solve_cap, minimum_emission, baseline_schedule,
)
from .scenario import Scenario
from .verify import verify, Verification

# 声明口径：拆格后每格仍独立选择"闲/充/放"。
ALLOW_MULTI_MODE = False


@dataclass
class FrontierPoint:
    cap: Fraction
    result: CapResult
    verification: Verification | None
    dominated: bool = False

    def to_dict(self) -> dict:
        result = self.result.to_dict()
        result["verification"] = None if self.verification is None else self.verification.to_dict()
        result["dominated"] = self.dominated
        return result


@dataclass
class Frontier:
    scenario_id: str
    points: list[FrontierPoint]
    baseline: Schedule | None
    min_emission: CapResult | None
    allowed_mode_invariant: bool = ALLOW_MULTI_MODE
    notes: list[str] = field(default_factory=list)

    def nondominated(self) -> list[FrontierPoint]:
        return [p for p in self.points if p.result.status == FEASIBLE and not p.dominated]

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "allowed_mode_invariant": self.allowed_mode_invariant,
            "notes": self.notes,
            "min_emission_unconstrained": None if self.min_emission is None else self.min_emission.to_dict(),
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "points": [p.to_dict() for p in self.points],
        }


def _dominate(points: Sequence[FrontierPoint]) -> None:
    feasible = [p for p in points if p.result.status == FEASIBLE and p.result.schedule is not None]
    for p in feasible:
        p.dominated = False
    for a in feasible:
        for b in feasible:
            if a is b or a.dominated:
                continue
            if (b.result.cost <= a.result.cost and b.result.emission <= a.result.emission
                    and (b.result.cost < a.result.cost or b.result.emission < a.result.emission)):
                a.dominated = True
                break


def run_frontier(scenario: Scenario, model: Model | None = None) -> Frontier:
    """对每个登记上限独立求解并重放校验。"""
    model = model or Model(scenario)
    points = []
    for cap in scenario.emission_caps:
        # solve_cap 默认按情景 solver 设置独立新建预算：一个上限耗尽不会
        # 吞掉其它上限的求解机会。
        result = solve_cap(model, cap)
        verification = None
        if result.schedule is not None:
            verification = verify(scenario, result.schedule)
            if not verification.ok:
                raise AssertionError("求解器返回的调度未通过独立重放校验")
        points.append(FrontierPoint(cap=cap, result=result, verification=verification))

    _dominate(points)
    baseline = baseline_schedule(scenario)
    minimum = minimum_emission(model)

    notes = [
        "每个上限独立求解，不同上限之间不共享能量或模式。",
        "下限来自同情景连续松弛（模式互斥删除）或未处理分支的最小节点下界。",
        "离散模式使费用-排放前沿可能非凸；本报告不以加权和声称找全连续前沿。",
        "不可行指全部模式分支均无可行解；超时/预算耗尽绝不解释为无解。",
    ]
    if minimum.status == FEASIBLE and minimum.emission is not None:
        unreachable = [p.cap for p in points
                       if p.result.status == INFEASIBLE and minimum.emission > p.cap]
        if unreachable:
            notes.append(
                "无上限时可达最小排放为 " + text(minimum.emission)
                + "；更低的上限在末态、效率与模式互斥下不可达。"
            )
    return Frontier(
        scenario_id=scenario.scenario_id,
        points=points,
        baseline=baseline,
        min_emission=minimum,
        notes=notes,
    )


def split_comparison(
    whole: Model,
    split: Model,
    cap: Fraction,
) -> dict:
    """比较"整格"与"等分拆格"两种口径。

    三者都报告：整格、拆格且每格独立选模式、拆格但子格绑定同一模式。
    只有在业务声明允许每格独立选择闲/充/放时，前两者才可直接比较；
    绑定模式的结果是可核对的"同一声明"检查。只要拆分的格边界、时长、
    负荷、电价、排放强度与合同完全一致，拆格就不应改变单格内的物理解，
    但会增加可行模式组合。
    """
    if not _constant_conditions(whole.scenario, split.scenario):
        raise ValueError("拆分前后条件不是同一恒定情景，不能比较")
    whole_result = solve_cap(whole, cap)
    split_result = solve_cap(split, cap)
    grouped = solve_cap(split, cap, group_of=tuple([0] * len(split.scenario.periods)))
    return {
        "cap": text(cap),
        "whole": {
            "status": whole_result.status,
            "cost": None if whole_result.cost is None else text(whole_result.cost),
            "emission": None if whole_result.emission is None else text(whole_result.emission),
            "throughput": None if whole_result.throughput is None else text(whole_result.throughput),
        },
        "split_independent_modes": {
            "status": split_result.status,
            "cost": None if split_result.cost is None else text(split_result.cost),
            "emission": None if split_result.emission is None else text(split_result.emission),
            "throughput": None if split_result.throughput is None else text(split_result.throughput),
        },
        "split_bound_to_one_mode": {
            "status": grouped.status,
            "cost": None if grouped.cost is None else text(grouped.cost),
            "emission": None if grouped.emission is None else text(grouped.emission),
            "throughput": None if grouped.throughput is None else text(grouped.throughput),
        },
        "comparable": grouped.status == whole_result.status and (
            grouped.cost == whole_result.cost if grouped.status == FEASIBLE else True
        ),
        "interpretation": (
            "整格与绑定同模式的拆格结果一致，说明拆格没有改变同一声明下的物理解；"
            "独立选模式的拆格结果不同，是模式集合扩大造成的，不能当作等价结果。"
        ),
    }


def _constant_conditions(whole: Scenario, split: Scenario) -> bool:
    """拆格前后必须保持负荷、电价、排放强度、合同与总时长不变。"""
    keys = ("load", "pv", "import_limit", "charge_limit", "discharge_limit",
            "price", "emission_factor", "billing_period")
    if len(whole.periods) != 1:
        return False
    first = whole.periods[0]
    for period in split.periods:
        if any(getattr(period, key) != getattr(first, key) for key in keys):
            return False
    if split.battery != whole.battery:
        return False
    total_hours = sum((p.hours for p in split.periods), start=Fraction(0))
    if total_hours != first.hours:
        return False
    if set(split.demand_fees) != set(whole.demand_fees):
        return False
    return True
