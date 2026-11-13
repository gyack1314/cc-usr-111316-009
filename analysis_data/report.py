"""把前沿结果翻译成业务问题的答复：钱省在哪里、排放移到哪里、哪些目标
根本达不到。

全部输出只描述离线方案，不控制任何设备。数值保持精确有理数字符串。
"""
from __future__ import annotations

from fractions import Fraction

from .frontier import Frontier
from .model import Schedule
from .numeric import text


def period_deltas(schedule: Schedule, baseline: Schedule) -> list[dict]:
    """逐格比较参照调度与方案，给出费用、排放与能量迁移。"""
    rows = []
    b = schedule.scenario.battery
    for period, sp, bp in zip(schedule.scenario.periods, schedule.periods, baseline.periods):
        base_cost = period.price * bp.import_energy + b.throughput_fee * (bp.charge_energy + bp.discharge_energy)
        plan_cost = period.price * sp.import_energy + b.throughput_fee * (sp.charge_energy + sp.discharge_energy)
        rows.append({
            "id": sp.id,
            "price": text(period.price),
            "emission_factor": text(period.emission_factor),
            "import_baseline": text(bp.import_energy),
            "import_plan": text(sp.import_energy),
            "charge": text(sp.charge_energy),
            "discharge": text(sp.discharge_energy),
            "soc_end": text(sp.soc_end),
            "mode": sp.mode,
            "cost_delta": text(plan_cost - base_cost),
            "emission_delta": text(period.emission_factor * sp.import_energy
                                   - period.emission_factor * bp.import_energy),
        })
    return rows


def demand_fee_delta(schedule: Schedule, baseline: Schedule) -> dict:
    fees = {}
    for label, fee in schedule.scenario.demand_fees.items():
        peaks = schedule.demand_peaks[label] - baseline.demand_peaks[label]
        fees[label] = {
            "rate": text(fee),
            "peak_change_kw": text(peaks),
            "fee_change": text(fee * peaks),
        }
    return fees


def compare_baseline(frontier: Frontier) -> list[dict]:
    """对每个可行点比较最优方案与参照调度（参照可能不满足末态约束）。"""
    baseline = frontier.baseline
    results = []
    for point in frontier.points:
        result = point.result
        if result.status != "feasible" or result.schedule is None:
            results.append({"cap": text(point.cap), "status": result.status})
            continue
        schedule = result.schedule
        rows = period_deltas(schedule, baseline)
        cost_delta = schedule.cost() - baseline.cost()
        emission_delta = schedule.emission() - baseline.emission()
        results.append({
            "cap": text(point.cap),
            "status": result.status,
            "cost": text(schedule.cost()),
            "baseline_cost": text(baseline.cost()),
            "cost_delta": text(cost_delta),
            "emission": text(schedule.emission()),
            "baseline_emission": text(baseline.emission()),
            "emission_delta": text(emission_delta),
            "periods": rows,
            "demand_fee_changes": demand_fee_delta(schedule, baseline),
        })
    return results


def impossible_targets(frontier: Frontier) -> dict:
    """区分'证明不可行'与'预算内未找到'，并报告可达最小排放。"""
    proven = []
    unfinished = []
    for point in frontier.points:
        result = point.result
        if result.status == "infeasible" and result.optimality_proven:
            proven.append(text(point.cap))
        elif result.status in ("timeout", "unknown") or not result.optimality_proven:
            unfinished.append(text(point.cap))
    minimum = frontier.min_emission
    return {
        "proven_infeasible_caps": proven,
        "unfinished_caps": unfinished,
        "minimum_emission_without_cap": None if minimum is None or minimum.emission is None
                                         else text(minimum.emission),
        "minimum_emission_optimality_proven": None if minimum is None else minimum.optimality_proven,
    }


def summary_report(frontier: Frontier) -> dict:
    """业务解读所需的一页式汇总。"""
    comparisons = compare_baseline(frontier)
    feasible = [c for c in comparisons if c["status"] == "feasible"]
    cheapest = min(feasible, key=lambda c: Fraction(c["cost"])) if feasible else None
    cleanest = min(feasible, key=lambda c: Fraction(c["emission"])) if feasible else None
    nondominated = [text(p.cap) for p in frontier.nondominated()]
    return {
        "scenario_id": frontier.scenario_id,
        "nondominated_caps": nondominated,
        "all_caps": [text(p.cap) for p in frontier.points],
        "impossible_targets": impossible_targets(frontier),
        "cheapest_feasible": cheapest,
        "cleanest_feasible": cleanest,
        "comparisons": comparisons,
        "caveats": [
            "前沿由有限个登记上限得到，不代表连续前沿已找全。",
            "离散模式互斥会让费用-排放集非凸，加权和不能代替穷举。",
            "不同 cap 的方案互不共享能量；平均输入求出的方案不能称为所有情景可执行。",
            "参照调度不做储能且不保证末态 SOC，仅用于展示排放位移。",
        ],
    }
