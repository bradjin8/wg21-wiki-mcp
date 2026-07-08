#!/usr/bin/env python3
"""Generate ``docs/API.md`` from MCP tool definitions and response models.

The MCP tool table is built by introspecting ``@mcp.tool()`` handlers in
``server.py`` (names, docstrings, and type hints). Return-type summaries come
from Pydantic model docstrings in ``models.py``.

Usage::

    python scripts/build_api_docs.py          # write docs/API.md
    python scripts/build_api_docs.py --check  # exit 1 when the file is stale
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from wg21_wiki_mcp import models
from wg21_wiki_mcp.server import mcp

EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        "search_wiki",
        "get_page",
        "list_pages",
        "list_namespaces",
        "list_meetings",
        "get_meeting_overview",
        "get_meeting_sessions",
        "get_recent_changes",
        "wiki_status",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "API.md"


def _escape_table_cell(text: str) -> str:
    """Escape pipe characters so markdown table rows stay valid."""
    return text.replace("|", "\\|")


def _type_name(annotation: Any) -> str:
    """Render a type annotation as a concise, markdown-safe string."""
    if annotation is inspect.Parameter.empty:
        return ""
    if annotation is type(None):
        return "None"
    if isinstance(annotation, str):
        return _escape_table_cell(annotation)
    if isinstance(annotation, type):
        return annotation.__name__
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        inner = _type_name(args[0]) if args else "Any"
        return f"list[{inner}]"
    if origin is Union or isinstance(annotation, types.UnionType):
        parts = [_type_name(arg) for arg in get_args(annotation)]
        return _escape_table_cell(" | ".join(parts))
    if origin is dict:
        key_t, val_t = get_args(annotation) or (Any, Any)
        return f"dict[{_type_name(key_t)}, {_type_name(val_t)}]"
    if origin is Callable:
        return "Callable"
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return _escape_table_cell(str(annotation).removeprefix("typing."))


def _default_repr(value: Any) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    return repr(value)


def _first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    text = inspect.cleandoc(doc)
    return text.split("\n\n", maxsplit=1)[0].replace("\n", " ")


def _return_summary(return_type: Any) -> str:
    """One-line summary for a tool return type."""
    origin = get_origin(return_type)
    if origin is list:
        args = get_args(return_type)
        if args:
            return _model_summary(args[0])
        return "list"
    return _model_summary(return_type)


def _model_summary(model_type: Any) -> str:
    if not isinstance(model_type, type):
        return _type_name(model_type)
    doc = _first_paragraph(model_type.__doc__)
    name = model_type.__name__
    if doc:
        return f"`{name}` — {doc}"
    return f"`{name}`"


def _param_descriptions(doc: str) -> dict[str, str]:
    """Parse Google-style ``Args:`` / ``Parameters:`` blocks from a tool docstring."""
    text = inspect.cleandoc(doc)
    lines = text.splitlines()
    section_start: int | None = None
    for idx, line in enumerate(lines):
        header = line.strip().rstrip(":")
        if header in {"Args", "Arguments", "Parameters"}:
            section_start = idx + 1
            break
    if section_start is None:
        return {}

    descriptions: dict[str, str] = {}
    for line in lines[section_start:]:
        if not line.strip():
            if descriptions:
                break
            continue
        if line.endswith(":") and not line.startswith(" "):
            break
        stripped = line.strip()
        if ":" not in stripped:
            continue
        name, _, body = stripped.partition(":")
        name = name.strip()
        if not name:
            continue
        descriptions[name] = body.strip()
    return descriptions


def _tool_sections() -> list[str]:
    import wg21_wiki_mcp.server as server_mod

    listed = asyncio.run(mcp.list_tools())
    found = {tool.name for tool in listed}
    missing = EXPECTED_TOOLS - found
    extra = found - EXPECTED_TOOLS
    if missing:
        raise RuntimeError(f"Missing MCP tools in server registration: {sorted(missing)}")
    if extra:
        raise RuntimeError(f"Unexpected MCP tools (update EXPECTED_TOOLS): {sorted(extra)}")

    sections: list[str] = []
    for tool in sorted(listed, key=lambda t: t.name):
        fn = getattr(server_mod, tool.name, None)
        if fn is None or not callable(fn):
            raise RuntimeError(f"No Python function for MCP tool {tool.name!r}")

        doc = inspect.getdoc(fn)
        if not doc:
            raise RuntimeError(f"MCP tool {tool.name!r} is missing a docstring")

        hints = get_type_hints(fn)
        return_type = hints.get("return", inspect.Signature.empty)
        if return_type is inspect.Signature.empty:
            raise RuntimeError(f"MCP tool {tool.name!r} is missing a return type annotation")

        lines = [f"## `{tool.name}`", "", _first_paragraph(doc), ""]
        param_docs = _param_descriptions(doc)
        sig = inspect.signature(fn)
        if not sig.parameters:
            lines.extend(["**Parameters:** none.", ""])
        else:
            if param_docs:
                lines.extend(
                    [
                        "| Parameter | Type | Default | Description |",
                        "| --- | --- | --- | --- |",
                    ]
                )
            else:
                lines.extend(
                    [
                        "| Parameter | Type | Default |",
                        "| --- | --- | --- |",
                    ]
                )
            for param_name, param in sig.parameters.items():
                if isinstance(param.annotation, str):
                    # Prefer deferred annotation strings from source; get_type_hints()
                    # can lose generic args on Python 3.10 (e.g. list[str] -> list).
                    type_str = _escape_table_cell(param.annotation)
                else:
                    type_str = _type_name(hints.get(param_name, param.annotation))
                if not type_str:
                    type_str = "Any"
                if param.default is inspect.Parameter.empty:
                    default_str = "required"
                else:
                    default_str = f"`{_default_repr(param.default)}`"
                if param_docs:
                    description = param_docs.get(param_name, "")
                    lines.append(f"| `{param_name}` | `{type_str}` | {default_str} | {description} |")
                else:
                    lines.append(f"| `{param_name}` | `{type_str}` | {default_str} |")
            lines.append("")

        lines.append(f"**Returns:** {_return_summary(return_type)}")
        lines.append("")
        sections.append("\n".join(lines))
    return sections


def _models_section() -> str:
    """Document public response models declared in ``models.__all__``."""
    model_names = [name for name in models.__all__ if hasattr(models, name)]
    classes = [getattr(models, name) for name in model_names]
    model_classes = [cls for cls in classes if isinstance(cls, type) and issubclass(cls, BaseModel)]
    if not model_classes:
        return ""

    lines = [
        "## Response models",
        "",
        "Structured tool outputs are Pydantic models in [models.py](../src/wg21_wiki_mcp/models.py). Public models:",
        "",
    ]
    for cls in sorted(model_classes, key=lambda c: c.__name__):
        doc = _first_paragraph(cls.__doc__) or "No description."
        lines.append(f"- **`{cls.__name__}`** — {doc}")
    lines.append("")
    return "\n".join(lines)


def render_api_md() -> str:
    """Return the full generated API reference markdown."""
    tool_blocks = _tool_sections()
    header = """# MCP tool API reference

