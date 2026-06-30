#!/usr/bin/env python3
"""Fail when the latest mwclient PyPI release exceeds a maximum age.

Used in CI to surface supply-chain staleness (see docs/DEPENDENCY-RISK.md).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

_DEFAULT_MAX_AGE_DAYS = 365
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=_DEFAULT_MAX_AGE_DAYS,
        help=f"fail when the latest release is older than this (default: {_DEFAULT_MAX_AGE_DAYS})",
    )
    args = parser.parse_args(argv)

    version, released = latest_release_date()
    now = datetime.now(timezone.utc)
    age_days = (now - released).days

    print(f"mwclient {version} released {released.date().isoformat()} ({age_days} days ago)")

    if age_days > args.max_age_days:
        print(
            f"ERROR: latest mwclient PyPI release is older than {args.max_age_days} days. "
            "See docs/DEPENDENCY-RISK.md for risk assessment and succession plan.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: release age within {args.max_age_days}-day threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
