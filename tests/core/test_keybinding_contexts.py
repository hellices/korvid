"""Shared physical-key and static-context validation for #404."""

from __future__ import annotations

import pytest

from korvid.core.keybindings import canonical_key, plan_keybindings, shift_alias_keys

_ACTIONS = {
    "logs": ("l",),
    "describe": ("d",),
    "restart": ("r",),
    "rollback": ("r",),
    "help": ("question_mark",),
}
_CONTEXTS = {
    "logs": frozenset({("", "pods")}),
    "restart": frozenset({("apps", "deployments")}),
    "rollback": frozenset({("", "helmrevisions")}),
}


def test_remap_can_share_defaults_in_mutually_exclusive_views() -> None:
    plan = plan_keybindings({"logs": "r"}, _ACTIONS, action_contexts=_CONTEXTS)

    assert plan.overrides == {"logs": "r"}
    assert plan.warnings == ()


def test_disjoint_overrides_can_share_a_physical_key() -> None:
    overrides = {"logs": "ctrl+k", "restart": "ctrl+k"}
    plan = plan_keybindings(overrides, _ACTIONS, action_contexts=_CONTEXTS)

    assert plan.overrides == overrides
    assert plan.warnings == ()


def test_unrestricted_action_conflicts_with_every_view() -> None:
    plan = plan_keybindings({"describe": "r"}, _ACTIONS, action_contexts=_CONTEXTS)

    assert plan.overrides == {}
    assert any("restart" in warning for warning in plan.warnings)


def test_explicit_overrides_still_reject_overlapping_contexts() -> None:
    plan = plan_keybindings({"logs": "z", "describe": "z"}, _ACTIONS, action_contexts=_CONTEXTS)

    assert plan.overrides == {"logs": "z"}
    assert any("duplicate" in warning for warning in plan.warnings)


def test_partial_context_intersection_is_a_conflict() -> None:
    contexts = {**_CONTEXTS, "describe": frozenset({("", "pods"), ("", "nodes")})}
    plan = plan_keybindings({"describe": "l"}, _ACTIONS, action_contexts=contexts)

    assert plan.overrides == {}
    assert plan.warnings


def test_missing_context_metadata_never_infers_exclusivity() -> None:
    plan = plan_keybindings({"logs": "r"}, _ACTIONS)

    assert plan.overrides == {}
    assert plan.warnings


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("?", "question_mark"),
        ("/", "slash"),
        (":", "colon"),
        ("~", "tilde"),
        ("+", "plus"),
        ("shift+g", "G"),
        ("ctrl+h", "backspace"),
        ("ctrl+i", "tab"),
        ("ctrl+m", "enter"),
        ("ctrl+left_square_brace", "escape"),
        ("ctrl+[", "escape"),
        ("ctrl+space", "ctrl+at"),
    ],
)
def test_physical_aliases_have_one_conflict_identity(alias: str, expected: str) -> None:
    assert canonical_key(alias) == expected


def test_printable_alias_cannot_hide_a_default_collision() -> None:
    plan = plan_keybindings({"logs": "?"}, _ACTIONS)

    assert plan.overrides == {}
    assert any("help" in warning for warning in plan.warnings)


@pytest.mark.parametrize("key", ["ctrl+m", "ctrl+left_square_brace", "ctrl+["])
def test_approval_protection_applies_to_physical_aliases(key: str) -> None:
    plan = plan_keybindings({"help": key}, _ACTIONS, {"help"})

    assert plan.overrides == {}
    assert any("approval" in warning for warning in plan.warnings)


@pytest.mark.parametrize("key", ["ctrl+w v", "ctrl+", "ctrl+ctrl+k", "unknown_key"])
def test_unusable_keys_warn_without_an_inert_override(key: str) -> None:
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {}
    assert plan.warnings


def test_scoped_default_collision_rejection_still_reaches_a_fixpoint() -> None:
    overrides = {"describe": "l", "logs": "r", "restart": "?"}
    plan = plan_keybindings(overrides, _ACTIONS, action_contexts=_CONTEXTS)

    assert plan.overrides == {"describe": "l", "logs": "r"}
    assert any("help" in warning for warning in plan.warnings)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("alt+ctrl+k", "alt+ctrl+k"),
        ("ctrl+alt+k", "alt+ctrl+k"),
        ("shift+ctrl+alt+k", "alt+ctrl+shift+k"),
        ("super+meta+ctrl+k", "ctrl+meta+super+k"),
    ],
)
def test_modifier_spellings_match_extended_terminal_keys(key: str, expected: str) -> None:
    """Match the extended key spellings verified at the UI parser boundary."""
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {"logs": key}
    assert not plan.warnings
    assert canonical_key(key) == expected
    assert shift_alias_keys(plan.overrides["logs"]) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("<", "less_than_sign"),
        ("less_than_sign", "less_than_sign"),
        (">", "greater_than_sign"),
        ("greater_than_sign", "greater_than_sign"),
    ],
)
def test_angle_punctuation_is_usable(key: str, expected: str) -> None:
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {"logs": key}
    assert not plan.warnings
    assert canonical_key(key) == expected
    assert shift_alias_keys(key) == expected


@pytest.mark.parametrize("literal", ["<", ">"])
def test_angle_punctuation_spellings_share_conflict_identity(literal: str) -> None:
    named = canonical_key(literal)
    plan = plan_keybindings({"logs": literal, "describe": named}, _ACTIONS)
    default_collision = plan_keybindings({"logs": literal}, {"logs": ("l",), "describe": (named,)})

    assert plan.overrides == {"logs": literal}
    assert any("duplicate" in warning for warning in plan.warnings)
    assert default_collision.overrides == {}
    assert any("default key" in warning for warning in default_collision.warnings)


@pytest.mark.parametrize(("key", "alias"), [("ctrl+h", "backspace"), ("backspace", "ctrl+h")])
def test_backspace_aliases_conflict_with_overrides_and_defaults(key: str, alias: str) -> None:
    plan = plan_keybindings({"logs": key, "describe": alias}, _ACTIONS)
    default_collision = plan_keybindings({"logs": key}, {"logs": ("l",), "describe": (alias,)})

    assert plan.overrides == {"logs": key}
    assert any("duplicate" in warning for warning in plan.warnings)
    assert default_collision.overrides == {}
    assert any("default key" in warning for warning in default_collision.warnings)
    assert set(shift_alias_keys(key).split(",")) == {"backspace", "ctrl+h"}


@pytest.mark.parametrize(("key", "alias"), [("ctrl+h", "backspace"), ("backspace", "ctrl+h")])
def test_backspace_aliases_respect_fixed_and_priority_reservations(key: str, alias: str) -> None:
    fixed = plan_keybindings({"logs": key}, _ACTIONS, reserved_keys={alias: "fixed owner"})
    priority = plan_keybindings(
        {"logs": key},
        _ACTIONS,
        {"logs"},
        priority_reserved_keys={alias: "modal owner"},
    )

    assert fixed.overrides == {}
    assert any("reserved by 'fixed owner'" in warning for warning in fixed.warnings)
    assert priority.overrides == {}
    assert any("fixed modal key" in warning for warning in priority.warnings)
