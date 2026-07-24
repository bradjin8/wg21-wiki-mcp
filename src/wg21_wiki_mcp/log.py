"""Library-style logging for wg21-wiki-mcp.

Uses stdlib ``logging`` with a ``NullHandler`` on the package logger so hosts
control output. Log calls must never include credentials or wiki page content
(see SECURITY.md).
"""

from __future__ import annotations

import logging

from .log_safety import LogSafetyFilter

_PACKAGE = "wg21_wiki_mcp"
_LOG_FILTER = LogSafetyFilter()
_HOOK_INSTALLED = False


def _ensure_package_filter(logger: logging.Logger) -> None:
    if not any(isinstance(f, LogSafetyFilter) for f in logger.filters):
        logger.addFilter(_LOG_FILTER)


def _install_get_logger_hook() -> None:
    """Attach ``LogSafetyFilter`` to every logger under the package namespace."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    manager = logging.Logger.manager
    original_get_logger = manager.getLogger

    def getLogger(name: str | None = None) -> logging.Logger:
        logger = original_get_logger(name)  # type: ignore[arg-type]
        if name is not None and (name == _PACKAGE or name.startswith(f"{_PACKAGE}.")):
            _ensure_package_filter(logger)
        return logger

    manager.getLogger = getLogger  # type: ignore[method-assign]
    _HOOK_INSTALLED = True


_install_get_logger_hook()

_root = logging.getLogger(_PACKAGE)
_root.addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace."""
    if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_PACKAGE}.{name}")
