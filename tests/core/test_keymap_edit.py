"""A pending keymap is one reversible transaction, not partial live changes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from korvid.core.keybindings import KeymapPlan
    from korvid.core.keymap_edit import KeymapEdit


def _edit(overrides: dict[str, str] | None = None) -> KeymapEdit:
    from korvid.core.keymap_edit import KeymapEdit, KeymapRules

    rules = KeymapRules(
        actions={"logs": ("l",), "describe": ("d",), "relationships": ("g",), "agent": ("ctrl+a",)},
        priority_actions=frozenset({"agent"}),
        reserved_keys={"1": "namespace slot", "enter": "table drill down"},
        priority_reserved_keys={"ctrl+p": "palette close"},
    )
    return KeymapEdit(rules, overrides or {})


def test_rotation_preserves_temporary_conflicts_until_the_complete_map_is_valid() -> None:
    edit = _edit()

    edit.assign("logs", "d")
    assert edit.conflicts()[0].actions == ("logs", "describe")
    edit.assign("describe", "g")
    assert edit.conflicts()[0].actions == ("describe", "relationships")
    edit.assign("relationships", "l")

    assert edit.overrides == {"logs": "d", "describe": "g", "relationships": "l"}
    assert not edit.conflicts()
    assert not edit.rules.plan(edit.overrides).warnings


def test_conflict_can_offer_a_valid_two_way_swap() -> None:
    edit = _edit()
    edit.assign("logs", "d")

    assert edit.can_swap
    assert edit.swap()
    assert edit.overrides == {"logs": "d", "describe": "l"}
    assert not edit.conflicts()
    assert edit.undo()
    assert edit.overrides == {"logs": "d"}
    assert edit.can_swap


def test_backtracking_restores_the_preceding_edit_and_conflict() -> None:
    edit = _edit()
    edit.assign("logs", "d")
    edit.assign("describe", "g")
    edit.assign("relationships", "l")

    assert edit.undo()
    assert edit.conflicts()
    assert edit.overrides == {"logs": "d", "describe": "g"}
    assert edit.undo()
    assert edit.undo()
    assert not edit.undo()
    assert not edit.dirty


def test_reset_one_retains_an_occupied_default_as_a_visible_conflict() -> None:
    edit = _edit({"logs": "z", "describe": "l"})

    edit.reset("logs")

    assert edit.overrides == {"describe": "l"}
    assert edit.conflicts()[0].key == "l"
    assert edit.can_swap
    assert edit.swap()
    assert edit.overrides == {"describe": "z"}


def test_full_reset_is_staged_and_undoable() -> None:
    original = {"logs": "z", "describe": "x"}
    edit = _edit(original)

    edit.reset_all()

    assert edit.overrides == {}
    assert original == {"logs": "z", "describe": "x"}
    assert edit.dirty
    assert edit.undo()
    assert edit.overrides == original
    assert not edit.dirty


def test_pending_map_is_not_mutable_through_the_public_snapshot() -> None:
    edit = _edit()
    snapshot = edit.overrides
    snapshot["logs"] = "x"

    assert edit.overrides == {}
    assert not edit.dirty


@pytest.mark.parametrize(
    ("action", "key", "reason"),
    [
        ("logs", "1", "reserved"),
        ("agent", "y", "approval"),
        ("agent", "ctrl+m", "approval"),
        ("agent", "ctrl+p", "fixed modal"),
        ("logs", "ctrl+w v", "one key"),
        ("invented", "z", "unknown action"),
    ],
)
def test_invalid_assignment_does_not_modify_the_session(action: str, key: str, reason: str) -> None:
    edit = _edit()

    with pytest.raises(ValueError, match=reason):
        edit.assign(action, key)

    assert not edit.dirty
    assert not edit.undo()


def test_suggestions_are_deterministic_bounded_and_usable() -> None:
    edit = _edit()
    edit.assign("logs", "d")
    suggestions = edit.suggestions("describe")

    assert 0 < len(suggestions) <= 3
    assert suggestions == edit.suggestions("describe")
    assert len(edit.suggestions("describe", limit=1000)) <= 5
    assert edit.suggestions("describe", limit=0) == ()
    for suggestion in suggestions:
        proposal = {**edit.overrides, "describe": suggestion}
        assert not edit.rules.plan(proposal).warnings


def test_context_disjoint_reuse_is_not_a_conflict_or_a_swap() -> None:
    from korvid.core.keymap_edit import KeymapEdit, KeymapRules

    edit = KeymapEdit(
        KeymapRules(
            actions={"logs": ("l",), "restart": ("r",)},
            action_contexts={
                "logs": frozenset({("", "pods")}),
                "restart": frozenset({("apps", "deployments")}),
            },
        ),
        {},
    )
    edit.assign("logs", "r")

    assert not edit.conflicts()
    assert not edit.can_swap
    assert not edit.swap()


def test_revisiting_an_action_does_not_automatically_chase_a_cycle() -> None:
    edit = _edit()
    edit.assign("logs", "d")
    edit.assign("describe", "g")
    edit.assign("logs", "g")

    assert edit.overrides == {"logs": "g", "describe": "g"}
    assert edit.conflicts()
    assert not edit.can_swap
    edit.assign("describe", "d")
    edit.assign("relationships", "l")
    assert not edit.conflicts()


def test_aliases_are_compared_before_planning_a_chain() -> None:
    from korvid.core.keymap_edit import KeymapEdit, KeymapRules

    edit = KeymapEdit(KeymapRules(actions={"first": ("x",), "second": ("shift+g", "G")}), {})
    edit.assign("first", "G")

    assert len(edit.conflicts()) == 1
    assert edit.conflicts()[0].key == "G"
    assert edit.swap()
    assert edit.overrides == {"first": "G", "second": "x"}


@pytest.mark.parametrize(("key", "alias"), [("ctrl+h", "backspace"), ("backspace", "ctrl+h")])
def test_staged_backspace_aliases_report_one_physical_conflict(key: str, alias: str) -> None:
    edit = _edit()
    edit.assign("logs", key)
    edit.assign("describe", alias)

    conflicts = edit.conflicts()
    assert edit.overrides == {"logs": key, "describe": alias}
    assert len(conflicts) == 1
    assert conflicts[0].actions == ("logs", "describe")
    assert conflicts[0].key == "backspace"
    assert edit.rules.plan(edit.overrides).warnings


def test_suggestions_do_not_advertise_unresolved_independent_conflicts() -> None:
    from korvid.core.keymap_edit import KeymapEdit, KeymapRules

    edit = KeymapEdit(
        KeymapRules(actions={"first": ("a",), "second": ("b",), "third": ("c",), "fourth": ("d",)}),
        {},
    )
    edit.assign("first", "b")
    edit.assign("third", "d")
    original = edit.overrides

    assert len(edit.conflicts()) == 2
    assert edit.suggestions("second", limit=5) == ()
    assert edit.suggestions("second", limit=5) == ()
    assert edit.overrides == original
    assert edit.last_action == "third"
    assert edit.undo()
    assert edit.overrides == {"first": "b"}


def test_suggestions_allow_context_disjoint_key_reuse() -> None:
    from korvid.core.keymap_edit import KeymapEdit, KeymapRules

    rules = KeymapRules(
        actions={"pods_action": ("a",), "nodes_action": ("b",)},
        action_contexts={
            "pods_action": frozenset({("", "pods")}),
            "nodes_action": frozenset({("", "nodes")}),
        },
    )
    edit = KeymapEdit(rules, {})
    edit.assign("pods_action", "z")
    original = edit.overrides

    suggestions = edit.suggestions("pods_action", limit=5)

    assert suggestions == ("a", "b", "c", "d", "e")
    assert suggestions == edit.suggestions("pods_action", limit=5)
    assert edit.overrides == original
    for suggestion in suggestions:
        candidate = KeymapEdit(rules, original)
        candidate.assign("pods_action", suggestion)
        assert not rules.plan(candidate.overrides).warnings


def test_suggestions_validate_default_equivalent_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    from korvid.core.keymap_edit import KeymapEdit, KeymapRules

    rules = KeymapRules(actions={"first": ("shift+g", "G"), "second": ("d",)})
    edit = KeymapEdit(rules, {})
    edit.assign("first", "z")
    proposals: list[dict[str, object]] = []
    original_plan = KeymapRules.plan

    def record_plan(rules: KeymapRules, overrides: Mapping[str, object]) -> KeymapPlan:
        proposals.append(dict(overrides))
        return original_plan(rules, overrides)

    monkeypatch.setattr(KeymapRules, "plan", record_plan)

    assert edit.suggestions("first", limit=1) == ("G",)
    assert proposals == [{}]
    assert edit.overrides == {"first": "z"}
    assert edit.last_action == "first"
    assert edit.undo()
    assert edit.overrides == {}
    assert not edit.can_undo
