"""Library-style logging for wg21-wiki-mcp.

Uses stdlib ``logging`` with a ``NullHandler`` on the package logger so hosts
control output. Log calls must never include credentials or wiki page content
(see SECURITY.md).
"""

from __future__ import annotations

import logging

_PACKAGE = "wg21_wiki_mcp"

logging.getLogger(_PACKAGE).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace."""
    if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_PACKAGE}.{name}")
