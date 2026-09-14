"""Tests for the palette domain model: derivation, dedup, and ranking (#388)."""

from __future__ import annotations

from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.action_palette import (
    AppActionInvocation,
    CommandInvocation,
    PaletteEntry,
    derive_action_entries,
    derive_command_entries,
    derive_palette_entries,
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


def _command_entry(
    entry_id: str,
    title: str,
    *,
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
    canonical = entry_id.removeprefix("command:")
    return PaletteEntry(
        id=entry_id,
        title=title,
        description="",
        category="Commands",
        trigger=f":{canonical}",
        aliases=(),
        declaration_order=order,
        availability=ActionAvailability(True, reason),
        invocation=CommandInvocation(canonical),
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


def test_empty_query_orders_invokable_actions_before_commands_before_unavailable() -> None:
    """Carry-over from the task 2 review: the brief's default-view ordering
    ("invokable actions, then invokable commands, then every unavailable
    entry") had no committed characterization test. This pins it, including
    the declaration-order tie-break within each group."""
    entries = [
        _command_entry("command:pulse", "Open Pulse", order=0),
        _entry("action:drain_node", "Drain", available=False, order=0),
        _entry("action:describe", "Describe", order=1),
        _command_entry("command:ns", "Choose namespace", order=1),
        _entry("action:cordon_node", "Cordon", order=0),
    ]
    ranked = rank_entries(entries, "")
    assert [entry.id for entry in ranked] == [
        "action:cordon_node",
        "action:describe",
        "command:pulse",
        "command:ns",
        "action:drain_node",
    ]


def test_action_entries_expose_a_humanized_title_and_the_bindings_description() -> None:
    """Carry-over from the task 2 review: the domain model's title/description
    split was reversed. `derive_action_entries` now uses a deterministic,
    catalog-derived rule rather than a new metadata table: the humanized
    action id is the (short) title, and the `Binding`'s own `description`
    text — sometimes a fuller explanation, e.g. `resize_pod`'s — becomes the
    palette's second-line description.
    """
    entries = derive_action_entries(
        APP_BINDINGS,
        availability=lambda _action: ActionAvailability.enabled(),
    )
    by_id = {entry.id: entry for entry in entries}
    resize = by_id["action:resize_pod"]
    assert resize.title == "Resize pod"
    assert resize.description == "Resize pod CPU/memory in place (K8s 1.35+)"
    drain = by_id["action:drain_node"]
    assert drain.title == "Drain node"
    assert drain.description == "Drain"


def test_command_entries_expose_the_palette_title_and_first_help_description() -> None:
    """Carry-over from the task 2 review: command entries now carry a real
    second-line description too, sourced from the descriptor's own first
    help row rather than a new table."""
    entries = derive_command_entries(
        COMMANDS,
        availability=lambda _command: ActionAvailability.enabled(),
    )
    by_id = {entry.id: entry for entry in entries}
    ai_command = by_id["command:ai"]
    assert ai_command.title == "Configure Agent"
    assert ai_command.description == "Agent setup; off disconnects, payload inspects (also :agent)"


def test_action_and_command_descriptions_are_reachable_by_fuzzy_search() -> None:
    """Carry-over from the task 2 review: exercise the description-match
    tier (tier 3 in `_match_tier`) end to end with the real catalogs, now
    that descriptions are populated."""
    action_entries = derive_action_entries(
        APP_BINDINGS,
        availability=lambda _action: ActionAvailability.enabled(),
    )
    ranked_actions = rank_entries(action_entries, "cpu/memory")
    assert ranked_actions[0].id == "action:resize_pod"

    command_entries = derive_command_entries(
        COMMANDS,
        availability=lambda _command: ActionAvailability.enabled(),
    )
    ranked_commands = rank_entries(command_entries, "disconnects")
    assert ranked_commands[0].id == "command:ai"


def test_palette_catalog_combines_both_catalogs_and_asks_about_every_name() -> None:
    """The app's one catalog call (task 6): actions first, then commands,
    with the same policy asked about each action name and each command's
    canonical text - no third, hand-maintained table in between."""
    asked: list[str] = []

    def availability(name: str) -> ActionAvailability:
        asked.append(name)
        return ActionAvailability.enabled()

    entries = derive_palette_entries(
        APP_BINDINGS,
        COMMANDS,
        overrides={"help": "f1"},
        availability=availability,
    )
    ids = [entry.id for entry in entries]
    assert ids == [
        *(entry.id for entry in derive_action_entries(APP_BINDINGS, availability=availability)),
        *(entry.id for entry in derive_command_entries(COMMANDS, availability=availability)),
    ]
    assert "action:open_action_palette" in ids
    assert "command:pulse" in ids
    assert {"help", "pulse"} <= set(asked)
    assert next(e for e in entries if e.id == "action:help").trigger == "f1"
