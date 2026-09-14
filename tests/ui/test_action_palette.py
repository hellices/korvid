"""Tests for the palette domain model: derivation, dedup, and ranking (#388)."""

from __future__ import annotations

from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.action_palette import (
    AppActionInvocation,
    PaletteEntry,
    derive_action_entries,
    derive_command_entries,
    rank_entries,
)
from korvid.ui.app_bindings import APP_BINDINGS
from korvid.ui.command import COMMANDS


def _entry(
    entry_id: str,
    title: str,
    *,
    description: str = "",
    aliases: tuple[str, ...] = (),
    available: bool = True,
    order: int = 0,
) -> PaletteEntry:
    reason = (
        None
        if available
        else UnavailableReason(
            AvailabilityCode.NO_SELECTION,
            "select a node first",
        )
    )
    return PaletteEntry(
        id=entry_id,
        title=title,
        description=description,
        category="Actions",
        trigger="D",
        aliases=aliases,
        declaration_order=order,
        availability=ActionAvailability(True, reason),
        invocation=AppActionInvocation(entry_id.removeprefix("action:")),
    )


def _ranking_fixture(*, drain_available: bool = True) -> list[PaletteEntry]:
    return [
        _entry(
            "action:drain_node",
            "Drain",
            aliases=("drain node",),
            available=drain_available,
            order=0,
        ),
        _entry("action:drain_preview", "Drain preview", order=1),
        _entry("action:node_drainage", "Node drainage", order=2),
    ]


def test_action_entries_come_from_bindings_and_deduplicate_terminal_aliases() -> None:
    entries = derive_action_entries(
        APP_BINDINGS,
        overrides={"logs_multi": "ctrl+g"},
        availability=lambda _action: ActionAvailability.enabled(),
    )
    by_id = {entry.id: entry for entry in entries}
    assert by_id["action:logs_multi"].trigger == "Ctrl-G"
    assert sum(entry.id == "action:logs_multi" for entry in entries) == 1
    assert all(
        "(" not in entry.invocation.action
        for entry in entries
        if isinstance(entry.invocation, AppActionInvocation)
    )


def test_command_entries_include_pulse_and_explain_every_omission() -> None:
    entries = derive_command_entries(
        COMMANDS,
        availability=lambda _command: ActionAvailability.enabled(),
    )
    assert any(entry.id == "command:pulse" for entry in entries)
    assert all(
        descriptor.palette is not None or descriptor.palette_omit_reason for descriptor in COMMANDS
    )


def test_ranking_prefers_exact_then_prefix_then_fuzzy() -> None:
    ranked = rank_entries(_ranking_fixture(), "drain")
    assert [entry.id for entry in ranked[:3]] == [
        "action:drain_node",
        "action:drain_preview",
        "action:node_drainage",
    ]


def test_exact_unavailable_result_stays_visible_with_its_reason() -> None:
    ranked = rank_entries(_ranking_fixture(drain_available=False), "drain")
    assert ranked[0].id == "action:drain_node"
    assert ranked[0].availability.reason is not None
