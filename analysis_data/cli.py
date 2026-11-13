"""命令行入口。

用法：
  python -m analysis_data.cli inspect  <scenario.json>
  python -m analysis_data.cli frontier <scenario.json> [--cache DIR] [--json]
  python -m analysis_data.cli verify   <scenario.json> <schedule.json>
  python -m analysis_data.cli demo     [--dir demo_data]

``inspect`` 只展示资料形状；``frontier`` 按业务登记的排放上限逐点求解并
输出可行性、费用、下界、间隙、非支配点与逐格重放校验；``verify`` 独立
重放一份调度；``demo`` 生成并分析演示资料。所有命令均为离线输出。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cache import ResultCache, read_asset_bytes, scenario_cache_key
from .frontier import run_frontier
from .model import Model, Schedule, SchedulePeriod
from .scenario import read_scenario, scenario_summary
from .verify import verify


def _load_raw(path):
    from .io import load
    return load(path)


def cmd_inspect(args: argparse.Namespace) -> int:
    value, digest = _load_raw(args.file)
    scenario = read_scenario(args.file)
    print(json.dumps({
        "manifest_sha256": digest,
        "summary": scenario_summary(scenario),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_frontier(args: argparse.Namespace) -> int:
    raw_scenario, raw_periods = read_asset_bytes(args.file)
    scenario = read_scenario(args.file)
    key = scenario_cache_key(scenario, raw_scenario, raw_periods, "frontier")
    cache = ResultCache(args.cache) if args.cache else None
    cached = cache.get(key) if cache else None
    if cached is not None and not args.refresh:
        payload = cached
        payload["cache"] = {"key": key, "hit": True}
    else:
        frontier = run_frontier(scenario)
        payload = frontier.to_dict()
        payload["cache"] = {"key": key, "hit": False}
        if cache:
            cache.put(key, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _schedule_from_dict(scenario, data: dict) -> Schedule:
    from .numeric import rational
    periods = []
    for row in data["periods"]:
        periods.append(SchedulePeriod(
            id=row["id"], hours=rational(row["hours"], "调度 hours"),
            import_energy=rational(row["import"], "调度 import"),
            charge_energy=rational(row["charge"], "调度 charge"),
            discharge_energy=rational(row["discharge"], "调度 discharge"),
            pv_used_energy=rational(row["pv_used"], "调度 pv_used"),
            soc_end=rational(row["soc_end"], "调度 soc_end"),
            mode=row.get("mode"),
        ))
    return Schedule(
        scenario=scenario,
        periods=tuple(periods),
        demand_peaks={k: rational(v, f"需量峰值 {k}") for k, v in data["demand_peaks"].items()},
        cap=None,
    )


def cmd_verify(args: argparse.Namespace) -> int:
    from .numeric import rational
    scenario = read_scenario(args.file)
    data = json.loads(Path(args.schedule).read_text(encoding="utf8"))
    schedule = _schedule_from_dict(scenario, data)
    result = verify(scenario, schedule)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import build, hull_report
    from .report import summary_report
    directory = Path(args.dir)
    written = build(directory)
    reports = {}
    for name, path in sorted(written.items()):
        scenario = read_scenario(path)
        reports[name] = {
            "business": summary_report(run_frontier(scenario)),
            "enumeration": hull_report(scenario),
        }
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analysis_data")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="查看资料摘要")
    p.add_argument("file")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("frontier", help="给定上限的费用-排放前沿")
    p.add_argument("file")
    p.add_argument("--cache", default=None)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_frontier)

    p = sub.add_parser("verify", help="独立重放校验调度")
    p.add_argument("file")
    p.add_argument("schedule")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("demo", help="生成并报告演示资料")
    p.add_argument("--dir", default="demo_data")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
