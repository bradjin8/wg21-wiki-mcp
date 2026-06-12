"""Deterministic, signal-based extraction from wikitext.

Only ~100%-reliable, mechanical extraction lives here (per the parse-vs-offload
policy). Today that is: detecting an agenda page and pulling its machine-readable
``session-start``/``session-end`` ISO time boundaries. Everything else about a
page is left verbatim for the calling LLM to interpret.
"""

from __future__ import annotations

import re

from .models import IsoSlot

# A <td ...> cell carrying both session-start and session-end ISO attributes.
_SLOT_RE = re.compile(
    r'session-start="(?P<start>[^"]+)"\s+session-end="(?P<end>[^"]+)"(?P<rest>[^>]*)>(?P<inner>.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
_INFO_RE = re.compile(r'class="info"[^>]*>(?P<label>[^<]*)<', re.IGNORECASE)
_AGENDA_SIGNAL_RE = re.compile(r'session-start=|id="agenda"', re.IGNORECASE)


def has_agenda_signal(wikitext: str) -> bool:
    """Return True if the page looks like a machine-readable agenda (structural signal)."""
    return bool(_AGENDA_SIGNAL_RE.search(wikitext))


def extract_iso_slots(wikitext: str, source_title: str) -> tuple[list[IsoSlot], str]:
    """Extract agenda time boundaries deterministically.

    Returns (slots, status) where status is "success", "partial", or "not_found".
    Times are returned exactly as written on the page (no timezone normalization).
    """
    slots: list[IsoSlot] = []
    for match in _SLOT_RE.finditer(wikitext):
        label: str | None = None
        info = _INFO_RE.search(match.group("inner") or "")
        if info:
            label = info.group("label").strip() or None
        slots.append(
            IsoSlot(
                start=match.group("start"),
                end=match.group("end"),
                source=source_title,
                label_hint=label,
            )
        )
    if slots:
        return slots, "success"
    if "session-start" in wikitext.lower():
        return [], "partial"  # signal present but nothing parsed cleanly
    return [], "not_found"
