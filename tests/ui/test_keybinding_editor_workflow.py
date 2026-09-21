"""Real command-to-editor workflows preserve dispatch, labels, and saved state."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from pathlib import Path

import pytest
import yaml
from textual._xterm_parser import XTermParser
from textual.pilot import Pilot
from textual.widgets import Button, Input

from korvid.core.config import KorvidConfig, load_config
from korvid.core.keybinding_config import save_keybindings
from korvid.core.store import ALL_NAMESPACES
from korvid.ui.app import KorvidApp
from korvid.ui.messages import BuiltinCommand, BuiltinOperation
from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.top_bar import TopBar

from .test_app import _pod, make_app
from .waits import until


async def _open_editor(
    pilot: Pilot[None], app: KorvidApp, command: str = "keys"
) -> KeybindingEditorScreen:
    await pilot.press("colon", *command, "enter")
    await until(pilot, lambda: isinstance(app.screen, KeybindingEditorScreen), label="editor open")
    assert isinstance(app.screen, KeybindingEditorScreen)
    return app.screen


async def _stage(pilot: Pilot[None], screen: KeybindingEditorScreen, action: str, key: str) -> None:
    await pilot.press("f1", "ctrl+u", *action, "tab", "enter", "f2", "ctrl+u")
    await pilot.press(*(character if character != "+" else "plus" for character in key), "enter")
    await until(pilot, lambda: screen.edit.overrides.get(action) == key, label=f"{action} staged")


async def _confirm(pilot: Pilot[None], app: KorvidApp) -> None:
    await pilot.press("f9", "f10")
    await until(pilot, lambda: len(app.screen_stack) == 1, label="confirmed editor dismissed")


async def _assert_help_key(pilot: Pilot[None], app: KorvidApp, key: str, label: str) -> None:
    if not app.query_one(TopBar).expanded:
        await pilot.press("tilde")
        await until(pilot, lambda: app.query_one(TopBar).expanded)
    await until(
        pilot,
        lambda: any(
            entry.action == "help" and entry.key == label
            for entry in app.query_one(TopBar)._entries
        ),
        label="top bar reflects effective help key",
    )
    assert label in str(app.query_one(TopBar).render())
    entry = next(entry for entry in app._palette_entries() if entry.id == "action:help")
    assert entry.trigger == label
    await pilot.press(key)
    await until(
        pilot, lambda: isinstance(app.screen, HelpScreen), label="effective help key dispatches"
    )
    assert isinstance(app.screen, HelpScreen)
    assert label in app.screen.body_text()
    await pilot.press("escape")


async def test_keyboard_apply_reload_and_reset_keep_all_consumers_in_sync(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    namespace_state = {"context": {"1": "prod", "2": "default"}}
    namespace_path = tmp_path / "namespace-assignments.yaml"
    namespace_path.write_text(yaml.safe_dump(namespace_state), encoding="utf-8")
    original_namespace_state = namespace_path.read_bytes()
    original = {
        "namespace": "default",
        "favorite_namespaces": ["prod", "default"],
        "ui": {"topbar": {"expanded": True}},
    }
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    saver = partial(save_keybindings, path)
    app = make_app([_pod("web")], config=load_config(path), save_keybindings=saver)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        screen = await _open_editor(pilot, app)
        await _stage(pilot, screen, "help", "f1")
        assert app._keybinding_overrides == {}
        assert "keybindings" not in yaml.safe_load(path.read_text(encoding="utf-8"))
        await pilot.press("f9")
        assert app._keybinding_overrides == {}
        await pilot.press("f10")
        await until(pilot, lambda: len(app.screen_stack) == 1)
        assert app.config.keybindings == {"help": "f1"}
        await _assert_help_key(pilot, app, "f1", "f1")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved == {**original, "keybindings": {"help": "f1"}}
    assert namespace_path.read_bytes() == original_namespace_state
    restarted = make_app([_pod("web")], config=load_config(path), save_keybindings=saver)
    async with restarted.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: restarted.query_one(ResourceTable).row_count == 1)
        await _assert_help_key(pilot, restarted, "f1", "f1")
        screen = await _open_editor(pilot, restarted, "keybindings")
        await pilot.press("f8")
        assert screen.edit.overrides == {}
        assert restarted._keybinding_overrides == {"help": "f1"}
        await _confirm(pilot, restarted)
        await _assert_help_key(pilot, restarted, "question_mark", "?")
        assert restarted.config.favorite_namespaces == ("prod", "default")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == original
    assert namespace_path.read_bytes() == original_namespace_state


@pytest.mark.parametrize(
    "expected",
    [
        {"logs": "d", "describe": "l"},
        {"logs": "d", "describe": "g", "relationships": "l"},
        {"logs": "r"},
    ],
    ids=["swap", "rotation", "context-separated"],
)
async def test_saved_permutations_dispatch_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: dict[str, str]
) -> None:
    path = tmp_path / "config.yaml"
    app = make_app([_pod("web")], save_keybindings=partial(save_keybindings, path))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        for action, key in expected.items():
            await _stage(pilot, screen, action, key)
            assert app._keybinding_overrides == {}
        assert not screen.edit.conflicts()
        await _confirm(pilot, app)
        assert app._keybinding_overrides == expected
    restarted = make_app([_pod("web")], config=load_config(path))
    dispatched: list[str] = []
    for action in expected:
        monkeypatch.setattr(restarted, f"action_{action}", partial(dispatched.append, action))
    async with restarted.run_test() as pilot:
        await until(pilot, lambda: restarted.query_one(ResourceTable).row_count == 1)
        assert restarted._keybinding_overrides == expected
        await pilot.press(*expected.values())
        assert dispatched == list(expected)


async def test_cancel_preserves_saved_state_namespace_policy_and_focus(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("favorite_namespaces: [prod]\nkeybindings: {help: f1}\n", encoding="utf-8")
    original = path.read_bytes()
    app = make_app(
        [_pod("web")], config=load_config(path), save_keybindings=partial(save_keybindings, path)
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        await _stage(pilot, screen, "toggle_all_namespaces", "z")
        await pilot.press("f9", "escape")
        await until(pilot, lambda: len(app.screen_stack) == 1)
        assert app._keybinding_overrides == {"help": "f1"}
        assert path.read_bytes() == original
        assert isinstance(app.focused, ResourceTable)
        await pilot.press("1")
        await until(pilot, lambda: app.current_scope == "prod")
        assert app.current_scope == "prod"


async def test_failed_atomic_save_keeps_live_map_and_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("keybindings: {help: f1}\nfavorite_namespaces: [prod]\n", encoding="utf-8")
    original = path.read_bytes()

    def fail_replace(source: str, destination: str) -> None:
        raise PermissionError("test read-only config")

    monkeypatch.setattr("korvid.core.config.os_replace", fail_replace)
    app = make_app(
        [_pod("web")], config=load_config(path), save_keybindings=partial(save_keybindings, path)
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        await _stage(pilot, screen, "help", "f12")
        await pilot.press("f9", "f10")
        assert app.screen is screen
        assert app._keybinding_overrides == {"help": "f1"}
        assert path.read_bytes() == original
        await pilot.press("escape")
        await _assert_help_key(pilot, app, "f1", "f1")


async def test_editor_is_reachable_from_the_existing_action_palette() -> None:
    saved: list[Mapping[str, str]] = []
    app = make_app([_pod("web")], save_keybindings=saved.append)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("ctrl+p")
        await until(pilot, lambda: isinstance(app.screen, ActionPaletteScreen))
        await pilot.press(*"keybindings", "enter")
        await until(pilot, lambda: isinstance(app.screen, KeybindingEditorScreen))
        assert saved == []
        await pilot.press("escape")


async def test_editor_cannot_stack_over_or_confirm_an_approval() -> None:
    saved: list[Mapping[str, str]] = []
    answers: list[bool | None] = []
    app = make_app([_pod("web")], save_keybindings=saved.append)
    async with app.run_test() as pilot:
        approval = ConfirmScreen("Delete pod", "delete pod web")
        app.push_screen(approval, answers.append)
        await until(pilot, lambda: app.screen is approval)
        app.post_message(BuiltinCommand(BuiltinOperation.KEYBINDINGS))
        await until(
            pilot,
            lambda: any("Close the open dialog" in notice.message for notice in app._notifications),
        )
        assert app.screen is approval
        assert answers == []
        assert saved == []
        await pilot.press("y")
        await until(pilot, lambda: answers == [True])
        assert answers == [True]


async def test_editor_priority_controls_and_namespace_slots_cannot_be_stolen() -> None:
    app = make_app(
        [_pod("web")],
        config=KorvidConfig(keybindings={"toggle_agent": "f10", "help": "1"}),
        save_keybindings=lambda proposal: None,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        assert app._keybinding_overrides == {}
        await pilot.press("f1", "ctrl+u", *"help", "tab", "enter", "f2", "ctrl+u", "1", "enter")
        assert screen.edit.overrides == {}
        assert screen.query_one("#keybinding-key", Input).value == "1"
        await pilot.press("escape")


async def test_remapped_zero_and_fixed_favorites_survive_apply_reload_and_reset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("namespace: default\nfavorite_namespaces: [prod]\n", encoding="utf-8")
    saver = partial(save_keybindings, path)
    app = make_app([_pod("web")], config=load_config(path), save_keybindings=saver)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        await _stage(pilot, screen, "toggle_all_namespaces", "z")
        await _confirm(pilot, app)
        await pilot.press("0")
        assert app.current_scope == "default"
        await pilot.press("z")
        await until(pilot, lambda: app.current_scope == ALL_NAMESPACES)
        await pilot.press("1")
        await until(pilot, lambda: app.current_scope == "prod")
        assert app.config.favorite_namespaces == ("prod",)
    restarted = make_app([_pod("web")], config=load_config(path), save_keybindings=saver)
    async with restarted.run_test(size=(120, 40)) as pilot:
        await pilot.press("z")
        await until(pilot, lambda: restarted.current_scope == ALL_NAMESPACES)
        await _open_editor(pilot, restarted)
        await pilot.press("f8")
        await _confirm(pilot, restarted)
        await pilot.press("0")
        await until(pilot, lambda: restarted.current_scope == "default")
        await pilot.press("z")
        assert restarted.current_scope == "default"
        await pilot.press("1")
        await until(pilot, lambda: restarted.current_scope == "prod")
        assert restarted._keybinding_overrides == {}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "namespace": "default",
        "favorite_namespaces": ["prod"],
    }


async def test_direct_editor_route_refuses_an_in_progress_context_switch() -> None:
    app = make_app([_pod("web")], save_keybindings=lambda proposal: None)
    async with app.run_test() as pilot:
        app._ctx._switching = True
        try:
            app.post_message(BuiltinCommand(BuiltinOperation.KEYBINDINGS))
            await until(
                pilot,
                lambda: any("switch" in notice.message.lower() for notice in app._notifications),
            )
            assert len(app.screen_stack) == 1
            assert app._keybinding_overrides == {}
        finally:
            app._ctx._switching = False


@pytest.mark.parametrize(
    ("edit_sequence", "expected_input"),
    [("\x7f", "f1"), ("\x15", ""), ("\x7f2", "f12")],
    ids=["backspace", "clear", "restored-text"],
)
@pytest.mark.parametrize(
    "confirmation", ["\x1b[21~", "\x1b[20~\x1b[21~"], ids=["apply", "review-apply"]
)
async def test_queued_key_edits_require_restaging_and_fresh_review(
    edit_sequence: str, expected_input: str, confirmation: str
) -> None:
    saved: list[Mapping[str, str]] = []
    app = make_app([_pod("web")], save_keybindings=saved.append)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        await _stage(pilot, screen, "help", "f12")
        await pilot.press("f9")
        key_input = screen.query_one("#keybinding-key", Input)
        apply_button = screen.query_one("#keybinding-apply", Button)
        assert key_input.has_focus
        assert not apply_button.disabled
        assert app._driver is not None
        for event in XTermParser().feed(edit_sequence + confirmation):
            event.set_sender(app)
            app._driver.send_message(event)
        await until(
            pilot,
            lambda: key_input.value == expected_input and (saved or apply_button.disabled),
            label="queued input edit invalidates confirmation",
        )
        assert saved == []
        assert app._keybinding_overrides == {}
        assert app.screen is screen
        await pilot.press("f9", "f10")
        assert saved == []
        await _stage(pilot, screen, "help", "f11")
        await _confirm(pilot, app)
        assert saved == [{"help": "f11"}]
        assert app._keybinding_overrides == {"help": "f11"}
