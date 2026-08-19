#!/usr/bin/env python3
"""Fail when a benchmark statistic regresses more than a threshold vs a committed baseline.

Compares pytest-benchmark ``--benchmark-json`` output against a committed baseline
(e.g. ``benchmarks/cache-baseline.json`` or ``benchmarks/meeting-time-baseline.json``).
Used in CI (see the ``benchmark`` job).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_MAX_REGRESSION = 0.20


def _load_benchmark_json(path: Path, label: str) -> dict:
    """Load a pytest-benchmark JSON file, raising ``SystemExit`` on I/O or parse errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"could not read {label} benchmark JSON at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {label} benchmark JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{label} benchmark JSON at {path} must be a JSON object")
    return data


def _stat_by_fullname(data: dict, stat: str) -> dict[str, float]:
    benchmarks = data.get("benchmarks") or []
    values: dict[str, float] = {}
    for index, entry in enumerate(benchmarks):
        if not isinstance(entry, dict):
            raise SystemExit(f"benchmark entry at index {index} must be a JSON object")
        try:
            fullname = entry["fullname"]
            value = float(entry["stats"][stat])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"benchmark entry at index {index} is missing fullname/stats.{stat}: {exc}") from exc
        values[str(fullname)] = value
    return values


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=Path, help="pytest-benchmark JSON from the current run")
    parser.add_argument("baseline", type=Path, help="committed baseline JSON")
    parser.add_argument(
        "--max-regression",
        type=float,
        default=_DEFAULT_MAX_REGRESSION,
        help=f"max allowed slowdown as a fraction (default: {_DEFAULT_MAX_REGRESSION})",
    )
    parser.add_argument(
        "--stat",
        choices=("mean", "median"),
        default="mean",
        help=(
            "central-tendency statistic to compare (default: mean). Use 'median' for "
            "high-variance, fat-tailed benchmarks (e.g. cold concurrent composites) where a "
            "single shared-runner spike would otherwise dominate the mean."
        ),
    )
    args = parser.parse_args(argv)

    current = _load_benchmark_json(args.current, "current")
    baseline = _load_benchmark_json(args.baseline, "baseline")
    cur_values = _stat_by_fullname(current, args.stat)
    base_values = _stat_by_fullname(baseline, args.stat)

    regressions: list[str] = []
    for name, base_value in sorted(base_values.items()):
        cur_value = cur_values.get(name)
        if cur_value is None:
            regressions.append(f"{name}: missing from current run")
            continue
        if base_value <= 0:
            continue
        delta = (cur_value - base_value) / base_value
        if delta > args.max_regression:
            regressions.append(
                f"{name}: {args.stat} {cur_value:.6f}s vs baseline {base_value:.6f}s (+{delta * 100:.1f}%)",
            )

    if regressions:
        for line in regressions:
            print(f"REGRESSION: {line}", file=sys.stderr)
        return 1

    print(f"OK: all benchmarks within {args.max_regression * 100:.0f}% {args.stat} regression threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
