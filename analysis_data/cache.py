"""结果缓存：键绑定到时间格边界、合同与情景摘要。

缓存键包含原始情景文件摘要、时间格资产摘要、逐格边界（start 与 hours）、
电池与合同参数、排放上限清单和求解器限制。任何一项变化都会使旧结果失效，
避免把不同格边界或不同合同下的结果混用。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .numeric import text
from .scenario import Scenario


def scenario_cache_key(scenario: Scenario, raw_scenario_bytes: bytes, raw_periods_bytes: bytes,
                       purpose: str) -> str:
    boundaries = [
        {"id": p.id, "start": p.start.isoformat(), "hours": text(p.hours)}
        for p in scenario.periods
    ]
    payload = {
        "purpose": purpose,
        "scenario_file_sha256": hashlib.sha256(raw_scenario_bytes).hexdigest(),
        "periods_asset_sha256": hashlib.sha256(raw_periods_bytes).hexdigest(),
        "period_boundaries": boundaries,
        "battery": {
            "capacity": text(scenario.battery.capacity),
            "initial_soc": text(scenario.battery.initial_soc),
            "terminal_soc": text(scenario.battery.terminal_soc),
            "charge_efficiency": text(scenario.battery.charge_efficiency),
            "discharge_efficiency": text(scenario.battery.discharge_efficiency),
            "hourly_retention": text(scenario.battery.hourly_retention),
            "throughput_fee": text(scenario.battery.throughput_fee),
        },
        "demand_fees": {k: text(v) for k, v in scenario.demand_fees.items()},
        "emission_caps": [text(c) for c in scenario.emission_caps],
        "solver": {
            "max_steps": scenario.solver.max_steps,
            "relative_gap": text(scenario.solver.relative_gap),
            "time_limit_seconds": scenario.solver.time_limit_seconds,
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf8")).hexdigest()


@dataclass
class ResultCache:
    directory: Path

    def __post_init__(self):
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf8"))

    def put(self, key: str, value: Any) -> Path:
        path = self._path(key)
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        path.write_text(payload, encoding="utf8")
        return path


def read_asset_bytes(scenario_path: str | Path) -> tuple[bytes, bytes]:
    """返回（情景配置原始字节，时间格资产原始字节）。"""
    path = Path(scenario_path)
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf8"))
    ref = value["periods"]
    periods_path = (path.parent / ref["path"]).resolve()
    return raw, periods_path.read_bytes()
