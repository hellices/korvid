"""Keep fixed modal controls ahead of remapped priority app actions."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping

import pytest
from textual.binding import Binding
from textual.dom import DOMNode
from textual.screen import ModalScreen
from textual.widgets import Input

from korvid.core.config import KorvidConfig
from korvid.core.keybindings import canonical_key
from korvid.ui import widgets
from korvid.ui.app_bindings import APP_BINDINGS, as_binding
from korvid.ui.keybinding_catalog import KeybindingCatalog
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.help_screen import HelpScreen

from .test_app import _pod, make_app
from .test_keybinding_editor_workflow import _open_editor, _stage
from .waits import until


def _modal(screen_kind: str) -> ModalScreen[None]:
    if screen_kind == "help":
        return HelpScreen([], [])
    return DescribeScreen("pod/web", {"kind": "Pod"}, [])


@pytest.mark.parametrize("action", ["toggle_agent", "interrupt_agent", "open_action_palette"])
def test_moving_quit_does_not_free_modal_dismissal_for_priority_actions(action: str) -> None:
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan({"quit": "f12", action: "q"})
    assert plan.overrides == {"quit": "f12"}
    assert any("q" in warning for warning in plan.warnings)


@pytest.mark.parametrize("action", ["toggle_agent", "interrupt_agent"])
@pytest.mark.parametrize("screen_kind", ["help", "describe"])
async def test_startup_multi_edit_cannot_intercept_modal_dismissal(
    monkeypatch: pytest.MonkeyPatch, action: str, screen_kind: str
) -> None:
    intercepted: list[str] = []
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"quit": "f12", action: "q"}))
    monkeypatch.setattr(app._actions, "_agent_available", lambda: True)
    monkeypatch.setattr(app, f"action_{action}", lambda: intercepted.append(action))
    async with app.run_test() as pilot:
        modal = _modal(screen_kind)
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await pilot.press("q")
        await until(pilot, lambda: app.screen is not modal or intercepted)
        assert intercepted == []
        assert app.screen is not modal
        assert app._keybinding_overrides == {"quit": "f12"}


@pytest.mark.parametrize("action", ["toggle_agent", "interrupt_agent", "open_action_palette"])
async def test_editor_rejects_priority_modal_key_after_staging_quit_elsewhere(
    action: str,
) -> None:
    saved: list[Mapping[str, str]] = []
    app = make_app([_pod("web")], save_keybindings=saved.append)
    async with app.run_test(size=(120, 40)) as pilot:
        editor = await _open_editor(pilot, app)
        await _stage(pilot, editor, "quit", "f12")
        await pilot.press("f1", "ctrl+u", *action, "tab", "enter", "f2", "ctrl+u", "q", "enter")
        assert editor.edit.overrides == {"quit": "f12"}
        assert editor.query_one("#keybinding-key", Input).value == "q"
        await pilot.press("f9", "f10")
        assert app.screen is editor
        assert saved == []
        assert app._keybinding_overrides == {}
        await pilot.press("escape")


def test_nonpriority_actions_can_reuse_modal_keys_outside_a_modal() -> None:
    overrides = {"quit": "f12", "help": "q"}
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan(overrides)
    assert plan.overrides == overrides
    assert not plan.warnings


def test_every_declared_modal_control_is_reserved_from_priority_remapping() -> None:
    expected: set[str] = set()
    for module_info in pkgutil.iter_modules(widgets.__path__, widgets.__name__ + "."):
        module = importlib.import_module(module_info.name)
        for candidate in vars(module).values():
            if not isinstance(candidate, type) or not issubclass(candidate, ModalScreen):
                continue
            if candidate.__module__ != module.__name__:
                continue
            for ancestor in candidate.__mro__:
                if issubclass(ancestor, DOMNode):
                    expected.update(
                        canonical_key(key.strip())
                        for raw in ancestor.BINDINGS
                        for key in as_binding(raw).key.split(",")
                    )
    catalog = KeybindingCatalog(APP_BINDINGS)
    assert expected
    assert expected <= set(catalog.rules.priority_reserved_keys)


def test_priority_reservations_follow_live_modal_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        HelpScreen,
        "BINDINGS",
        [*HelpScreen.BINDINGS, Binding("alt+f12", "dismiss", "Close")],
    )
    plan = KeybindingCatalog(APP_BINDINGS).rules.plan({"toggle_agent": "alt+f12"})
    assert plan.overrides == {}
    assert any("dismiss" in warning for warning in plan.warnings)
