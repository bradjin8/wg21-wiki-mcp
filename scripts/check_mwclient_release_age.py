#!/usr/bin/env python3
"""Fail when the latest mwclient PyPI release exceeds a maximum age.

Used in CI to surface supply-chain staleness (see docs/DEPENDENCY-RISK.md).
The documented succession plan distinguishes a 12-month *review* signal
(advisory in CI) from a 24-month *migration* hard trigger (blocking).

An optional dated waiver (``--waiver-file`` or ``--waiver-until``) defers the
hard gate until a linked succession decision is scheduled; the gate still fails
once the waiver expires.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

_DOCUMENTED_REVIEW_AGE_DAYS = 365
_DOCUMENTED_HARD_AGE_DAYS = 730  # 24 months
_PYPI_URL = "https://pypi.org/pypi/mwclient/json"
_WAIVER_REQUIRED_FIELDS = ("expires_on", "reason", "tracking_issue")


@dataclass(frozen=True)
class Waiver:
    """Parsed waiver artifact deferring the hard migration gate until ``expires_on``."""

    expires_on: date
    reason: str
    tracking_issue: str


def _parse_iso_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


def load_waiver_file(path: Path) -> Waiver:
    """Load and validate a committed waiver artifact."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read waiver file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse waiver file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"waiver file {path} must be a JSON object")

    missing = [key for key in _WAIVER_REQUIRED_FIELDS if key not in raw]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"waiver file {path} missing required field(s): {joined}")

    expires_on = _parse_iso_date(str(raw["expires_on"]), field="expires_on")
    reason = raw["reason"]
    tracking_issue = raw["tracking_issue"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"waiver file {path} field reason must be a non-empty string")
    if not isinstance(tracking_issue, str) or not tracking_issue.strip():
        raise ValueError(f"waiver file {path} field tracking_issue must be a non-empty string")

    return Waiver(expires_on=expires_on, reason=reason.strip(), tracking_issue=tracking_issue.strip())


def resolve_waiver(
    *,
    waiver_file: str | None,
    waiver_until: str | None,
) -> Waiver | None:
    """Return the active waiver config from CLI flags, or ``None`` when unset."""
    if waiver_file is None and waiver_until is None:
        return None
    if waiver_file is not None and waiver_until is not None:
        raise ValueError("use only one of --waiver-file or --waiver-until")

    if waiver_until is not None:
        return Waiver(
            expires_on=_parse_iso_date(waiver_until, field="--waiver-until"),
            reason="(from --waiver-until)",
            tracking_issue="(not specified)",
        )

    assert waiver_file is not None
    return load_waiver_file(Path(waiver_file))


def waiver_status(waiver: Waiver, today: date) -> str:
    """Return ``active`` or ``expired`` for ``waiver`` on ``today``."""
    if today > waiver.expires_on:
        return "expired"
    return "active"


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


def _print_waiver_bypass(waiver: Waiver, *, today: date, fail_reason: str) -> None:
    print(
        f"WAIVER: {waiver.expires_on.isoformat()} {fail_reason} gate waived "
        f"({(waiver.expires_on - today).days} day(s) remaining)"
    )
    print(f"Reason: {waiver.reason}")
    print(f"Tracking issue: {waiver.tracking_issue}")


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
    parser.add_argument(
        "--waiver-file",
        metavar="PATH",
        help="JSON waiver artifact with expires_on, reason, and tracking_issue",
    )
    parser.add_argument(
        "--waiver-until",
        metavar="YYYY-MM-DD",
        help="ISO expiry date for a scratch waiver (testing; prefer --waiver-file in CI)",
    )
    args = parser.parse_args(argv)

    try:
        waiver = resolve_waiver(waiver_file=args.waiver_file, waiver_until=args.waiver_until)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    version, released = latest_release_date()
    now = datetime.now(timezone.utc)
    today = now.date()
    age_days = (now - released).days

    print(f"mwclient {version} released {released.date().isoformat()} ({age_days} days ago)")

    if age_days > args.max_age_days:
        if waiver is not None and args.fail_reason == "hard":
            status = waiver_status(waiver, today)
            if status == "active":
                _print_waiver_bypass(waiver, today=today, fail_reason=args.fail_reason)
                print(
                    f"OK: release age exceeds {args.max_age_days}-day threshold "
                    f"but active waiver defers the {args.fail_reason} gate"
                )
                return 0

            print(
                f"WAIVER EXPIRED: waiver lapsed on {waiver.expires_on.isoformat()} "
                f"(tracking issue: {waiver.tracking_issue})",
                file=sys.stderr,
            )

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
