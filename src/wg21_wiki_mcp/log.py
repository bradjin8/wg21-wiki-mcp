"""Library-style logging for wg21-wiki-mcp.

Uses stdlib ``logging`` with a silent handler on the package logger so hosts
control output. Log calls must never include credentials or wiki page content
(see SECURITY.md).
"""

from __future__ import annotations

import logging

from .log_safety import LogSafetyFilter

_PACKAGE = "wg21_wiki_mcp"
_LOG_FILTER = LogSafetyFilter()


class _SilentHandler(logging.Handler):
    """Handler that runs filters but emits nowhere (library default)."""

    def emit(self, record: logging.LogRecord) -> None:
        pass


_root = logging.getLogger(_PACKAGE)
if not any(isinstance(handler, _SilentHandler) for handler in _root.handlers):
    _handler = _SilentHandler()
    _handler.addFilter(_LOG_FILTER)
    _root.addHandler(_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace."""
    if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_PACKAGE}.{name}")
