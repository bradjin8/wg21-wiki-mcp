"""Tests for the deprecation warning utility."""

from __future__ import annotations

import warnings

from wg21_wiki_mcp.deprecation import warn_deprecated


def test_warn_deprecated_emits_deprecation_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        warn_deprecated("old_tool is deprecated; use new_tool instead", since="0.3.0", removal="0.4.0")

    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
    assert "old_tool is deprecated" in str(caught[0].message)
    assert "Deprecated since 0.3.0" in str(caught[0].message)
    assert "Scheduled for removal in 0.4.0" in str(caught[0].message)


def test_warn_deprecated_message_only():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        warn_deprecated("minimal deprecation notice")

    assert len(caught) == 1
    assert str(caught[0].message) == "minimal deprecation notice"
