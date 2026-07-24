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


_SILENT_HANDLER: _SilentHandler | None = None


def _install_package_log_safety(logger: logging.Logger | None = None) -> None:
    """Ensure ``LogSafetyFilter`` runs before any package handler emits a record."""
    global _SILENT_HANDLER
    root = logger or logging.getLogger(_PACKAGE)

    silent = _SILENT_HANDLER
    if silent is None:
        silent = _SilentHandler()
        silent.addFilter(_LOG_FILTER)
        _SILENT_HANDLER = silent

    # Insert first: LogSafetyFilter mutates the record in place before later handlers.
    if silent in root.handlers:
        root.handlers.remove(silent)
    root.handlers.insert(0, silent)


_install_package_log_safety()


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace."""
    if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_PACKAGE}.{name}")
