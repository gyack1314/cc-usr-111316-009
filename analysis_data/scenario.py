"""冻结情景的严格读取、形状核对与摘要。

读取器做两件事：核验文件身份，并把资料拆成不可变的 ``Scenario``。
它**不**替换缺失数值、不把 ``null`` 当 0、不做单位换算，也不替分析者
猜测"通常正电价"之类的业务假设。不可识别、无界或不适用之处在读取阶段
就报错，而不是在求解阶段悄悄变成别的模型。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from .io import load as load_json_bytes
from .numeric import rational, nonnegative, positive, in_unit_interval, integer, NumericError


class ShapeError(ValueError):
    """资料形状或业务前提不成立。"""


@dataclass(frozen=True)
class Period:
    id: str
    start: datetime
    hours: "Fraction"
    load: "Fraction"
    pv: "Fraction"
    import_limit: "Fraction"
    charge_limit: "Fraction"
    discharge_limit: "Fraction"
    price: "Fraction"
    emission_factor: "Fraction"
    billing_period: str


@dataclass(frozen=True)
class Battery:
    capacity: "Fraction"
    initial_soc: "Fraction"
    terminal_soc: "Fraction"
    charge_efficiency: "Fraction"
    discharge_efficiency: "Fraction"
    hourly_retention: "Fraction"
    throughput_fee: "Fraction"


@dataclass(frozen=True)
class SolverLimits:
    max_steps: int
    relative_gap: "Fraction"
    time_limit_seconds: int


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    periods: Sequence[Period]
    battery: Battery
    demand_fees: "dict[str, Fraction]"
    emission_caps: Sequence["Fraction"]
    solver: SolverLimits


# ---------------------------------------------------------------- 解析辅助

def _parse_time(text: str, name: str) -> datetime:
    if not isinstance(text, str) or not text:
        raise ShapeError(f"{name} 必须是时间字符串")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShapeError(f"{name} 不是 ISO 8601 时间：{text!r}") from exc


def _hours_since_previous(previous: datetime, current: datetime) -> "Fraction":
    """两个时刻之间的确切小时数（有理数）。"""
    delta = current - previous
    seconds = delta.days * 86400 + delta.seconds
    if delta.microseconds:
        raise ShapeError("时间格边界必须落在整秒上")
    return rational(f"{seconds}/3600")


def _field(row: dict, key: str, name: str, required: bool = True):
    if key not in row:
        if not required:
            return None
        raise ShapeError(f"{name} 缺少字段 {key!r}")
    return row[key]


def _period_from_row(row: dict, index: int) -> Period:
    name = f"第 {index} 行"
    try:
        pid = _field(row, "id", name)
        if not isinstance(pid, str) or not pid:
            raise ShapeError(f"{name} 的 id 必须是非空字符串")
        return Period(
            id=pid,
            start=_parse_time(_field(row, "start", name), f"{name} 的 start"),
            hours=positive(_field(row, "hours", name), f"{name} 的 hours"),
            load=nonnegative(_field(row, "load", name), f"{name} 的 load"),
            pv=nonnegative(_field(row, "pv", name), f"{name} 的 pv"),
            import_limit=nonnegative(_field(row, "import_limit", name), f"{name} 的 import_limit"),
            charge_limit=nonnegative(_field(row, "charge_limit", name), f"{name} 的 charge_limit"),
            discharge_limit=nonnegative(_field(row, "discharge_limit", name), f"{name} 的 discharge_limit"),
            price=rational(_field(row, "price", name), f"{name} 的 price"),
            emission_factor=nonnegative(_field(row, "emission_factor", name), f"{name} 的 emission_factor"),
            billing_period=_billing_label(_field(row, "billing_period", name), name),
        )
    except NumericError as exc:
        raise ShapeError(f"{name}：{exc}") from exc


def _billing_label(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ShapeError(f"{name} 的 billing_period 必须是非空字符串")
    return value


def load_periods(raw: bytes, expected_rows: int | None = None) -> list[Period]:
    """解析 JSONL 时间格，保证身份唯一、时间严格递增且覆盖完整。"""
    rows = []
    for lineno, line in enumerate(raw.decode("utf8").splitlines(), start=1):
        if line.strip():
            import json
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ShapeError(f"时间格第 {lineno} 行不是 JSON") from exc
            if not isinstance(row, dict):
                raise ShapeError(f"时间格第 {lineno} 行必须是对象")
            rows.append((lineno, row))
    if not rows:
        raise ShapeError("时间格文件为空")
    periods = [_period_from_row(row, lineno) for lineno, row in rows]
    seen = set()
    for p in periods:
        if p.id in seen:
            raise ShapeError(f"时间格 id 重复：{p.id}")
        seen.add(p.id)
    for earlier, later in zip(periods, periods[1:]):
        if later.start <= earlier.start:
            raise ShapeError(f"时间格顺序不递增：{earlier.id} -> {later.id}")
    if expected_rows is not None and len(periods) != expected_rows:
        raise ShapeError(f"声明 {expected_rows} 行，文件中为 {len(periods)} 行")
    return periods


def load_battery(obj: dict) -> Battery:
    if not isinstance(obj, dict):
        raise ShapeError("battery 必须是对象")
    try:
        battery = Battery(
            capacity=positive(obj.get("capacity"), "电池容量"),
            initial_soc=nonnegative(obj.get("initial_soc"), "初始 SOC"),
            terminal_soc=nonnegative(obj.get("terminal_soc"), "末态 SOC"),
            charge_efficiency=in_unit_interval(obj.get("charge_efficiency"), "充电效率"),
            discharge_efficiency=in_unit_interval(obj.get("discharge_efficiency"), "放电效率"),
            hourly_retention=in_unit_interval(obj.get("hourly_retention"), "小时保留系数"),
            throughput_fee=nonnegative(obj.get("throughput_fee"), "吞吐衰减费"),
        )
    except NumericError as exc:
        raise ShapeError(str(exc)) from exc
    if battery.initial_soc > battery.capacity:
        raise ShapeError("初始 SOC 超过容量")
    if battery.terminal_soc > battery.capacity:
        raise ShapeError("末态 SOC 超过容量")
    return battery


def load_demand_fees(obj, billing_labels: set[str]) -> dict[str, "Fraction"]:
    if not isinstance(obj, dict) or not obj:
        raise ShapeError("demand_fees 必须是非空对象")
    fees = {}
    for label, value in obj.items():
        if not isinstance(label, str) or not label:
            raise ShapeError("计费周期名必须是非空字符串")
        try:
            fees[label] = nonnegative(value, f"计费周期 {label} 的需量费率")
        except NumericError as exc:
            raise ShapeError(str(exc)) from exc
    missing = billing_labels - set(fees)
    if missing:
        raise ShapeError(f"时间格使用了未登记需量费率的计费周期：{sorted(missing)}")
    return fees


def load_solver(obj) -> SolverLimits:
    if not isinstance(obj, dict):
        raise ShapeError("solver 必须是对象")
    try:
        return SolverLimits(
            max_steps=integer(obj.get("max_steps"), "max_steps", 1),
            relative_gap=nonnegative(obj.get("relative_gap"), "relative_gap"),
            time_limit_seconds=integer(obj.get("time_limit_seconds"), "time_limit_seconds", 1),
        )
    except NumericError as exc:
        raise ShapeError(str(exc)) from exc


# ---------------------------------------------------------------- 主入口

def read_scenario(path: str | Path) -> Scenario:
    root = Path(path).parent
    value, _digest = load_json_bytes(path)
    if not isinstance(value, dict) or value.get("kind") != "storage-frontier":
        raise ShapeError("kind 必须是 storage-frontier")
    scenario_id = value.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ShapeError("scenario_id 必须是非空字符串")

    ref = value.get("periods")
    if not isinstance(ref, dict):
        raise ShapeError("periods 必须是资产引用对象")
    from .io import asset
    raw = asset(root, ref)
    expected_rows = ref.get("rows")
    if expected_rows is not None:
        expected_rows = integer(expected_rows, "periods.rows", 1)
    periods = load_periods(raw, expected_rows)

    battery = load_battery(value.get("battery"))
    demand_fees = load_demand_fees(value.get("demand_fees"), {p.billing_period for p in periods})
    solver = load_solver(value.get("solver"))

    caps_obj = value.get("emission_caps")
    if not isinstance(caps_obj, list) or not caps_obj:
        raise ShapeError("emission_caps 必须是非空列表")
    caps = []
    for index, cap in enumerate(caps_obj):
        try:
            value_cap = nonnegative(cap, f"emission_caps[{index}]")
        except NumericError as exc:
            raise ShapeError(str(exc)) from exc
        caps.append(value_cap)
    if len(set(caps)) != len(caps):
        raise ShapeError("emission_caps 含重复上限")

    # 间隙语义：相邻时间格之间的空档属于"未调度停机"，但自放电按真实小时推进。
    for earlier, later in zip(periods, periods[1:]):
        gap = later.start - earlier.start
        if gap <= timedelta(0):
            raise ShapeError("时间格必须先于其后一格开始")

    return Scenario(
        scenario_id=scenario_id,
        periods=tuple(periods),
        battery=battery,
        demand_fees=demand_fees,
        emission_caps=tuple(caps),
        solver=solver,
    )


def scenario_summary(scenario: Scenario) -> dict:
    total_hours = sum((p.hours for p in scenario.periods), start=rational("0"))
    return {
        "scenario_id": scenario.scenario_id,
        "periods": len(scenario.periods),
        "scheduled_hours": str(total_hours),
        "billing_periods": sorted(scenario.demand_fees),
        "emission_caps": [str(c) for c in scenario.emission_caps],
    }
