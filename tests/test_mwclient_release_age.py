"""Offline tests for scripts/check_mwclient_release_age.py."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_mwclient_release_age.py"
_spec = importlib.util.spec_from_file_location("check_mwclient_release_age", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
latest_release_date = _mod.latest_release_date
main = _mod.main
_failure_message = _mod._failure_message


def _mock_pypi(monkeypatch: pytest.MonkeyPatch, upload_time: str) -> None:
    payload = {
        "releases": {
            "0.11.0": [{"upload_time": upload_time, "yanked": False}],
        },
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            import json

            return json.dumps(payload).encode()

    monkeypatch.setattr(_mod.urllib.request, "urlopen", lambda *a, **k: _Resp())


def test_latest_release_date_skips_fully_yanked_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """When info.version is fully yanked, use the newest non-yanked release."""
    payload = {
        "info": {"version": "9.9.9"},
        "releases": {
            "9.9.9": [{"upload_time": "2026-01-01T00:00:00", "yanked": True}],
            "0.11.0": [{"upload_time": "2024-08-12T09:08:13", "yanked": False}],
        },
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            import json

            return json.dumps(payload).encode()

    monkeypatch.setattr(_mod.urllib.request, "urlopen", lambda *a, **k: _Resp())

    version, released = latest_release_date()

    assert version == "0.11.0"
    assert released == datetime(2024, 8, 12, 9, 8, 13, tzinfo=timezone.utc)


def test_hard_gate_passes_under_documented_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _mock_pypi(monkeypatch, recent)

    assert main(["--max-age-days", "730", "--fail-reason", "hard"]) == 0


def test_review_gate_fails_over_twelve_months(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _mock_pypi(monkeypatch, old)

    assert main(["--max-age-days", "365", "--fail-reason", "review"]) == 1
    err = capsys.readouterr().err
    assert "REVIEW TRIGGER" in err
    assert "GATE MISCONFIGURED" not in err


def test_lowered_review_threshold_reports_misconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scratch-branch gate verification: sub-12-month breach is misconfiguration, not policy."""
    age_days = 200
    released = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    _mock_pypi(monkeypatch, released)

    assert main(["--max-age-days", "30", "--fail-reason", "review"]) == 1
    err = capsys.readouterr().err
    assert "GATE MISCONFIGURED" in err
    assert "REVIEW TRIGGER" not in err


def test_hard_gate_fails_over_twenty_four_months(monkeypatch: pytest.MonkeyPatch) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=800)).isoformat()
    _mock_pypi(monkeypatch, old)

    assert main(["--max-age-days", "730", "--fail-reason", "hard"]) == 1


def test_lowered_hard_threshold_reports_misconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scratch-branch gate verification: sub-24-month breach is misconfiguration, not policy."""
    age_days = 400
    released = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    _mock_pypi(monkeypatch, released)

    assert main(["--max-age-days", "30", "--fail-reason", "hard"]) == 1
    err = capsys.readouterr().err
    assert "GATE MISCONFIGURED" in err
    assert "HARD TRIGGER" not in err


def test_hard_gate_failure_message_distinguishes_policy_breach() -> None:
    msg = _failure_message(fail_reason="hard", age_days=800, max_age_days=730)
    assert "HARD TRIGGER" in msg
    assert "GATE MISCONFIGURED" not in msg


def test_review_gate_failure_message() -> None:
    msg = _failure_message(fail_reason="review", age_days=400, max_age_days=365)
    assert "REVIEW TRIGGER" in msg
    assert "GATE MISCONFIGURED" not in msg


def test_review_gate_failure_message_misconfigured() -> None:
    msg = _failure_message(fail_reason="review", age_days=200, max_age_days=30)
    assert "GATE MISCONFIGURED" in msg
    assert "REVIEW TRIGGER" not in msg
