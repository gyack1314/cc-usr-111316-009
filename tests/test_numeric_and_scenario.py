"""精确有理数、资料读取与身份校验的测试。"""
import hashlib
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from analysis_data.io import load, asset, integer, unique
from analysis_data.numeric import rational, text, NumericError
from analysis_data.scenario import (
    read_scenario, load_periods, ShapeError, scenario_summary,
)
from analysis_data.domain import summarize


class NumericTests(unittest.TestCase):
    def test_rational_forms(self):
        self.assertEqual(rational("1/2"), Fraction(1, 2))
        self.assertEqual(rational("-3/4"), Fraction(-3, 4))
        self.assertEqual(rational("0.25"), Fraction(1, 4))
        self.assertEqual(rational("12"), Fraction(12))
        self.assertEqual(rational(7), Fraction(7))

    def test_rejects_bad_forms(self):
        for value in (True, 1.5, None, [], {}, "", "abc", "1/0"):
            with self.subTest(value=value):
                with self.assertRaises(NumericError):
                    rational(value)

    def test_text_round_trip(self):
        self.assertEqual(text(Fraction(-8, 7)), "-8/7")
        self.assertEqual(text(Fraction(4)), "4")


class MaterialTests(unittest.TestCase):
    def test_normal(self):
        value, digest = load("data/normal.json")
        self.assertEqual(len(digest), 64)
        self.assertIsInstance(summarize(value, Path("data")), dict)

    def test_invalid(self):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            summarize(load("data/invalid-config.json")[0], Path("data"))

    def test_boundary_materials(self):
        for path in Path("data").glob("*.json"):
            if path.stem == "invalid-config":
                continue
            with self.subTest(file=path.name):
                self.assertIsInstance(summarize(load(path)[0], path.parent), dict)

    def test_numeric(self):
        for value in (True, -1, 1.2):
            with self.assertRaises(ValueError):
                integer(value)
        self.assertEqual(integer(3, 3), 3)

    def test_identity_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x"
            path.write_bytes(b"abc")
            ref = {"path": "x", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
            self.assertEqual(asset(directory, ref), b"abc")
            for bad in ({**ref, "path": "../outside"}, {**ref, "sha256": "wrong"},
                        {**ref, "bytes": 4}):
                with self.assertRaises(ValueError):
                    asset(directory, bad)
        with self.assertRaises(ValueError):
            unique([{"id": "a"}, {"id": "a"}], "id")

    def test_json_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "资料.json"
            for content in ("[]", "null", '{"n": Infinity}'):
                path.write_text(content, encoding="utf8")
                with self.assertRaises(ValueError):
                    load(path)


class ScenarioReaderTests(unittest.TestCase):
    def test_normal_scenario(self):
        scenario = read_scenario("data/normal.json")
        self.assertEqual(len(scenario.periods), 2)
        self.assertEqual(scenario.battery.capacity, Fraction(1))
        summary = scenario_summary(scenario)
        self.assertEqual(summary["periods"], 2)

    def test_invalid_battery_capacity(self):
        with self.assertRaises(ShapeError):
            read_scenario("data/invalid-config.json")

    def test_negative_price_fixture(self):
        scenario = read_scenario("data/negative-price-loss.json")
        self.assertEqual(scenario.periods[0].price, Fraction(-1))
        self.assertEqual(scenario.battery.charge_efficiency, Fraction(1, 2))

    def test_cross_billing_fixture(self):
        scenario = read_scenario("data/cross-billing-period.json")
        self.assertEqual(scenario.demand_fees, {"jan": Fraction(2), "feb": Fraction(5)})

    def test_period_order_and_ids(self):
        rows = (
            b'{"id":"t1","start":"2026-01-01T00:00:00Z","hours":"1","load":"1","pv":"0",'
            b'"import_limit":"2","charge_limit":"1","discharge_limit":"1","price":"1",'
            b'"emission_factor":"1","billing_period":"a"}\n'
        )
        periods = load_periods(rows, 1)
        self.assertEqual(periods[0].id, "t1")
        with self.assertRaises(ShapeError):
            load_periods(rows, 2)

    def test_duplicate_ids_rejected(self):
        line = (
            b'{"id":"t1","start":"2026-01-01T00:00:00Z","hours":"1","load":"1","pv":"0",'
            b'"import_limit":"2","charge_limit":"1","discharge_limit":"1","price":"1",'
            b'"emission_factor":"1","billing_period":"a"}\n'
        )
        with self.assertRaises(ShapeError):
            load_periods(line + line.replace(b"00:00:00Z", b"01:00:00Z"), 2)

    def test_missing_demand_fee_period(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = Path(directory) / "p.jsonl"
            rows.write_text(
                '{"id":"t1","start":"2026-01-01T00:00:00Z","hours":"1","load":"1",'
                '"pv":"0","import_limit":"2","charge_limit":"1","discharge_limit":"1",'
                '"price":"1","emission_factor":"1","billing_period":"unknown"}\n',
                encoding="utf8",
            )
            raw = rows.read_bytes()
            config = {
                "kind": "storage-frontier", "scenario_id": "x",
                "periods": {"path": "p.jsonl", "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(), "rows": 1},
                "battery": {"capacity": "1", "initial_soc": "0", "terminal_soc": "0",
                            "charge_efficiency": "1", "discharge_efficiency": "1",
                            "hourly_retention": "1", "throughput_fee": "0"},
                "demand_fees": {"a": "0"}, "emission_caps": ["1"],
                "solver": {"max_steps": 100, "relative_gap": "0", "time_limit_seconds": 5},
            }
            path = Path(directory) / "c.json"
            path.write_text(json.dumps(config), encoding="utf8")
            with self.assertRaises(ShapeError):
                read_scenario(path)


if __name__ == "__main__":
    unittest.main()
