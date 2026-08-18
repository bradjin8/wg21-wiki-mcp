#!/usr/bin/env python3
"""Run mypy against ``src/`` using the project venv when present."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _venv_python() -> Path | None:
    for relative in (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python"):
        candidate = _ROOT / relative
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    """Run mypy on ``src/`` and return its exit code."""
    python = _venv_python()
    executable = str(python) if python is not None else sys.executable
    return subprocess.call([executable, "-m", "mypy", "src"], cwd=_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
