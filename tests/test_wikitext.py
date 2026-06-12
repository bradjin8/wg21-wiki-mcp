"""Deterministic agenda extraction tests (synthetic content only)."""

from __future__ import annotations

from wg21_wiki_mcp.wikitext import extract_iso_slots, has_agenda_signal

_AGENDA = """
<table id="agenda">
<td session-start="2026-06-08T09:00+02:00" session-end="2026-06-08T10:15+02:00" class="x">
<div class="info">(plenary)</div></td>
<td session-start="2026-06-08T10:30+02:00" session-end="2026-06-08T12:00+02:00" class="y"></td>
</table>
"""


def test_extract_success_with_label():
    slots, status = extract_iso_slots(_AGENDA, "Some:Agenda")
    assert status == "success"
    assert len(slots) == 2
    assert slots[0].start == "2026-06-08T09:00+02:00"
    assert slots[0].end == "2026-06-08T10:15+02:00"
    assert slots[0].label_hint == "(plenary)"
    assert slots[0].source == "Some:Agenda"
    assert slots[1].label_hint is None


def test_has_agenda_signal():
    assert has_agenda_signal(_AGENDA)
    assert has_agenda_signal('<table id="agenda"></table>')
    assert not has_agenda_signal("== Agenda ==\nNothing machine-readable here.")


def test_not_found():
    slots, status = extract_iso_slots("just prose, no schedule", "X")
    assert status == "not_found"
    assert slots == []


def test_partial_signal_without_parse():
    # Signal substring present but not a well-formed slot element.
    slots, status = extract_iso_slots("session-start= but malformed", "X")
    assert status == "partial"
    assert slots == []
