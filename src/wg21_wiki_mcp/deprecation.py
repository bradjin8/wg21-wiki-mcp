"""Deprecation warnings for wg21-wiki-mcp.

Use :func:`warn_deprecated` when marking APIs for removal. Deprecations are
communicated for at least one minor version before removal; see STABILITY.md.
"""

from __future__ import annotations

import warnings


def warn_deprecated(
    message: str,
    *,
    since: str | None = None,
    removal: str | None = None,
) -> None:
    """Emit a :class:`DeprecationWarning` for a feature scheduled for removal.

    Parameters
    ----------
    message
        What is deprecated and what callers should use instead.
    since
        Version when the feature was deprecated (e.g. ``"0.3.0"``).
    removal
        Planned removal version (e.g. ``"0.4.0"``).
    """
    parts = [message]
    if since is not None:
        parts.append(f"Deprecated since {since}.")
    if removal is not None:
        parts.append(f"Scheduled for removal in {removal}.")
    warnings.warn(" ".join(parts), DeprecationWarning, stacklevel=2)
