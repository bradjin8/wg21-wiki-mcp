#!/usr/bin/env python3
"""Fail when the latest mwclient PyPI release exceeds a maximum age.

Used in CI to surface supply-chain staleness (see docs/DEPENDENCY-RISK.md).
The documented succession plan distinguishes a 12-month *review* signal
(advisory in CI) from a 24-month *migration* hard trigger (blocking).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

_DOCUMENTED_REVIEW_AGE_DAYS = 365
_DOCUMENTED_HARD_AGE_DAYS = 730  # 24 months
_PYPI_URL = "https://pypi.org/pypi/mwclient/json"


def _non_yanked_upload_times(files: list[dict]) -> list[str]:
    return [f["upload_time"] for f in files if not f.get("yanked")]


def latest_release_date() -> tuple[str, datetime]:
    """Return (version, upload_time) for the newest non-yanked mwclient release."""
    try:
        with urllib.request.urlopen(_PYPI_URL, timeout=30) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not fetch mwclient metadata from PyPI: {exc}") from exc

    releases: dict[str, list[dict]] = data.get("releases") or {}
    candidates: list[tuple[str, str]] = []
    for version, files in releases.items():
        upload_times = _non_yanked_upload_times(files)
        if upload_times:
            candidates.append((version, max(upload_times)))

    if not candidates:
        raise RuntimeError("mwclient has no non-yanked release files on PyPI")

    version, upload_time = max(candidates, key=lambda item: item[1])
    released = datetime.fromisoformat(upload_time.replace("Z", "+00:00"))
    if released.tzinfo is None:
        released = released.replace(tzinfo=timezone.utc)
    return version, released


def _failure_message(
    *,
    fail_reason: str,
    age_days: int,
    max_age_days: int,
) -> str:
    if fail_reason == "review":
        if age_days > _DOCUMENTED_REVIEW_AGE_DAYS:
            return (
                f"REVIEW TRIGGER: latest mwclient PyPI release is older than "
                f"{max_age_days} days ({_DOCUMENTED_REVIEW_AGE_DAYS}-day quarterly review "
                "threshold per docs/DEPENDENCY-RISK.md). "
                "This step is advisory; schedule succession-plan review."
            )
        return (
            f"GATE MISCONFIGURED: release age {age_days} days exceeds custom "
            f"--max-age-days {max_age_days} but not the documented "
            f"{_DOCUMENTED_REVIEW_AGE_DAYS}-day review trigger. "
            "Lowered thresholds are for gate verification only."
        )

    if age_days > _DOCUMENTED_HARD_AGE_DAYS:
        return (
            f"HARD TRIGGER: latest mwclient PyPI release is older than "
            f"{max_age_days} days (documented 24-month migration boundary). "
            "Evaluate fork or requests-only WikiClient rewrite per "
            "docs/DEPENDENCY-RISK.md."
        )

    return (
        f"GATE MISCONFIGURED: release age {age_days} days exceeds custom "
        f"--max-age-days {max_age_days} but not the documented "
        f"{_DOCUMENTED_HARD_AGE_DAYS}-day hard trigger. "
        "Lowered thresholds are for gate verification only."
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=_DOCUMENTED_HARD_AGE_DAYS,
        help=(
            "fail when the latest release is older than this "
            f"(default: {_DOCUMENTED_HARD_AGE_DAYS}, the documented hard trigger)"
        ),
    )
    parser.add_argument(
        "--fail-reason",
        choices=("review", "hard"),
        default="hard",
        help=(
            "failure classification for log output: "
            "'review' for the 12-month advisory signal, "
            "'hard' for the 24-month migration gate"
        ),
    )
    args = parser.parse_args(argv)

    version, released = latest_release_date()
    now = datetime.now(timezone.utc)
    age_days = (now - released).days

    print(f"mwclient {version} released {released.date().isoformat()} ({age_days} days ago)")

    if age_days > args.max_age_days:
        message = _failure_message(
            fail_reason=args.fail_reason,
            age_days=age_days,
            max_age_days=args.max_age_days,
        )
        print(message, file=sys.stderr)
        print("See docs/DEPENDENCY-RISK.md for risk assessment and succession plan.", file=sys.stderr)
        return 1

    print(f"OK: release age within {args.max_age_days}-day threshold ({args.fail_reason} gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
