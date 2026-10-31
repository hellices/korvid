"""Copying a resource's name or a describe view's text with `y` (#519).

Each test replaces the app's `copy_to_clipboard` with a recorder, so what is
checked is exactly the text the OSC 52 sequence would carry.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from textual.widgets import Input

from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen, describe_body_text
from korvid.ui.widgets.resource_table import ResourceTable

from .test_describe_search import _EVENTS_LIST, _POD_MANIFEST, _open_describe_screen, make_app
from .test_secret_screen import _SECRET_MANIFEST, _b64, make_secret_app
from .waits import until


def _record(app: KorvidApp) -> list[str]:
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign,assignment]  # record what OSC 52 would carry
    return copied


def _count(copied: list[str], expected: int) -> Callable[[], bool]:
    return lambda: len(copied) == expected


def _notes(app: KorvidApp) -> list[str]:
    return [n.message for n in app._notifications]


async def test_y_copies_the_selected_rows_namespace_and_name() -> None:
    app = make_app()
    copied = _record(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")

        await pilot.press("y")
        await until(pilot, lambda: bool(copied), label="copied")

        assert copied == ["default/my-pod"]
        assert "Copied the selected resource's namespace/name to the clipboard" in _notes(app)


async def test_y_in_the_describe_view_copies_what_it_shows() -> None:
    app = make_app()
    copied = _record(app)
    async with app.run_test() as pilot:
        await _open_describe_screen(pilot, app)

        await pilot.press("y")
        await until(pilot, lambda: bool(copied), label="copied")

        assert copied == [describe_body_text(_POD_MANIFEST, _EVENTS_LIST)]
        assert "Copied the describe view to the clipboard" in _notes(app)


async def test_y_copies_the_open_describe_pane_before_the_row() -> None:
    app = make_app()
    copied = _record(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
        pane = app.query_one(DescribePane)
        pane.show("pods/default/my-pod", dict(_POD_MANIFEST), list(_EVENTS_LIST))
        await until(pilot, lambda: pane.display, label="pane shown")

        await pilot.press("y")
        await until(pilot, lambda: bool(copied), label="copied")

        assert copied == [pane.body_text]
        assert "kind: Pod" in copied[0]


async def test_a_masked_secret_is_copied_masked(tmp_path: Any) -> None:
    from korvid.core.audit import AuditLog

    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    copied = _record(app)
    async with app.run_test() as pilot:
        manifest = json.loads(json.dumps(_SECRET_MANIFEST))
        for expected, share in ((1, True), (2, False)):
            await app._agent_ui._show_describe(share, "secrets/default/db-creds", manifest, [])
            await pilot.pause()
            await pilot.press("y")
            await until(pilot, _count(copied, expected), label="copied")

        assert len(copied) == 2
        for text in copied:
            assert MASK_PLACEHOLDER in text
            assert "hunter2" not in text
            assert _b64("hunter2") not in text
            assert "plain-note" not in text


async def test_a_remapped_copy_key_works_in_the_describe_view_too() -> None:
    app = make_app()
    copied = _record(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
        app.set_keymap({"copy": "x"})
        await pilot.press("x")
        await until(pilot, lambda: len(copied) == 1, label="row copied")
        await _open_describe_screen(pilot, app)

        search = app.screen.query_one(Input)
        await pilot.press("y", "slash")
        # The search opens only once `y` has been handled, and `y` copied nothing.
        await until(pilot, lambda: search.display, label="search open")
        assert len(copied) == 1
        await pilot.press("escape")
        await until(pilot, lambda: not search.display, label="search closed")
        await pilot.press("x")
        await until(pilot, lambda: len(copied) == 2, label="view copied")

        assert isinstance(app.screen, DescribeScreen)
        assert copied == ["default/my-pod", describe_body_text(_POD_MANIFEST, _EVENTS_LIST)]
