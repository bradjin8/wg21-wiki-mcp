#!/usr/bin/env python3
"""Fail when cache benchmark means regress more than a threshold vs baseline.

Compares pytest-benchmark ``--benchmark-json`` output against the committed
``benchmarks/cache-baseline.json``. Used in CI (see ``cache benchmark`` job).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_MAX_REGRESSION = 0.20


def _means_by_fullname(data: dict) -> dict[str, float]:
    benchmarks = data.get("benchmarks") or []
    return {entry["fullname"]: float(entry["stats"]["mean"]) for entry in benchmarks}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=Path, help="pytest-benchmark JSON from the current run")
    parser.add_argument("baseline", type=Path, help="committed baseline JSON")
    parser.add_argument(
        "--max-regression",
        type=float,
        default=_DEFAULT_MAX_REGRESSION,
        help=f"max allowed mean slowdown as a fraction (default: {_DEFAULT_MAX_REGRESSION})",
    )
    args = parser.parse_args(argv)

    current = json.loads(args.current.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    cur_means = _means_by_fullname(current)
    base_means = _means_by_fullname(baseline)

    regressions: list[str] = []
    for name, base_mean in sorted(base_means.items()):
        cur_mean = cur_means.get(name)
        if cur_mean is None:
            regressions.append(f"{name}: missing from current run")
            continue
        if base_mean <= 0:
            continue
        delta = (cur_mean - base_mean) / base_mean
        if delta > args.max_regression:
            regressions.append(
                f"{name}: mean {cur_mean:.6f}s vs baseline {base_mean:.6f}s (+{delta * 100:.1f}%)",
            )

    if regressions:
        for line in regressions:
            print(f"REGRESSION: {line}", file=sys.stderr)
        return 1

    print(f"OK: all benchmarks within {args.max_regression * 100:.0f}% mean regression threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
