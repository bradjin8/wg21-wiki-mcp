"""Offline tests for scripts/check_mwclient_release_age.py."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_mwclient_release_age.py"
_spec = importlib.util.spec_from_file_location("check_mwclient_release_age", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
latest_release_date = _mod.latest_release_date


def test_latest_release_date_skips_fully_yanked_version(monkeypatch):
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
