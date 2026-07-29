"""Keybinding remapping UI tests (issue #35): the `keybindings:` config
section actually rebinds keys at startup, warns on bad entries, and the
help overlay shows the effective keys."""

from __future__ import annotations

from pathlib import Path

from korvid.core.config import KorvidConfig
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .waits import until


def _config(keybindings: dict[str, str]) -> KorvidConfig:
    return KorvidConfig(namespace="default", keybindings=keybindings)


async def test_remapped_key_triggers_action_and_default_is_freed() -> None:
    app = make_app([_pod("web")], config=_config({"help": "f1"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("question_mark")  # freed default must be inert now
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        await pilot.press("f1")
        await until(
            pilot,
            lambda: isinstance(app.screen, HelpScreen),
            label="help opens on f1",
        )


async def test_unknown_action_warns_at_startup_instead_of_crashing() -> None:
    app = make_app([_pod("web")], config=_config({"warp_drive": "w"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("warp_drive" in n.message for n in app._notifications),
            label="unknown-action warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_approval_dialog_actions_cannot_be_remapped() -> None:
    # Safety invariant: approval dialogs are confirmed only by their fixed
    # keystrokes — config must never rebind them.
    app = make_app([_pod("web")], config=_config({"confirm": "enter"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("approval" in n.message for n in app._notifications),
            label="protected-action warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_help_overlay_shows_remapped_key() -> None:
    app = make_app([_pod("web")], config=_config({"logs": "ctrl+g"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("question_mark")
        await until(
            pilot,
            lambda: isinstance(app.screen, HelpScreen),
            label="help overlay open",
        )
        body = app.screen.body_text() if isinstance(app.screen, HelpScreen) else ""
        assert "Ctrl-G" in body
        # The old default key row for logs is gone from the overlay.
        assert "  l          Logs" not in body


async def test_uppercase_alt_binding_follows_the_remap() -> None:
    # sort_by_age is bound to both shift+a and the terminal-delivered "A";
    # remapping the action must retire both spellings.
    pods = [_pod("bb"), _pod("aa")]
    app = make_app(pods, config=_config({"sort_by_age": "g"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")

        def _sorted_by_age() -> bool:
            return any(
                "AGE" in str(c.label) and "▼" in str(c.label) for c in table.columns.values()
            )

        await pilot.press("A")
        await pilot.pause()
        assert not _sorted_by_age()
        await pilot.press("g")
        await until(pilot, lambda: _sorted_by_age(), label="g sorts by age")


def test_keybindings_doc_documents_every_remappable_action() -> None:
    # docs/keybindings.md's action-name list must not drift from the real
    # BINDINGS (the list moved out of README.md in the docs restructure).
    doc = Path(__file__).parents[2].joinpath("docs", "keybindings.md").read_text()
    for action in KorvidApp._binding_actions():
        assert f"`{action}`" in doc, f"docs/keybindings.md missing keybinding action {action!r}"


def test_favorite_namespace_keys_are_not_remappable() -> None:
    # The nine 1-9 favorite bindings carry no keymap id — the keymap cannot
    # move them, so offering them as remappable actions would be a lie.
    actions = KorvidApp._binding_actions()
    assert not any(action.startswith("favorite_namespace") for action in actions)


async def test_shifted_letter_remap_works_via_terminal_uppercase_spelling() -> None:
    # Real terminals deliver shift+g as "G" — the documented `shift+g`
    # syntax must still work there, not only under Pilot.
    pods = [_pod("bb"), _pod("aa")]
    app = make_app(pods, config=_config({"sort_by_age": "shift+g"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")

        def _sorted_by_age() -> bool:
            return any(
                "AGE" in str(c.label) and "▼" in str(c.label) for c in table.columns.values()
            )

        await pilot.press("G")  # the terminal spelling of shift+g
        await until(pilot, lambda: _sorted_by_age(), label="G sorts by age")


async def test_priority_action_cannot_take_an_approval_dialog_key() -> None:
    # toggle_agent is a priority binding (fires before any screen); giving
    # it "y" would steal the approval dialog's confirm keystroke.
    app = make_app([_pod("web")], config=_config({"toggle_agent": "y"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("approval" in n.message for n in app._notifications),
            label="priority/approval-key warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_help_overlay_applies_remap_to_helm_rows() -> None:
    """The Helm rows in the help overlay come from dedicated remappable
    bindings (issue #114): the overlay must advertise the effective keys,
    not the hardcoded defaults."""
    app = make_app([_pod("web")], config=_config({"helm_install": "x"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("question_mark")
        await until(
            pilot,
            lambda: isinstance(app.screen, HelpScreen),
            label="help overlay open",
        )
        body = app.screen.body_text() if isinstance(app.screen, HelpScreen) else ""
        assert "x          Install chart" in body
        assert "i          Install chart" not in body


async def test_favorite_digit_keys_are_reserved_against_overrides() -> None:
    # The 1-9 favorites are excluded from the remappable-action map, but
    # their keys must still be reserved: `logs: "1"` would otherwise be
    # accepted while the live favorite binding still owns the key.
    app = make_app([_pod("web")], config=_config({"logs": "1"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("reserved" in n.message for n in app._notifications),
            label="reserved-key warning notified",
        )
        assert app._keybinding_overrides == {}
