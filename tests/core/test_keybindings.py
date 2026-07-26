"""Keybinding override validation (issue #35): the `keybindings:` config
section maps action names to new keys; unknown actions, protected actions,
and duplicate keys warn instead of crashing."""

from __future__ import annotations

from korvid.core.keybindings import PROTECTED_ACTIONS, plan_keybindings

_ACTIONS: dict[str, tuple[str, ...]] = {
    "quit": ("q",),
    "help": ("question_mark",),
    "logs": ("l",),
    "describe": ("d",),
    "sort_by_age": ("shift+a", "A"),
}


def test_valid_override_is_applied_without_warnings() -> None:
    plan = plan_keybindings({"quit": "ctrl+q"}, _ACTIONS)
    assert plan.overrides == {"quit": "ctrl+q"}
    assert plan.warnings == ()


def test_empty_config_yields_no_overrides_and_no_warnings() -> None:
    plan = plan_keybindings({}, _ACTIONS)
    assert plan.overrides == {}
    assert plan.warnings == ()


def test_unknown_action_warns_and_is_skipped() -> None:
    plan = plan_keybindings({"warp_drive": "w"}, _ACTIONS)
    assert plan.overrides == {}
    assert len(plan.warnings) == 1
    assert "warp_drive" in plan.warnings[0]
    assert "unknown" in plan.warnings[0]


def test_protected_approval_actions_cannot_be_remapped() -> None:
    # Approval dialogs must only be confirmed by the fixed keystrokes —
    # remapping them via config is rejected with an explicit warning.
    for action in sorted(PROTECTED_ACTIONS):
        plan = plan_keybindings({action: "y"}, _ACTIONS)
        assert plan.overrides == {}
        assert len(plan.warnings) == 1
        assert "approval" in plan.warnings[0]


def test_duplicate_key_across_overrides_first_wins() -> None:
    plan = plan_keybindings({"quit": "x", "logs": "x"}, _ACTIONS)
    assert plan.overrides == {"quit": "x"}
    assert len(plan.warnings) == 1
    assert "x" in plan.warnings[0]
    assert "logs" in plan.warnings[0]


def test_collision_with_default_key_of_another_action_warns() -> None:
    # `d` still describes; giving logs the same key would fire two actions.
    plan = plan_keybindings({"logs": "d"}, _ACTIONS)
    assert plan.overrides == {}
    assert len(plan.warnings) == 1
    assert "describe" in plan.warnings[0]


def test_reassigning_a_freed_default_key_is_allowed() -> None:
    # describe moves away from `d`, so giving `d` to logs is not a clash.
    plan = plan_keybindings({"describe": "x", "logs": "d"}, _ACTIONS)
    assert plan.overrides == {"describe": "x", "logs": "d"}
    assert plan.warnings == ()


def test_non_string_or_blank_key_warns() -> None:
    plan = plan_keybindings({"quit": "", "logs": 3}, _ACTIONS)
    assert plan.overrides == {}
    assert len(plan.warnings) == 2


def test_key_is_stripped() -> None:
    plan = plan_keybindings({"quit": " ctrl+q "}, _ACTIONS)
    assert plan.overrides == {"quit": "ctrl+q"}
