"""Tests for generated API reference (docs/API.md)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_MD = _REPO_ROOT / "docs" / "API.md"
_BUILD_SCRIPT = _REPO_ROOT / "scripts" / "build_api_docs.py"

_spec = importlib.util.spec_from_file_location("build_api_docs", _BUILD_SCRIPT)
assert _spec and _spec.loader
_build_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _build_mod
_spec.loader.exec_module(_build_mod)
EXPECTED_TOOLS = _build_mod.EXPECTED_TOOLS
render_api_md = _build_mod.render_api_md


def test_render_includes_all_mcp_tools():
    md = render_api_md()
    for name in sorted(EXPECTED_TOOLS):
        assert f"## `{name}`" in md, f"Missing tool section for {name}"


def test_render_marks_generated_banner():
    md = render_api_md()
    assert "Generated from source" in md
    assert "python scripts/build_api_docs.py" in md


def test_api_md_matches_generator():
    """Committed docs/API.md must match the generator (CI drift gate)."""
    if not _API_MD.is_file():
        pytest.skip("docs/API.md not present")
    generated = render_api_md()
    existing = _API_MD.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not existing.endswith("\n"):
        existing += "\n"
    assert existing == generated


def test_build_script_check_mode():
    result = subprocess.run(
        [sys.executable, str(_BUILD_SCRIPT), "--check"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
