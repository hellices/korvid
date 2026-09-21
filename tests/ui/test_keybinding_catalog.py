"""One catalog for startup, editing, and complete Textual keymaps."""

from __future__ import annotations

import pytest
from textual._xterm_parser import XTermParser
from textual.app import App
from textual.binding import Binding, BindingsMap
from textual.events import Key

from korvid.core.keybindings import canonical_key, shift_alias_keys
from korvid.ui.app_bindings import APP_BINDINGS
from korvid.ui.keybinding_catalog import KeybindingCatalog


def test_catalog_uses_dispatch_scopes_not_help_groups() -> None:
    catalog = KeybindingCatalog(APP_BINDINGS)
    plan = catalog.rules.plan({"logs": "r", "helm_install": "l"})
    assert plan.overrides == {"logs": "r", "helm_install": "l"}
    assert not plan.warnings
    assert catalog.rules.action_contexts["logs"] == frozenset({("", "pods")})
    assert "log_format" not in catalog.rules.action_contexts
    assert catalog.rules.plan({"helm_install": "f"}).warnings


@pytest.mark.parametrize(
    "key",
    [
        "1",
        "9",
        "ctrl+w",
        "enter",
        "ctrl+m",
        "escape",
        "ctrl+[",
        "up",
        "tab",
        "ctrl+i",
        "ctrl+q",
        "ctrl+pageup",
        "ctrl+pagedown",
    ],
)
def test_fixed_handlers_and_namespace_slots_are_reserved(key: str) -> None:
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan({"logs": key})
    assert not plan.overrides
    assert plan.warnings


def test_zero_remains_remappable_and_defaults_are_derived() -> None:
    catalog = KeybindingCatalog(APP_BINDINGS)
    assert catalog.rules.plan({"toggle_all_namespaces": "z"}).overrides == {
        "toggle_all_namespaces": "z"
    }
    assert "favorite_namespace(1)" not in catalog.rules.actions
    assert catalog.descriptions["logs"] == "Logs"
    assert catalog.rules.actions["logs_multi"] == ("shift+l", "L")


def test_priority_actions_cannot_steal_editor_controls() -> None:
    catalog = KeybindingCatalog(APP_BINDINGS, modal_keys=("f9", "f10"))
    assert catalog.rules.plan({"toggle_agent": "f9"}).warnings
    assert catalog.rules.plan({"open_action_palette": "f10"}).warnings
    assert catalog.rules.plan({"help": "f10"}).overrides == {"help": "f10"}


@pytest.mark.parametrize("action", ["toggle_agent", "interrupt_agent", "open_action_palette"])
def test_priority_actions_cannot_steal_approval_decline(action: str) -> None:
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan({action: "ctrl+n"})
    assert not plan.overrides
    assert any("decline" in warning for warning in plan.warnings)


def test_nonpriority_actions_can_reuse_approval_decline_outside_a_modal() -> None:
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan({"help": "ctrl+n"})
    assert plan.overrides == {"help": "ctrl+n"}
    assert not plan.warnings


def test_complete_keymap_preserves_contextual_owners_on_the_same_key() -> None:
    catalog = KeybindingCatalog(APP_BINDINGS)
    keymap = catalog.keymap({"logs": "r"})
    bindings = BindingsMap(APP_BINDINGS)
    bindings.apply_keymap(keymap)
    assert {binding.action for binding in bindings.key_to_bindings["r"]} == {
        "logs",
        "rollout_restart",
        "helm_rollback",
    }
    assert set(keymap) == {
        binding.id for binding in APP_BINDINGS if isinstance(binding, Binding) and binding.id
    }
    assert keymap["logs_multi--alt"] == "shift+l,L"


def test_complete_reset_restores_defaults_and_alt_bindings() -> None:
    catalog = KeybindingCatalog(APP_BINDINGS)
    bindings = BindingsMap(APP_BINDINGS)
    bindings.apply_keymap(catalog.keymap({}))
    assert any(binding.action == "logs" for binding in bindings.key_to_bindings["l"])
    assert any(binding.action == "sort_by_age" for binding in bindings.key_to_bindings["A"])
    assert any(binding.action == "sort_by_age" for binding in bindings.key_to_bindings["shift+a"])


def test_dispatch_scope_access_does_not_mutate_the_policy() -> None:
    catalog = KeybindingCatalog(APP_BINDINGS)
    copied = dict(catalog.rules.action_contexts)
    copied.clear()
    assert KeybindingCatalog(APP_BINDINGS).rules.action_contexts["logs"] == frozenset(
        {("", "pods")}
    )


def test_complete_keymap_matches_an_actual_extended_terminal_event() -> None:
    events = XTermParser()._parse_extended_key("\x1b[107;7u")
    assert events is not None
    catalog = KeybindingCatalog(APP_BINDINGS)
    bindings = BindingsMap(APP_BINDINGS)
    bindings.apply_keymap(catalog.keymap({"logs": "ctrl+alt+k"}))
    assert events[0].key in bindings.key_to_bindings
    assert any(binding.action == "logs" for binding in bindings.key_to_bindings[events[0].key])


def test_backtab_alias_matches_terminal_events_and_fixed_navigation() -> None:
    events = XTermParser()._parse_extended_key("\x1b[9;2u")
    assert events is not None
    assert canonical_key("backtab") == events[0].key
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan({"logs": "backtab"})
    assert not plan.overrides
    assert plan.warnings


@pytest.mark.parametrize(
    ("key", "sequence"),
    [
        ("ctrl+space", "\x00"),
        ("ctrl+space", "\x1b[32;5u"),
        ("ctrl+@", "\x00"),
        ("ctrl+@", "\x1b[32;5u"),
        ("ctrl+at", "\x00"),
        ("ctrl+at", "\x1b[64;5u"),
        ("ctrl+h", "\x08"),
        ("ctrl+h", "\x7f"),
        ("ctrl+h", "\x1b[104;5u"),
        ("backspace", "\x1b[104;5u"),
        ("ctrl+m", "\r"),
        ("ctrl+m", "\x1b[109;5u"),
        ("ctrl+i", "\t"),
        ("ctrl+i", "\x1b[105;5u"),
        ("escape", "\x1b[91;5u"),
        ("ctrl+[", "\x1b[91;5u"),
        ("alt+/", "\x1b[47;3u"),
        ("ctrl+,", "\x1b[44;5u"),
    ],
)
def test_emitted_aliases_dispatch_actual_legacy_and_extended_events(
    key: str, sequence: str
) -> None:
    events = [event for event in XTermParser().feed(sequence) if isinstance(event, Key)]
    assert len(events) == 1
    bindings = BindingsMap([Binding("l", "logs", "Logs", id="logs")])
    bindings.apply_keymap(App._normalize_keymap({"logs": shift_alias_keys(key)}))
    assert any(alias in bindings.key_to_bindings for alias in events[0].aliases)
