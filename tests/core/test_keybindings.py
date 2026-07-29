"""Keybinding override validation (issue #35): the `keybindings:` config
section maps action names to new keys; unknown actions, protected actions,
and duplicate keys warn instead of crashing."""

from __future__ import annotations

from korvid.core.keybindings import PROTECTED_ACTIONS, plan_keybindings, shift_alias_keys

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


def test_collision_check_runs_to_fixpoint_when_rejection_restores_defaults() -> None:
    # bb→3 is rejected (3 is cc's default), which restores bb's default 2 —
    # so aa→2, accepted in the first pass, must also be rejected.
    actions = {"aa": ("1",), "bb": ("2",), "cc": ("3",)}
    plan = plan_keybindings({"aa": "2", "bb": "3"}, actions)
    assert plan.overrides == {}
    assert len(plan.warnings) == 2


def test_shift_and_uppercase_spellings_collide_with_each_other() -> None:
    # Terminals deliver shift+a as "A"; both spellings are one physical key.
    plan = plan_keybindings({"logs": "shift+a"}, _ACTIONS)
    assert plan.overrides == {}
    assert any("sort_by_age" in w for w in plan.warnings)
    plan = plan_keybindings({"quit": "shift+g", "logs": "G"}, _ACTIONS)
    assert plan.overrides == {"quit": "shift+g"}
    assert any("duplicate" in w for w in plan.warnings)


def test_priority_actions_may_not_take_approval_dialog_keys() -> None:
    # A priority binding fires before ConfirmScreen's handlers — remapping
    # one onto y/n/enter/escape would steal the dialog's fixed keys.
    actions = {**_ACTIONS, "toggle_agent": ("ctrl+a",)}
    for key in ("y", "n", "enter", "escape"):
        plan = plan_keybindings({"toggle_agent": key}, actions, {"toggle_agent"})
        assert plan.overrides == {}
        assert any("approval" in w for w in plan.warnings)
    # Non-priority actions may use those keys (they never outrank a dialog).
    plan = plan_keybindings({"describe": "y"}, actions, {"toggle_agent"})
    assert plan.overrides == {"describe": "y"}


def test_shift_alias_keys_expands_both_spellings() -> None:
    assert shift_alias_keys("shift+g") == "shift+g,G"
    assert shift_alias_keys("G") == "shift+g,G"
    assert shift_alias_keys("ctrl+q") == "ctrl+q"
    assert shift_alias_keys("g") == "g"


def test_reserved_keys_cannot_be_taken_by_an_override() -> None:
    # Keys owned by non-remappable bindings (the 1-9 favorites, issue #108)
    # must still participate in collision checks even though their actions
    # are not in the remappable-action map.
    plan = plan_keybindings({"logs": "1"}, _ACTIONS, reserved_keys={"1": "favorite_namespace(1)"})
    assert plan.overrides == {}
    assert len(plan.warnings) == 1
    assert "reserved" in plan.warnings[0]
    assert "favorite_namespace(1)" in plan.warnings[0]


def test_non_reserved_key_override_is_unaffected_by_reserved_keys() -> None:
    plan = plan_keybindings(
        {"logs": "ctrl+l"}, _ACTIONS, reserved_keys={"1": "favorite_namespace(1)"}
    )
    assert plan.overrides == {"logs": "ctrl+l"}
    assert plan.warnings == ()
