"""给定排放上限的费用最优调度：精确分支定界 + 字典型破同分。

离散决策只有"每格至多充电或放电一种模式"。本模块不做任何"电价通常
为正"的假设，而是把模式互斥当作真约束：

* 连续松弛（删除模式互斥）给出合法下界，并显式暴露被禁止的同时充放。
* 分支定界在每个节点求解连续 LP；若解仍然同时充放，就在该格分支为
  "禁充"与"禁放"两支。每个分支都把某个连续变量压到 0，树有限且完整。
* 到达叶节点（解已满足互斥）后，用字典序 LP 在该分支内求
  (费用, 排放, 吞吐量, 逐格 import→charge→discharge→pv_used→soc) 最小。
* 只有所有带下界且可能改进的节点都被处理，才宣称最优性；预算耗尽时
  返回当前可行调度、合法下界与间隙，状态标为 timeout，绝不把"没找到解"
  当成无解。

小日历另提供 ``enumerate_modes`` 完整枚举全部 3^n 模式组合，用于与
分支定界交叉核对，并向分析者展示离散前沿的结构。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field, replace
from fractions import Fraction
from itertools import product
from typing import Sequence

from .lp import solve_lp, OPTIMAL, INFEASIBLE, UNBOUNDED, STEP_LIMIT, LPResult
from .model import (
    Model, Schedule, SchedulePeriod, MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE, MODE_NAMES,
    gap_hours, period_energy,
)
from .numeric import text
from .scenario import Scenario

FEASIBLE = "feasible"
INFEASIBLE = "infeasible"
TIMEOUT = "timeout"
UNKNOWN = "unknown"

# 模式标签 -> 需要在分支中压到 0 的变量
_IMPORT, _CHARGE, _DISCHARGE = 0, 1, 2


@dataclass
class CapResult:
    cap: Fraction | None
    status: str
    cost: Fraction | None
    emission: Fraction | None
    throughput: Fraction | None
    lower_bound: Fraction | None
    gap: Fraction | None
    relaxation_gap: Fraction | None
    schedule: Schedule | None
    optimality_proven: bool
    mode_assignment: tuple[int, ...] | None
    explored_modes: int
    total_modes: int | None
    lp_pivots: int
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "cap": None if self.cap is None else text(self.cap),
            "status": self.status,
            "optimality_proven": self.optimality_proven,
            "explored_modes": self.explored_modes,
            "total_modes": self.total_modes,
            "lp_pivots": self.lp_pivots,
            "cost": None if self.cost is None else text(self.cost),
            "emission": None if self.emission is None else text(self.emission),
            "throughput": None if self.throughput is None else text(self.throughput),
            "lower_bound": None if self.lower_bound is None else text(self.lower_bound),
            "gap": None if self.gap is None else text(self.gap),
            "relaxation_gap": None if self.relaxation_gap is None else text(self.relaxation_gap),
            "mode_assignment": None if self.mode_assignment is None else list(self.mode_assignment),
            "detail": self.detail,
        }
        if self.schedule is not None:
            result["schedule"] = self.schedule.to_dict()
        return result


class Budget:
    """跨上限共享的步数与墙钟预算。"""

    def __init__(self, max_steps: int, time_limit_seconds: float):
        import time
        self.max_steps = max_steps
        self.time_limit_seconds = time_limit_seconds
        self._clock = time.monotonic
        self.started = self._clock()
        self.steps = 0

    def start(self) -> "Budget":
        self.started = self._clock()
        return self

    def spend(self, n: int = 1) -> bool:
        self.steps += n
        if self.steps > self.max_steps:
            return False
        return self._clock() - self.started <= self.time_limit_seconds

    @property
    def elapsed(self) -> float:
        return self._clock() - self.started


def _new_budget(model: Model, budget: Budget | None) -> Budget:
    """未传入预算时新建；传入的预算代表本次调用的独立额度。

    每次都重置步数与计时，避免上一次求解的消耗静默吞掉后续上限的机会。
    """
    if budget is not None:
        budget.steps = 0
        budget.start()
        return budget
    return Budget(model.scenario.solver.max_steps, model.scenario.solver.time_limit_seconds)


def _mode_branch(modes: Sequence[int]) -> tuple[tuple[int, str], ...]:
    """完整模式指派 -> 等价的 0 上界行。

    charge -> 禁放；discharge -> 禁充；idle -> 两者都禁。
    """
    rows: list[tuple[int, str]] = []
    for i, mode in enumerate(modes):
        if mode == MODE_CHARGE:
            rows.append((i, "zero_discharge"))
        elif mode == MODE_DISCHARGE:
            rows.append((i, "zero_charge"))
        elif mode == MODE_IDLE:
            rows.append((i, "zero_charge"))
            rows.append((i, "zero_discharge"))
        else:
            raise ValueError("未知模式")
    return tuple(rows)


def _fixed_rows(model: Model, branch: Sequence[tuple[int, str]]) -> list[tuple[list[Fraction], Fraction]]:
    rows = []
    for period, kind in branch:
        column = _CHARGE if kind in ("zero_charge", "charge_zero") else _DISCHARGE
        row = model.zero()
        row[model.var(period, column)] = Fraction(1)
        rows.append((row, Fraction(0)))
    return rows


def _solve_node(model: Model, cap: Fraction | None, branch: Sequence[tuple[int, str]],
                objective: Sequence[Fraction]) -> LPResult:
    eq, rhs_eq, le, rhs_le = model.constraints(cap)
    for row, value in _fixed_rows(model, branch):
        le.append(row)
        rhs_le.append(value)
    return solve_lp(objective, eq, rhs_eq, le, rhs_le,
                    pivot_limit=model.scenario.solver.max_steps)


def _simultaneous(vector: Sequence[Fraction], model: Model) -> list[int]:
    return [i for i in range(model.n)
            if vector[model.var(i, _CHARGE)] > 0 and vector[model.var(i, _DISCHARGE)] > 0]


def _order_key(schedule: Schedule) -> tuple:
    seq = tuple(
        x for sp in schedule.periods
        for x in (sp.import_energy, sp.charge_energy, sp.discharge_energy,
                  sp.pv_used_energy, sp.soc_end)
    )
    return (schedule.cost(), schedule.emission(), schedule.throughput(), seq)


def _lex_under(model: Model, cap: Fraction | None, branch: Sequence[tuple[int, str]],
               cost_bound: Fraction, budget: Budget,
               emission_bound: Fraction | None = None,
               throughput_bound: Fraction | None = None) -> Schedule | None:
    """在固定分支、费用与排放/吞吐上界内求逐格决策字典序最小的连续解。

    费用、排放、吞吐量三元组已经由 :func:`_mode_triple` 固定；这里只对
    逐格 (import, charge, discharge, pv_used, soc) 做字典序，以满足
    "费用、排放、总吞吐量，再按时间顺序"的完整破同分规则。
    """
    eq, rhs_eq, le, rhs_le = model.constraints(cap)
    extra = _fixed_rows(model, branch)
    le.extend(row for row, _ in extra)
    rhs_le.extend(value for _, value in extra)
    extra_eq: list[tuple[list[Fraction], Fraction]] = []
    extra_le: list[tuple[list[Fraction], Fraction]] = [(model.cost_vector(), cost_bound)]
    if emission_bound is not None:
        extra_le.append((model.emission_vector(), emission_bound))
    if throughput_bound is not None:
        extra_le.append((model.throughput_vector(), throughput_bound))

    def solve(objective):
        return solve_lp(
            objective,
            eq + [r for r, _ in extra_eq], rhs_eq + [v for _, v in extra_eq],
            le + [r for r, _ in extra_le], rhs_le + [v for _, v in extra_le],
            pivot_limit=model.scenario.solver.max_steps,
        )

    vector = None
    for i in range(model.n):
        for kind in (_IMPORT, _CHARGE, _DISCHARGE, 3, 4):
            objective = model.zero()
            objective[model.var(i, kind)] = Fraction(1)
            result = solve(objective)
            budget.spend(1)
            if result.status != OPTIMAL:
                return None
            extra_eq.append((objective, result.objective))
            vector = result.vector
    if vector is None:
        return None
    cost = sum((model.cost_vector()[j] * vector[j] for j in range(model.nv)), start=Fraction(0))
    if cost > cost_bound:
        raise AssertionError("字典序解超过费用上界")
    return replace(model.unpack(vector), cap=cap)


def _relaxation_detail(model: Model, cap: Fraction | None, relaxation: LPResult,
                       objective_is_cost: bool) -> dict:
    detail = {"relaxation_status": relaxation.status}
    if relaxation.status == OPTIMAL and relaxation.vector is not None:
        relaxed = replace(model.unpack(relaxation.vector), cap=cap)
        simultaneous = _simultaneous(relaxation.vector, model)
        detail["relaxed_cost"] = text(relaxed.cost()) if objective_is_cost else None
        detail["relaxed_emission"] = text(relaxed.emission())
        detail["relaxed_simultaneous_charge_discharge"] = [model.periods[i].id for i in simultaneous]
        detail["relaxed_schedule"] = relaxed.to_dict()
    return detail


def _mode_triple(model: Model, cap: Fraction | None, branch: Sequence[tuple[int, str]],
                 budget: Budget) -> tuple[Fraction, Fraction, Fraction, list[Fraction]] | None:
    """固定模式后的 (费用, 排放, 吞吐量) 词典最优三元组。

    只解 3 个 LP（费用 -> 在费用最优面上最小排放 -> 再最小吞吐量），
    不再逐变量做完整字典序；后者仅用于最终最优模式。
    返回 None 表示该模式不可行或预算不足。
    """
    eq, rhs_eq, le, rhs_le = model.constraints(cap)
    extra = _fixed_rows(model, branch)
    le.extend(row for row, _ in extra)
    rhs_le.extend(value for _, value in extra)
    extra_le: list[tuple[list[Fraction], Fraction]] = []

    def solve(objective):
        return solve_lp(objective, eq, rhs_eq,
                        le + [r for r, _ in extra_le], rhs_le + [v for _, v in extra_le],
                        pivot_limit=model.scenario.solver.max_steps)

    vector = None
    values = []
    for objective in (model.cost_vector(), model.emission_vector(), model.throughput_vector()):
        result = solve(objective)
        budget.spend(1)
        if result.status != OPTIMAL:
            return None
        extra_le.append((objective, result.objective))
        values.append(result.objective)
        vector = result.vector
    assert vector is not None
    return values[0], values[1], values[2], vector


def _branch_and_bound(model: Model, cap: Fraction | None, budget: Budget,
                      objective: Sequence[Fraction] | None) -> CapResult:
    """精确分支定界。``objective`` 为 None 时按费用并做字典型破同分。"""
    cost_like = objective is None
    primary = model.cost_vector() if cost_like else objective

    relaxation = _solve_node(model, cap, (), primary)
    budget.spend(1)
    detail = _relaxation_detail(model, cap, relaxation, cost_like)
    if relaxation.status == INFEASIBLE:
        detail.update({"nodes": 0, "integer_leaves": 0, "open_nodes": 0,
                       "proven": True, "reason": "连续松弛已证明不可行",
                       "budget_steps": budget.steps, "budget_elapsed_seconds": round(budget.elapsed, 6)})
        return CapResult(cap, INFEASIBLE, None, None, None, None, None, None, None,
                         True, None, 0, None, 0, detail)
    if relaxation.status in (STEP_LIMIT, UNBOUNDED):
        detail.update({"reason": "连续松弛未在预算内求解",
                       "budget_steps": budget.steps, "budget_elapsed_seconds": round(budget.elapsed, 6)})
        return CapResult(cap, TIMEOUT, None, None, None, None, None, None, None,
                         False, None, 0, None, 0, detail)
    global_bound = relaxation.objective
    detail["global_relaxation_bound"] = text(global_bound)

    incumbent: Schedule | None = None
    incumbent_key: tuple | None = None
    incumbent_modes: tuple[int, ...] | None = None
    leaves = 0
    nodes = 1
    pivots = relaxation.pivots
    proven = False
    budget_hit = False
    reason = ""

    heap: list[tuple[Fraction, int, tuple[tuple[int, str], ...]]] = []
    counter = 0
    # 根节点先处理（分支约束为空）
    root = relaxation
    queue: list[tuple[Fraction, int, tuple[tuple[int, str], ...], LPResult]] = [
        (global_bound, counter, (), root)
    ]
    counter += 1

    while queue or heap:
        if queue:
            bound, _, branch, relaxed_result = queue.pop()
        else:
            bound, _, branch = heapq.heappop(heap)
            if incumbent_key is not None and bound > incumbent_key[0]:
                continue
            if not budget.spend(1):
                budget_hit = True
                heapq.heappush(heap, (bound, counter, branch))
                counter += 1
                break
            nodes += 1
            relaxed_result = _solve_node(model, cap, branch, primary)
            pivots += relaxed_result.pivots
            if relaxed_result.status == INFEASIBLE:
                continue
            if relaxed_result.status in (STEP_LIMIT, UNBOUNDED):
                budget_hit = True
                heapq.heappush(heap, (bound, counter, branch))
                counter += 1
                break
        vector = relaxed_result.vector
        assert vector is not None
        node_bound = relaxed_result.objective
        assert node_bound is not None
        if incumbent_key is not None and node_bound > incumbent_key[0]:
            continue
        simultaneous = _simultaneous(vector, model)
        if not simultaneous:
            leaves += 1
            modes = tuple(
                MODE_CHARGE if vector[model.var(i, _CHARGE)] > 0 else
                MODE_DISCHARGE if vector[model.var(i, _DISCHARGE)] > 0 else MODE_IDLE
                for i in range(model.n)
            )
            if cost_like:
                triple = _mode_triple(model, cap, branch, budget)
                if triple is None:
                    budget_hit = True
                    heapq.heappush(heap, (bound, counter, branch))
                    counter += 1
                    break
                cost_value, emission_value, throughput_value, triple_vector = triple
                partial_key = (cost_value, emission_value, throughput_value)
                if incumbent_key is None or partial_key <= incumbent_key[:3]:
                    schedule = _lex_under(model, cap, branch, cost_value, budget,
                                          emission_bound=emission_value,
                                          throughput_bound=throughput_value)
                    if schedule is None:
                        budget_hit = True
                        heapq.heappush(heap, (bound, counter, branch))
                        counter += 1
                        break
                    key = _order_key(schedule)
                    if incumbent_key is None or key < incumbent_key:
                        incumbent, incumbent_key, incumbent_modes = schedule, key, modes
            else:
                schedule = replace(model.unpack(vector), cap=cap)
                key = (schedule.cost(),)
                if incumbent_key is None or key < incumbent_key:
                    incumbent, incumbent_key, incumbent_modes = schedule, key, modes
            continue
        period = simultaneous[0]
        for kind in ("zero_charge", "zero_discharge"):
            heapq.heappush(heap, (node_bound, counter, branch + ((period, kind),)))
            counter += 1

    if budget_hit:
        proven = False
        reason = "预算耗尽：存在未处理分支"
    elif not heap and not queue:
        proven = True
        reason = "搜索完成：全部分支已处理"
    else:
        proven = False
        reason = "存在未处理分支"

    incumbent_cost = None if incumbent is None else incumbent.cost()
    lower_bound: Fraction | None
    if proven:
        lower_bound = incumbent_cost if incumbent_cost is not None else global_bound
    elif queue or heap:
        open_bounds = [b for b, _, _ in heap] + [b for b, _, _, _ in queue]
        lower_bound = min(open_bounds)
    else:
        lower_bound = global_bound
    if incumbent_cost is not None and lower_bound is not None and lower_bound > incumbent_cost:
        lower_bound = incumbent_cost

    gap = None
    if incumbent_cost is not None:
        if proven:
            gap = Fraction(0)
        elif lower_bound is not None:
            gap = max(Fraction(0), incumbent_cost - lower_bound)
    relaxation_gap = None
    if incumbent_cost is not None:
        relaxation_gap = max(Fraction(0), incumbent_cost - global_bound)

    status = FEASIBLE if incumbent is not None else (INFEASIBLE if proven else TIMEOUT)
    detail.update({
        "nodes": nodes,
        "integer_leaves": leaves,
        "open_nodes": len(heap) + len(queue),
        "proven": proven,
        "reason": reason,
        "budget_steps": budget.steps,
        "budget_elapsed_seconds": round(budget.elapsed, 6),
    })
    return CapResult(
        cap=cap,
        status=status,
        cost=incumbent_cost,
        emission=None if incumbent is None else incumbent.emission(),
        throughput=None if incumbent is None else incumbent.throughput(),
        lower_bound=lower_bound,
        gap=gap,
        relaxation_gap=relaxation_gap,
        schedule=incumbent,
        optimality_proven=proven,
        mode_assignment=incumbent_modes,
        explored_modes=leaves,
        total_modes=None,
        lp_pivots=pivots,
        detail=detail,
    )


def _group_of_periods(scenario: Scenario, groups: Sequence[int] | None) -> tuple[int, ...] | None:
    if groups is None:
        return None
    if len(groups) != len(scenario.periods):
        raise ValueError("模式分组长度必须等于时间格数")
    return tuple(groups)


def _enumerated_groups(model: Model, cap: Fraction | None, budget: Budget,
                       group_of: tuple[int, ...], objective: Sequence[Fraction] | None,
                       lexicographic: bool) -> CapResult:
    """对分组模式完整枚举（用于小日历/等价拆格核对）。"""
    n_groups = max(group_of) + 1
    if 3 ** n_groups > 3000:
        raise ValueError("分组模式数超过枚举阈值，请改用逐格分支定界")
    best_key = None
    best = None
    detail = {"enumeration": True, "groups": n_groups, "proven": True}
    feasible = 0
    exhausted = False
    for group_modes in product((MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE), repeat=n_groups):
        if not budget.spend(0):
            exhausted = True
            detail["proven"] = False
            detail["reason"] = "枚举预算耗尽"
            break
        modes = tuple(group_modes[g] for g in group_of)
        branch = _mode_branch(modes)
        primary = model.cost_vector() if objective is None else objective
        result = _solve_node(model, cap, branch, primary)
        budget.spend(1)
        if result.status == INFEASIBLE:
            continue
        if result.status != OPTIMAL:
            exhausted = True
            detail["proven"] = False
            detail["reason"] = "枚举节点未完成"
            break
        feasible += 1
        if lexicographic and objective is None:
            triple = _mode_triple(model, cap, branch, budget)
            if triple is None:
                exhausted = True
                detail["proven"] = False
                detail["reason"] = "三元组子问题未完成"
                break
            cost_value, emission_value, throughput_value, _ = triple
            partial = (cost_value, emission_value, throughput_value)
            if best_key is not None and partial > best_key[:3]:
                continue
            schedule = _lex_under(model, cap, branch, cost_value, budget,
                                  emission_bound=emission_value,
                                  throughput_bound=throughput_value)
            if schedule is None:
                exhausted = True
                detail["proven"] = False
                detail["reason"] = "字典序子问题未完成"
                break
        else:
            schedule = replace(model.unpack(result.vector), cap=cap)
        key = _order_key(schedule) if objective is None else (schedule.cost(),)
        if best_key is None or key < best_key:
            best_key, best = key, (schedule, modes)
    if best is None:
        status = TIMEOUT if exhausted else INFEASIBLE
        return CapResult(cap, status, None, None, None, None, None, None, None,
                         not exhausted, None, feasible, 3 ** n_groups, 0, detail)
    schedule, modes = best
    detail["feasible_modes"] = feasible
    lower = schedule.cost()
    return CapResult(cap, FEASIBLE, schedule.cost(), schedule.emission(), schedule.throughput(),
                     lower, Fraction(0) if not exhausted else None,
                     Fraction(0), schedule, not exhausted, modes, feasible, 3 ** n_groups,
                     0, detail)


def solve_cap(
    model: Model,
    cap: Fraction | None,
    budget: Budget | None = None,
    group_of: Sequence[int] | None = None,
    objective: Sequence[Fraction] | None = None,
    lexicographic: bool = True,
) -> CapResult:
    """给定 cap 求最优调度。

    ``objective`` 为 None 时按费用最小，并用排放、吞吐量与逐格决策字典序
    破同分；否则按给定线性目标最小（此时不声明费用最优）。
    """
    budget = _new_budget(model, budget)
    grouped = _group_of_periods(model.scenario, group_of)
    if grouped is not None:
        return _enumerated_groups(model, cap, budget, grouped, objective,
                                  lexicographic and objective is None)
    return _branch_and_bound(model, cap, budget, objective)


def enumerate_modes(model: Model, cap: Fraction | None, budget: Budget | None = None) -> dict:
    """完整枚举全部 3^n 模式组合，分别求解连续子问题（仅小日历）。"""
    if 3 ** model.n > 3000:
        raise ValueError(f"3^{model.n} 超过枚举阈值")
    budget = _new_budget(model, budget)
    entries = []
    best_key = None
    best = None
    for modes in product((MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE), repeat=model.n):
        if not budget.spend(1):
            break
        branch = _mode_branch(modes)
        triple = _mode_triple(model, cap, branch, budget)
        entry = {"modes": modes, "status": "infeasible" if triple is None else "optimal"}
        if triple is not None:
            cost_value, emission_value, throughput_value, vector = triple
            schedule = replace(model.unpack(vector), cap=cap)
            if schedule.cost() != cost_value:
                raise AssertionError("三元组向量的费用与 LP 目标不符")
            entry.update({
                "cost": text(cost_value),
                "emission": text(emission_value),
                "throughput": text(throughput_value),
                "schedule": schedule.to_dict(),
            })
            if best_key is None or (cost_value, emission_value, throughput_value) < best_key[:3]:
                # 仅对可能改变最优的模式做完整逐格字典序，控制精确 LP 次数
                detailed = _lex_under(model, cap, branch, cost_value, budget,
                                      emission_bound=emission_value,
                                      throughput_bound=throughput_value)
                if detailed is not None:
                    key = _order_key(detailed)
                    if best_key is None or key < best_key:
                        best_key = key
                        best = {**entry, "modes": modes}
        entries.append(entry)
    return {"modes_evaluated": len(entries), "entries": entries,
            "best": None if best is None else best["modes"]}


def minimum_emission(model: Model, budget: Budget | None = None) -> CapResult:
    """不设排放上限时可达的最小排放（保留末态 SOC 与模式互斥）。"""
    return solve_cap(model, None, budget=budget,
                     objective=model.emission_vector(), lexicographic=False)


def max_emission(model: Model, cap: Fraction | None, budget: Budget | None = None) -> CapResult:
    """给定 cap 下的最大排放，作为前沿右端点参考。"""
    return solve_cap(model, cap, budget=budget,
                     objective=[-v for v in model.emission_vector()], lexicographic=False)


def baseline_schedule(scenario: Scenario) -> Schedule:
    """无储能、光伏优先自用的参照调度（不保证末态 SOC，故只作参照）。"""
    b = scenario.battery
    periods = []
    soc = b.initial_soc
    peaks = {label: Fraction(0) for label in scenario.demand_fees}
    for i, p in enumerate(scenario.periods):
        e = period_energy(p)
        pv_used = min(e["pv"], e["load"])
        imported = e["load"] - pv_used
        factor = b.hourly_retention ** gap_hours(scenario.periods, i) * b.hourly_retention ** p.hours
        soc = soc * factor
        power = imported / p.hours
        if power > peaks[p.billing_period]:
            peaks[p.billing_period] = power
        periods.append(SchedulePeriod(
            id=p.id, hours=p.hours, import_energy=imported, charge_energy=Fraction(0),
            discharge_energy=Fraction(0), pv_used_energy=pv_used, soc_end=soc,
            mode=MODE_NAMES[MODE_IDLE],
        ))
    return Schedule(scenario=scenario, periods=tuple(periods), demand_peaks=peaks, cap=None)
