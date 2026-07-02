"""Tests for scripts/check_cache_benchmark_regression.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_cache_benchmark_regression.py"
_spec = importlib.util.spec_from_file_location("check_cache_benchmark_regression", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
main = _mod.main


def _bench_json(means: dict[str, float]) -> dict:
    benchmarks = []
    for fullname, mean in means.items():
        benchmarks.append(
            {
                "fullname": fullname,
                "stats": {"mean": mean},
            },
        )
    return {"benchmarks": benchmarks}


def test_regression_gate_passes_within_threshold(tmp_path: Path) -> None:
    name = "tests/test_benchmark.py::test_benchmark_cache_count"
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_bench_json({name: 10.0})), encoding="utf-8")
    current.write_text(json.dumps(_bench_json({name: 11.0})), encoding="utf-8")

    assert main([str(current), str(baseline), "--max-regression", "0.20"]) == 0


def test_regression_gate_fails_beyond_threshold(tmp_path: Path) -> None:
    name = "tests/test_benchmark.py::test_benchmark_cache_count"
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_bench_json({name: 10.0})), encoding="utf-8")
    current.write_text(json.dumps(_bench_json({name: 13.0})), encoding="utf-8")

    assert main([str(current), str(baseline), "--max-regression", "0.20"]) == 1


def test_regression_gate_fails_on_missing_benchmark(tmp_path: Path) -> None:
    name = "tests/test_benchmark.py::test_benchmark_cache_count"
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_bench_json({name: 10.0})), encoding="utf-8")
    current.write_text(json.dumps(_bench_json({})), encoding="utf-8")

    assert main([str(current), str(baseline)]) == 1
