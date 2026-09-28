"""Rejected persisted bindings are cleaned only by a reviewed, reversible edit."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from pathlib import Path

import pytest
import yaml
from textual.pilot import Pilot
from textual.widgets import Button, Input, Static

from korvid.core.config import load_config
from korvid.core.keybinding_config import save_keybindings
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .waits import until


async def _open_editor(pilot: Pilot[None], app: KorvidApp) -> KeybindingEditorScreen:
    await pilot.press("colon", *"keys", "enter")
    await until(pilot, lambda: isinstance(app.screen, KeybindingEditorScreen), label="editor open")
    assert isinstance(app.screen, KeybindingEditorScreen)
    screen = app.screen
    await until(pilot, lambda: screen.query_one("#keybinding-search", Input).has_focus)
    return screen


@pytest.mark.parametrize(
    "section",
    [{"help": 1}, True, 1, False, [], [["help", "f1"]]],
    ids=["rejected-entry", "boolean", "integer", "false", "empty-list", "pairs"],
)
async def test_rejected_only_cleanup_requires_confirmation_and_stops_restart_warnings(
    tmp_path: Path, section: object
) -> None:
    path = tmp_path / "config.yaml"
    settings = {
        "favorite_namespaces": ["prod", "default"],
        "log_buffer_lines": 800,
        "ui": {"topbar": {"expanded": True}},
    }
    path.write_text(yaml.safe_dump({**settings, "keybindings": section}), encoding="utf-8")
    original = path.read_bytes()
    saved: list[dict[str, str]] = []

    def save(proposal: Mapping[str, str]) -> None:
        save_keybindings(path, proposal)
        saved.append(dict(proposal))

    app = make_app([_pod("web")], config=load_config(path), save_keybindings=save)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        assert any("keybindings:" in notice.message for notice in app._notifications)
        if not isinstance(section, dict):
            warnings = [
                notice.message
                for notice in app._notifications
                if notice.message.startswith("keybindings:")
            ]
            assert len(warnings) == 1
            assert all(
                fragment in warnings[0] for fragment in ("mapping", ":keys", "F8", "F9", "F10")
            )
        assert app._keybinding_overrides == {}
        screen = await _open_editor(pilot, app)
        assert not screen.query_one("#keybinding-reset-all", Button).disabled
        assert screen.query_one("#keybinding-review", Button).disabled
        assert screen.query_one("#keybinding-apply", Button).disabled
        await pilot.press("f9", "f10")
        assert saved == []
        await pilot.press("f8")
        assert screen.edit.overrides == {}
        assert screen.edit.dirty
        preview = str(screen.query_one("#keybinding-preview", Static).render()).lower()
        assert "rejected persisted" in preview
        assert "no changes" not in preview
        assert not screen.query_one("#keybinding-review", Button).disabled
        assert screen.query_one("#keybinding-apply", Button).disabled
        await pilot.press("f10", "f9")
        assert not screen.query_one("#keybinding-apply", Button).disabled
        assert app._keybinding_overrides == {}
        assert path.read_bytes() == original
        assert saved == []
        screen.query_one("#keybinding-apply", Button).focus()
        await pilot.press("enter")
        assert saved == []
        await pilot.press("f10")
        await until(pilot, lambda: len(app.screen_stack) == 1)
        assert saved == [{}]
        assert app.config.keybindings == {}
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == settings
        screen = await _open_editor(pilot, app)
        assert screen.query_one("#keybinding-reset-all", Button).disabled
        await pilot.press("f8", "f9", "f10")
        assert saved == [{}]
        assert screen.query_one("#keybinding-apply", Button).disabled
        await pilot.press("escape")

    restarted = make_app(
        [_pod("web")], config=load_config(path), save_keybindings=partial(save_keybindings, path)
    )
    async with restarted.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: restarted.query_one(ResourceTable).row_count == 1)
        assert restarted._keybinding_overrides == {}
        assert not any("keybindings:" in notice.message for notice in restarted._notifications)
        screen = await _open_editor(pilot, restarted)
        assert screen.query_one("#keybinding-reset-all", Button).disabled
        assert screen.query_one("#keybinding-apply", Button).disabled
        await pilot.press("escape")


@pytest.mark.parametrize("control", ["escape", "f6"], ids=["cancel", "undo"])
@pytest.mark.parametrize(
    ("section", "safe"),
    [({"help": 1}, {}), ({"help": 1, "logs": "r"}, {"logs": "r"}), (True, {}), ([], {})],
    ids=["rejected-only", "mixed", "boolean-section", "empty-list-section"],
)
async def test_cancel_or_undo_preserves_rejected_and_safe_persisted_entries(
    tmp_path: Path, control: str, section: object, safe: dict[str, str]
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"favorite_namespaces": ["prod"], "keybindings": section}),
        encoding="utf-8",
    )
    original = path.read_bytes()
    app = make_app(
        [_pod("web")], config=load_config(path), save_keybindings=partial(save_keybindings, path)
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        await pilot.press("f8", "f9")
        assert screen.edit.cleanup_pending
        preview = str(screen.query_one("#keybinding-preview", Static).render()).lower()
        assert "rejected persisted" in preview
        if safe:
            assert "logs: r -> l" in preview
        await pilot.press(control)
        if control == "f6":
            assert screen.edit.overrides == safe
            assert not screen.edit.dirty
            assert not screen.edit.cleanup_pending
            assert screen.query_one("#keybinding-apply", Button).disabled
            await pilot.press("f8", "f10")
            assert screen.edit.cleanup_pending
            assert app.screen is screen
            assert screen.query_one("#keybinding-apply", Button).disabled
            await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1)
        assert app._keybinding_overrides == safe
        assert app.config.keybindings == safe
        assert path.read_bytes() == original
        reopened = await _open_editor(pilot, app)
        assert not reopened.query_one("#keybinding-reset-all", Button).disabled
        assert not reopened.edit.dirty
        await pilot.press("f8")
        assert reopened.edit.cleanup_pending
        assert path.read_bytes() == original
        await pilot.press("escape")


@pytest.mark.parametrize(
    ("section", "safe"),
    [({"help": 1}, {}), ({"help": 1, "logs": "r"}, {"logs": "r"}), (1, {}), (False, {})],
    ids=["rejected-only", "mixed", "integer-section", "false-section"],
)
async def test_failed_atomic_cleanup_can_be_reopened_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: object, safe: dict[str, str]
) -> None:
    path = tmp_path / "config.yaml"
    settings = {"favorite_namespaces": ["prod"], "log_buffer_lines": 800}
    path.write_text(yaml.safe_dump({**settings, "keybindings": section}), encoding="utf-8")
    original = path.read_bytes()
    attempts: list[Path] = []

    def fail_replace(source: Path, destination: Path) -> None:
        attempts.append(destination)
        raise PermissionError("test read-only config")

    app = make_app(
        [_pod("web")], config=load_config(path), save_keybindings=partial(save_keybindings, path)
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        await pilot.press("f8")
        assert screen.edit.dirty
        with monkeypatch.context() as patch:
            patch.setattr("korvid.core.config.os_replace", fail_replace)
            await pilot.press("f9", "f10")
            assert app.screen is screen
            assert screen.edit.cleanup_pending
            assert "read-only config" in str(
                screen.query_one("#keybinding-status", Static).render()
            )
            assert app._keybinding_overrides == safe
            assert app.config.keybindings == safe
            assert path.read_bytes() == original
            assert len(attempts) == 1
            await pilot.press("f10")
            assert len(attempts) == 1
            assert screen.query_one("#keybinding-apply", Button).disabled
        await pilot.press("escape")
        reopened = await _open_editor(pilot, app)
        assert not reopened.query_one("#keybinding-reset-all", Button).disabled
        assert not reopened.edit.dirty
        await pilot.press("f9", "f10")
        assert app.screen is reopened
        assert path.read_bytes() == original
        await pilot.press("f8")
        assert reopened.edit.cleanup_pending
        assert app._keybinding_overrides == safe
        assert path.read_bytes() == original
        await pilot.press("f9", "f10")
        await until(pilot, lambda: len(app.screen_stack) == 1)
        assert app._keybinding_overrides == {}
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == settings
        reopened = await _open_editor(pilot, app)
        assert reopened.query_one("#keybinding-reset-all", Button).disabled
        await pilot.press("escape")


@pytest.mark.parametrize("content", ["", "keybindings: {}\n", "keybindings: null\n"])
async def test_absent_or_empty_sections_leave_cleanup_and_apply_disabled(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    saved: list[Mapping[str, str]] = []
    app = make_app([_pod("web")], config=load_config(path), save_keybindings=saved.append)

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_editor(pilot, app)
        assert not any("keybindings:" in notice.message for notice in app._notifications)
        assert screen.query_one("#keybinding-reset-all", Button).disabled
        assert screen.query_one("#keybinding-review", Button).disabled
        assert screen.query_one("#keybinding-apply", Button).disabled
        await pilot.press("f8", "f9", "f10")
        assert app.screen is screen
        assert not screen.edit.dirty
        assert not screen.edit.cleanup_pending
        assert saved == []
        assert path.read_bytes() == original
        await pilot.press("escape")
