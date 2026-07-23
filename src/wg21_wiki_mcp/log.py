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


class _PackageLogger(logging.Logger):
    """Logger that always attaches :class:`LogSafetyFilter` under the package namespace."""

    def __init__(self, name: str, level: int = logging.NOTSET) -> None:
        super().__init__(name, level)
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            if not any(isinstance(f, LogSafetyFilter) for f in self.filters):
                self.addFilter(_LOG_FILTER)


if not issubclass(logging.getLoggerClass(), _PackageLogger):
    logging.setLoggerClass(_PackageLogger)

_root = logging.getLogger(_PACKAGE)
_root.addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace."""
    if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
        logger = logging.getLogger(name)
    else:
        logger = logging.getLogger(f"{_PACKAGE}.{name}")
    if not any(isinstance(f, LogSafetyFilter) for f in logger.filters):
        logger.addFilter(_LOG_FILTER)
    return logger
