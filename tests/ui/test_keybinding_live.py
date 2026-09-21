"""Exercise complete live keymaps through Textual dispatch."""

from __future__ import annotations

from functools import partial

import pytest

from korvid.core.config import KorvidConfig
from korvid.k8s.helm import HELM_REVISIONS_META
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.top_bar import TopBar

from .test_app import _pod, make_app
from .waits import until


async def test_context_separated_remap_dispatches_each_shared_key_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"logs": "r"}))
    app.aliases["helmrevisions"] = HELM_REVISIONS_META
    dispatched: list[str] = []
    for action in ("logs", "rollout_restart", "helm_rollback"):
        monkeypatch.setattr(app, f"action_{action}", partial(dispatched.append, action))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        assert app._keybinding_overrides == {"logs": "r"}
        for kind, action in (
            ("pods", "logs"),
            ("deployments", "rollout_restart"),
            ("helmrevisions", "helm_rollback"),
        ):
            app.current_kind = kind
            await pilot.press("r")
            assert dispatched[-1] == action
        assert len(dispatched) == 3


async def test_topbar_prefers_the_remapped_quit_key_to_the_inherited_fixed_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"quit": "f12"}))
    invoked: list[str] = []
    monkeypatch.setattr(app, "action_quit", lambda: invoked.append("quit"))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        entry = next(entry for entry in app.query_one(TopBar)._entries if entry.action == "quit")
        assert entry.key == "f12"
        palette_entry = next(entry for entry in app._palette_entries() if entry.id == "action:quit")
        assert palette_entry.trigger == "f12"
        await pilot.press("q")
        assert invoked == []
        await pilot.press("f12")
        assert invoked == ["quit"]


async def test_control_space_override_dispatches_legacy_and_extended_spellings() -> None:
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"help": "ctrl+space"}))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        for key in ("ctrl+space", "ctrl+@", "ctrl+at"):
            await pilot.press(key)
            await until(pilot, lambda: isinstance(app.screen, HelpScreen))
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