> **Generated from source.** Do not edit this file manually.
> Regenerate with: `python scripts/build_api_docs.py`

Stable MCP host contract for `wg21-wiki-mcp`. Tool names, parameter
names/types, error codes, and provenance fields are governed by
[STABILITY.md](../STABILITY.md). See [CHANGELOG.md](../CHANGELOG.md) for release
version history. FastMCP also exposes JSON schemas to MCP clients at runtime.

All tools return structured Pydantic models (see [models.py](../src/wg21_wiki_mcp/models.py)).
Errors surface as `McpError` with application codes documented in
[README.md](../README.md#error-contract).
"""
    footer = """## Programmatic access

Tool implementations live in [tools.py](../src/wg21_wiki_mcp/tools.py). Build a
[ServerContext](../src/wg21_wiki_mcp/context.py) and call the same functions the
MCP layer wraps:

```python
from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp import tools

ctx = ServerContext.create(Config.from_env())
tools.get_page(ctx, "Main Page")
```

See [README.md](../README.md#quickstart-programmatic) for a minimal example.
"""
    parts = [header, *tool_blocks, _models_section(), footer]
    return "\n".join(part for part in parts if part)


def _normalize_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 when docs/API.md does not match generated output.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Output path (default: {_DEFAULT_OUTPUT.relative_to(_REPO_ROOT)})",
    )
    args = parser.parse_args(argv)

    try:
        generated = _normalize_newlines(render_api_md())
    except RuntimeError as exc:
        print(f"API docs generation failed: {exc}", file=sys.stderr)
        return 1

    output_path: Path = args.output
    if args.check:
        if not output_path.is_file():
            print(f"{output_path} is missing; run python scripts/build_api_docs.py", file=sys.stderr)
            return 1
        existing = _normalize_newlines(output_path.read_text(encoding="utf-8"))
        if existing != generated:
            print(
                f"{output_path} is out of date; run python scripts/build_api_docs.py",
                file=sys.stderr,
            )
            return 1
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generated, encoding="utf-8", newline="\n")
    print(f"Wrote {output_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
