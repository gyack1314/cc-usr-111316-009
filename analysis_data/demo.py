"""演示资料与“小日历穷举”报告。

演示资料全部是合成数值，用于核对模型口径，不构成实证结论。报告通过
穷举每一格的模式，给出全部可达 (排放, 费用) 点、Pareto 集合、凸包之外
的点（前沿非凸的证据），并逐条解释负电价、跨计费周期、效率损失和末态
约束为什么改变前沿。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Sequence

from .model import Model, MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE
from .numeric import text
from .optimizer import solve_cap, minimum_emission, max_emission, enumerate_modes
from .scenario import Scenario, read_scenario


DEMO_DIR = Path("demo_data")

# 每份演示资料的"要看什么"说明
EXPLANATIONS = {
    "small-calendar": "小日历：低价时段排放更高，储能把购电从贵时段移到便宜时段，"
                      "排放从 7 升到 10，费用从 6 降到 4。",
    "negative-price-simultaneous": "负电价：两格都是负电价。连续松弛在第二格同时充放，"
                                   "把负电价当成可重复的补贴，费用低到 -11；整数模式互斥"
                                   "禁止同时充放，上限 6 时最优费用 -19/2，比松弛下界高 3/2。"
                                   "排放上限 4 时松弛解本身已不满足上限，仍有 1/2 的间隙。"
                                   "这 3/2 正说明：不能拿连续松弛的加权和声称整数前沿。",
    "cross-billing": "跨计费周期：次月的需量费让月末低价充电有利，电池在 23:00 充电、"
                     "00:00 放电，同时压低两个计费周期的峰值。",
    "roundtrip-loss": "效率损失：往返效率 1/4 时，每放 1 kWh 要买 4 kWh；"
                      "排放上限放宽后，最优吞吐仍按效率比例增长。",
    "terminal-soc-one": "末状态约束：日末必须留 1 kWh，低价时段无法把便宜高排放电全部"
                        "用掉，可达最小排放从 5 抬到 6。",
    "terminal-soc-zero": "末状态约束：日末回到 0，电池可把低价高排放电完全移走；"
                         "同电价与负荷下最小排放为 5。",
    "split-one": "拆格对照（整格）：一个 2 小时恒定时段，储能只能选一种模式。"
                 "上限 2/3/4 的费用都是 -4、排放 2。",
    "split-two": "拆格对照（两格）：同一时段拆成两个 1 小时格。若声明仍允许每格"
                 "独立选闲/充/放，上限 3/4 的最优费用为 -6 与 -7，排放升到 3 与 7/2——"
                 "拆格扩大了可行方案集。若把两个子格绑定为同一模式，结果回到整格口径的"
                 "-4、排放 2；这意味着两种口径不能混用，只有在声明允许独立选模式时"
                 "拆格结果才可信。",
    "combined": "组合情景：跨计费周期、需量费、效率损失与负电价同时出现；"
                "上限 8 可行，费用 488/31，排放 8。",
    "nonconvex-midpoint": "非凸可行集：上限 2/3/4 的最优费用为 10、7、4，中间点"
                          "(3,7) 恰在两端点连线上（弦值也是 7），但端点的最优调度"
                          "是't1 放电 + t2 购电'与't1 充电 + t2 放电'；50/50 混合会让 t1"
                          "同时半充半放，违反每格至多一种模式，因此不可行。上限 3 的"
                          "7 是另一个可行调度（t1 闲置、t2 放电）凑出来的。这说明"
                          "可行集本身非凸：凸组合落在可行集外，费用-排放前沿上可以"
                          "出现连续松弛取不到的整数值。",
}


def _row(pid, start, hours, load, pv, import_limit, charge_limit, discharge_limit,
         price, emission_factor, billing_period):
    return {
        "id": pid, "start": start, "hours": hours, "load": load, "pv": pv,
        "import_limit": import_limit, "charge_limit": charge_limit,
        "discharge_limit": discharge_limit, "price": price,
        "emission_factor": emission_factor, "billing_period": billing_period,
    }


def _battery(**overrides):
    value = {
        "capacity": "1", "initial_soc": "0", "terminal_soc": "0",
        "charge_efficiency": "1", "discharge_efficiency": "1",
        "hourly_retention": "1", "throughput_fee": "0",
    }
    value.update({k: str(v) for k, v in overrides.items()})
    return value


def _config(scenario_id, battery, demand_fees, caps):
    return {
        "kind": "storage-frontier",
        "scenario_id": scenario_id,
        "battery": battery,
        "demand_fees": demand_fees,
        "emission_caps": [str(c) for c in caps],
        "solver": {"max_steps": 10000, "relative_gap": "0", "time_limit_seconds": 30},
    }


def _emit(directory: Path, name: str, rows: list[dict], config: dict) -> Path:
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf8")
    raw = path.read_bytes()
    config["periods"] = {"path": path.name, "bytes": len(raw),
                         "sha256": hashlib.sha256(raw).hexdigest(), "rows": len(rows)}
    config_path = directory / f"{name}.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return config_path


def build(directory: Path = DEMO_DIR) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = {}

    # A. 小日历：三格、逐格模式可穷举；便宜时段高排放
    written["small-calendar"] = _emit(
        directory, "small-calendar",
        [
            _row("t1", "2026-01-01T00:00:00Z", "1", "1", "0", "2", "1", "1", "1", "4", "p1"),
            _row("t2", "2026-01-01T01:00:00Z", "1", "1", "0", "2", "1", "1", "2", "2", "p1"),
            _row("t3", "2026-01-01T02:00:00Z", "1", "1", "0", "2", "1", "1", "3", "1", "p1"),
        ],
        _config("small-calendar", _battery(), {"p1": "0"}, ["6", "7", "8"]),
    )

    # A'. 等分拆格对照：同一恒定条件时段（负荷/电价/排放强度不变）拆成
    #     1 个 2 小时格 vs 2 个 1 小时格。整格里储能至多充或放一种模式；
    #     拆格后子格可各自选模式，可行方案集变大。因此两台方案只有在
    #     声明仍允许"每格独立选闲/充/放"时才可直接比较；若要求子格保持
    #     同一模式，结果不能与整格口径混用。
    written["split-one"] = _emit(
        directory, "split-one",
        [_row("h1", "2026-01-01T00:00:00Z", "2", "1", "0", "4", "2", "2", "-2", "1", "p1")],
        _config("split-one", _battery(capacity="2", charge_efficiency="1/2",
                                       discharge_efficiency="1/2"), {"p1": "0"}, ["2", "3", "4"]),
    )
    written["split-two"] = _emit(
        directory, "split-two",
        [
            _row("h1a", "2026-01-01T00:00:00Z", "1", "1", "0", "4", "2", "2", "-2", "1", "p1"),
            _row("h1b", "2026-01-01T01:00:00Z", "1", "1", "0", "4", "2", "2", "-2", "1", "p1"),
        ],
        _config("split-two", _battery(capacity="2", charge_efficiency="1/2",
                                       discharge_efficiency="1/2"), {"p1": "0"}, ["2", "3", "4"]),
    )

    # B. 负电价 + 效率损失：同一格同时充放在连续松弛里吃两次负电价补贴，
    #    整数模式互斥禁止它，下界与最优费用出现 9/4 的缺口。
    written["negative-price-simultaneous"] = _emit(
        directory, "negative-price-simultaneous",
        [
            _row("t1", "2026-01-01T00:00:00Z", "1", "1", "0", "5", "2", "2", "-3", "1", "p1"),
            _row("t2", "2026-01-01T01:00:00Z", "1", "1", "0", "5", "2", "2", "-1", "1", "p1"),
        ],
        _config("negative-price-simultaneous",
                _battery(capacity="2", charge_efficiency="1/2", discharge_efficiency="1/2"),
                {"p1": "0"}, ["2", "4", "6"]),
    )

    # C. 跨计费周期：月末低价充、次月高需量费时放电
    written["cross-billing"] = _emit(
        directory, "cross-billing",
        [
            _row("t1", "2026-01-31T23:00:00Z", "1", "1", "0", "3", "1", "1", "1", "2", "jan"),
            _row("t2", "2026-02-01T00:00:00Z", "1", "2", "0", "3", "1", "1", "3", "1", "feb"),
        ],
        _config("cross-billing", _battery(), {"jan": "0", "feb": "5"}, ["4", "5", "6"]),
    )

    # D. 末状态对照：同电价负荷，终值 1 与 0
    rows_terminal = [
        _row("t1", "2026-01-01T00:00:00Z", "1", "1", "0", "2", "1", "1", "-2", "3", "p1"),
        _row("t2", "2026-01-01T01:00:00Z", "1", "1", "0", "2", "1", "1", "3", "1", "p1"),
        _row("t3", "2026-01-01T02:00:00Z", "1", "1", "0", "2", "1", "1", "3", "1", "p1"),
    ]
    written["terminal-soc-one"] = _emit(
        directory, "terminal-soc-one", rows_terminal,
        _config("terminal-soc-one", _battery(terminal_soc="1"), {"p1": "0"}, ["6", "7", "8"]),
    )
    written["terminal-soc-zero"] = _emit(
        directory, "terminal-soc-zero", rows_terminal,
        _config("terminal-soc-zero", _battery(terminal_soc="0"), {"p1": "0"}, ["5", "6", "7"]),
    )

    # E. 效率损失：往返效率 1/4
    written["roundtrip-loss"] = _emit(
        directory, "roundtrip-loss",
        [
            _row("t1", "2026-01-01T00:00:00Z", "1", "1", "0", "4", "3", "1", "0", "3", "p1"),
            _row("t2", "2026-01-01T01:00:00Z", "1", "1", "0", "4", "3", "1", "4", "1", "p1"),
        ],
        _config("roundtrip-loss",
                _battery(capacity="2", charge_efficiency="1/2", discharge_efficiency="1/2"),
                {"p1": "0"}, ["4", "6", "8", "10"]),
    )

    # F. 组合：跨期 + 需量费 + 效率损失 + 负电价
    written["combined"] = _emit(
        directory, "combined",
        [
            _row("t1", "2026-01-31T22:00:00Z", "1", "1", "0", "3", "1", "1", "-1", "3", "jan"),
            _row("t2", "2026-01-31T23:00:00Z", "1", "1", "0", "3", "1", "1", "1", "2", "jan"),
            _row("t3", "2026-02-01T00:00:00Z", "1", "2", "0", "3", "1", "1", "4", "1", "feb"),
        ],
        _config("combined",
                _battery(charge_efficiency="3/4", discharge_efficiency="3/4", throughput_fee="1"),
                {"jan": "1", "feb": "4"}, ["4", "5", "6", "8"]),
    )

    # G. 非凸可行集：两个可行调度存在，但按凸组合得到的中间调度不可行。
    #    初始 SOC=1、末态=0、容量 2。方案 A 在 t1 放电、t2 买电；方案 B
    #    在 t1 充电、t2 放电。两者都可行；50/50 混合后 t1 同时半充半放，
    #    触发"每格至多充或放一种模式"，因此不可行。连续松弛允许同时充放，
    #    会给出两端之间的费用与排放，但现实中做不到。
    written["nonconvex-midpoint"] = _emit(
        directory, "nonconvex-midpoint",
        [
            _row("t1", "2026-01-01T00:00:00Z", "1", "1", "0", "4", "1", "1", "2", "2", "a"),
            _row("t2", "2026-01-01T01:00:00Z", "1", "2", "0", "4", "1", "2", "5", "1", "a"),
        ],
        _config("nonconvex-midpoint", _battery(capacity="2", initial_soc="1"),
                {"a": "0"}, ["2", "3", "4"]),
    )

    return written


def _pareto(points: Sequence[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    unique = sorted(set(points))
    pareto = []
    for e, c in unique:
        dominated = any(e2 <= e and c2 <= c and (e2 < e or c2 < c) for e2, c2 in unique)
        if not dominated:
            pareto.append((e, c))
    return pareto


def _nonconvex_points(pareto: Sequence[tuple[Fraction, Fraction]]) -> list[dict]:
    """在非支配点集上识别非凸折点。

    连续 LP（删除模式互斥）的可行集是凸的，其费用-排放投影本应凸。
    若非支配点的"费用随排放增加而下降"曲线在中间点高于两端点连线，
    则中间点无法由两端方案的凸组合实现：这就是离散充放模式造成的
    非凸性。加权和在这里会给出不可达的费用，因此不能代替逐上限求解。

    注意：只有点集完整（至少是登记上限的最优点，且对同一情景独立求解）
    时才可判断；任何被支配点都不参与凸包计算。
    """
    ordered = sorted(pareto)
    result = []
    for i in range(1, len(ordered) - 1):
        e0, c0 = ordered[i - 1]
        e1, c1 = ordered[i]
        e2, c2 = ordered[i + 1]
        if e2 == e0:
            continue
        chord = c0 + (c2 - c0) * (e1 - e0) / (e2 - e0)
        if c1 > chord:
            result.append({
                "emission": text(e1), "cost": text(c1),
                "chord_cost": text(chord),
                "excess_cost": text(c1 - chord),
                "neighbors": [
                    {"emission": text(e0), "cost": text(c0)},
                    {"emission": text(e2), "cost": text(c2)},
                ],
            })
    return result


def hull_report(scenario: Scenario, model: Model | None = None) -> dict:
    """对一个情景的全部上限穷举模式，报告可达点、Pareto 集与非凸点。"""
    model = model or Model(scenario)
    if 3 ** model.n > 3000:
        raise ValueError(f"3^{model.n} 超过枚举阈值")
    caps = list(scenario.emission_caps)
    all_points = []
    per_cap = {}
    # 逐上限登记点：由求解器在给定 cap 下的费用最优解给出（与前沿输出一致）
    for cap in caps:
        result = solve_cap(model, cap)
        entry = {
            "status": result.status,
            "optimality_proven": result.optimality_proven,
            "cost": None if result.cost is None else text(result.cost),
            "emission": None if result.emission is None else text(result.emission),
            "throughput": None if result.throughput is None else text(result.throughput),
            "lower_bound": None if result.lower_bound is None else text(result.lower_bound),
            "gap": None if result.gap is None else text(result.gap),
            "relaxation_gap": None if result.relaxation_gap is None else text(result.relaxation_gap),
            "relaxed_simultaneous_charge_discharge": result.detail.get(
                "relaxed_simultaneous_charge_discharge"),
        }
        if result.status == "feasible":
            all_points.append((result.emission, result.cost))
        per_cap[text(cap)] = entry

    # 小日历：穷举全部模式组合，展示同一上限下不同模式造成的离散点集
    enumeration = None
    if 3 ** model.n <= 3000:
        enumeration = {}
        for cap in caps:
            item = enumerate_modes(model, cap)
            pairs = sorted({(Fraction(e["emission"]), Fraction(e["cost"]))
                            for e in item["entries"] if e["status"] == "optimal"})
            enumeration[text(cap)] = {
                "mode_patterns_evaluated": item["modes_evaluated"],
                "distinct_objective_pairs": [{"emission": text(a), "cost": text(b)}
                                             for a, b in pairs],
                "pareto": [{"emission": text(a), "cost": text(b)}
                           for a, b in _pareto(pairs)],
            }

    # 全部可达 (排放, 最小费用) 对：只有小日历能完整穷举；
    # 大日历只报告登记上限的最优点，明确标注不是连续前沿。
    reachable_pairs = None
    if enumeration is not None:
        reachable_pairs = sorted(
            {(Fraction(pair["emission"]), Fraction(pair["cost"]))
             for entry in enumeration.values() for pair in entry["distinct_objective_pairs"]}
        )
    minimum = minimum_emission(model)
    maximum = max_emission(model, None)
    overall_pareto = _pareto(all_points)
    return {
        "scenario_id": scenario.scenario_id,
        "explanation": EXPLANATIONS.get(scenario.scenario_id, ""),
        "periods": len(scenario.periods),
        "emission_caps": [text(c) for c in caps],
        "minimum_emission": None if minimum.emission is None else text(minimum.emission),
        "minimum_emission_optimality_proven": minimum.optimality_proven,
        "maximum_emission_reference": None if maximum.emission is None else text(maximum.emission),
        "per_cap": per_cap,
        "enumeration": enumeration,
        "enumerated_reachable_pairs": None if reachable_pairs is None else [
            {"emission": text(e), "cost": text(c)} for e, c in reachable_pairs
        ],
        "overall_pareto": [{"emission": text(e), "cost": text(c)} for e, c in overall_pareto],
        "nonconvex_cap_points": _nonconvex_points(overall_pareto),
        "nonconvex_attainable_points": (
            None if reachable_pairs is None else
            _nonconvex_points(_pareto(sorted(reachable_pairs)))
        ),
        "convexity_caveat": (
            "nonconvex_cap_points 只使用登记的离散上限，是业务可核对的最小证据；"
            "nonconvex_attainable_points 基于可穷举模式得到的完整可达点集，"
            "是更强的凸性判断。两者都不代表连续前沿已找全。"
        ),
    }


def build_and_report(directory: Path = DEMO_DIR) -> dict:
    paths = build(directory)
    report = {}
    for name, path in sorted(paths.items()):
        scenario = read_scenario(path)
        report[name] = hull_report(scenario)
    return report


if __name__ == "__main__":
    print(json.dumps(build_and_report(), ensure_ascii=False, indent=2))
