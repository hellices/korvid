"""Tests for issue #42: search (/) inside describe and YAML views."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual.widgets import Input, Static

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import PodSummary
from korvid.ui.app import EventsFetcher, KorvidApp
from korvid.ui.widgets.describe_screen import (
    BodySearch,
    DescribePane,
    DescribeScreen,
)
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))

_ALIASES: dict[str, ResourceMeta] = {"pods": _PODS_META, "po": _PODS_META, "pod": _PODS_META}

_POD_MANIFEST = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "my-pod", "namespace": "default"},
    "spec": {"containers": [{"name": "app", "image": "nginx:latest"}]},
    "status": {"phase": "Running"},
}

_EVENTS_LIST = [
    {
        "type": "Normal",
        "reason": "Pulled",
        "lastTimestamp": "2024-01-01T00:00:00Z",
        "message": "pulled nginx image",
    },
    {
        "type": "Warning",
        "reason": "BackOff",
        "lastTimestamp": "2024-01-01T00:01:00Z",
        "message": "nginx restarting",
    },
]


def _pod(name: str, namespace: str = "default") -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
    )


def _fake_source(pods: list[PodSummary]) -> Any:
    async def source(kind: str, scope: str) -> Any:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


class _FnEvents(EventsFetcher):
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._fn(namespace, name)  # type: ignore[no-any-return]  # test fake returns list[dict]


def make_app() -> KorvidApp:
    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return list(_EVENTS_LIST)

    store = ResourceStore()
    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _fake_source([_pod("my-pod")])),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest,
        get_events=_FnEvents(get_events),
    )


async def _open_describe_screen(pilot: Any, app: KorvidApp) -> DescribeScreen:
    """Wait for the pod row, press d, and wait for the modal."""
    await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
    await pilot.press("d")
    await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe screen")
    screen = app.screen
    assert isinstance(screen, DescribeScreen)
    return screen


async def _submit_search(pilot: Any, host: Any, input_id: str, pattern: str) -> None:
    """Open the inline search, type *pattern*, and submit it."""
    await pilot.press("slash")
    await until(pilot, lambda: host.query_one(input_id, Input).display, label="search input open")
    for ch in pattern:
        await pilot.press(ch)
    await pilot.press("enter")
    await until(
        pilot, lambda: not host.query_one(input_id, Input).display, label="search submitted"
    )


def _pane_title(pane: DescribePane) -> str:
    return str(pane.query_one("#describe-pane-title", Static).content)


# ---------------------------------------------------------------------------
# BodySearch unit tests
# ---------------------------------------------------------------------------


def _search(pattern: str) -> BodySearch:
    body = BodySearch()
    body.set_body("name: my-pod\nimage: nginx:latest\n", Text("Pulled  pulled nginx image"))
    body.run(pattern)
    return body


def test_body_search_is_case_insensitive() -> None:
    body = _search("NGINX")
    assert len(body.hits) == 2


def test_body_search_empty_pattern_returns_no_hits() -> None:
    body = _search("")
    assert body.hits == []
    assert body.counter == ""


def test_body_search_counter_and_wraparound() -> None:
    body = _search("nginx")
    assert body.counter == "1/2"
    body.next()
    assert body.counter == "2/2"
    body.next()
    assert body.counter == "1/2"
    body.prev()
    assert body.counter == "2/2"


def test_body_search_yaml_highlights_are_one_based_yaml_lines() -> None:
    body = _search("nginx")
    # "image: nginx:latest" is yaml line 2 (1-based); the event hit is not a
    # yaml line so it must not appear in the yaml highlight set.
    assert body.yaml_highlights() == {2}


def test_body_search_current_line_spans_yaml_and_events() -> None:
    body = _search("nginx")
    # First hit: yaml line index 1 (0-based). Second hit: first events line,
    # displayed after 2 yaml lines + 3 separator lines (blank/EVENTS/rule).
    assert body.current_line == 1
    body.next()
    assert body.current_line == 2 + 3


def test_body_search_no_match_keeps_empty_state() -> None:
    body = _search("zzz-not-there")
    assert body.hits == []
    assert body.counter == ""
    assert body.current_line is None


def test_body_search_display_row_accounts_for_wrapped_lines() -> None:
    """With word wrap, a long line before the hit occupies extra display rows."""
    body = BodySearch()
    long_line = "annotation: " + "x" * 100  # wraps into several rows at width 40
    body.set_body(f"{long_line}\nimage: nginx\n", Text("no events"))
    body.run("nginx")
    # At an ample width nothing wraps: row == source line index.
    assert body.display_row(width=400) == 1
    # At width 40 the 112-char first line needs >= 3 rows, pushing the hit down.
    row = body.display_row(width=40)
    assert row is not None
    assert row >= 3


def test_body_search_display_row_none_without_hits() -> None:
    body = _search("zzz-not-there")
    assert body.display_row(width=80) is None


# ---------------------------------------------------------------------------
# DescribeScreen (modal) interaction tests
# ---------------------------------------------------------------------------


async def test_slash_opens_search_input_in_describe_screen() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        screen = await _open_describe_screen(pilot, app)
        await pilot.press("slash")
        await until(
            pilot,
            lambda: screen.query_one("#describe-search", Input).display,
            label="search input visible",
        )
        assert screen.query_one("#describe-search", Input).display is True


async def test_submit_shows_counter_and_n_advances() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        screen = await _open_describe_screen(pilot, app)
        await _submit_search(pilot, screen, "#describe-search", "nginx")
        await until(pilot, lambda: "1/" in str(screen.sub_title), label="counter 1/N")
        await pilot.press("n")
        await until(pilot, lambda: "2/" in str(screen.sub_title), label="counter 2/N")
        assert "2/" in str(screen.sub_title)


async def test_shift_n_wraps_backwards() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        screen = await _open_describe_screen(pilot, app)
        await _submit_search(pilot, screen, "#describe-search", "nginx")
        await until(pilot, lambda: "1/" in str(screen.sub_title), label="counter 1/N")
        total = int(str(screen.sub_title).split("/")[1])
        await pilot.press("N")
        await until(
            pilot,
            lambda: str(screen.sub_title) == f"{total}/{total}",
            label="counter wrapped to last hit",
        )
        assert str(screen.sub_title) == f"{total}/{total}"


async def test_escape_closes_search_input_before_screen() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        screen = await _open_describe_screen(pilot, app)
        await _submit_search(pilot, screen, "#describe-search", "nginx")
        await pilot.press("slash")
        await until(
            pilot,
            lambda: screen.query_one("#describe-search", Input).display,
            label="search input reopened",
        )
        # First escape: close the input and clear the counter, keep the screen.
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not screen.query_one("#describe-search", Input).display,
            label="search input closed",
        )
        assert isinstance(app.screen, DescribeScreen)
        assert str(screen.sub_title) == ""
        # Second escape: dismiss the screen itself.
        await pilot.press("escape")
        await until(
            pilot, lambda: not isinstance(app.screen, DescribeScreen), label="screen dismissed"
        )
        assert not isinstance(app.screen, DescribeScreen)


async def test_no_match_shows_no_counter() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        screen = await _open_describe_screen(pilot, app)
        await _submit_search(pilot, screen, "#describe-search", "zzz")
        assert str(screen.sub_title) == ""


# ---------------------------------------------------------------------------
# DescribePane (non-modal, agent-shared) tests
# ---------------------------------------------------------------------------


async def _open_pane(pilot: Any, app: KorvidApp) -> DescribePane:
    pane = app.query_one(DescribePane)
    pane.show("Pod: my-pod", dict(_POD_MANIFEST), list(_EVENTS_LIST))
    await until(pilot, lambda: pane.display, label="describe pane visible")
    return pane


async def test_slash_routes_to_describe_pane_search_when_open() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        pane = await _open_pane(pilot, app)
        await pilot.press("slash")
        await until(
            pilot,
            lambda: pane.query_one("#describe-pane-search", Input).display,
            label="pane search input visible",
        )
        assert pane.query_one("#describe-pane-search", Input).display is True


async def test_describe_pane_search_counter_and_n_navigation() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        pane = await _open_pane(pilot, app)
        await _submit_search(pilot, pane, "#describe-pane-search", "nginx")
        await until(pilot, lambda: "1/" in _pane_title(pane), label="pane counter 1/N")
        await pilot.press("n")
        await until(pilot, lambda: "2/" in _pane_title(pane), label="pane counter 2/N")
        assert "2/" in _pane_title(pane)


async def test_describe_pane_highlights_yaml_hits_after_submit() -> None:
    """Submitting a pane search re-renders the body with YAML hit lines highlighted."""
    app = make_app()
    async with app.run_test() as pilot:
        pane = await _open_pane(pilot, app)
        await _submit_search(pilot, pane, "#describe-pane-search", "nginx")
        body = pane.query_one("#describe-pane-body", Static)
        # The body Group's first renderable is the Syntax; it must carry the
        # yaml highlight lines for the matched pattern.
        syntax = body.content.renderables[0]  # type: ignore[union-attr]  # Group in tests
        assert syntax.highlight_lines, "expected yaml hit lines to be highlighted"


async def test_describe_pane_escape_after_submit_clears_search_before_closing() -> None:
    """After a submitted search, Escape clears the search state; the pane stays open."""
    app = make_app()
    async with app.run_test() as pilot:
        pane = await _open_pane(pilot, app)
        await _submit_search(pilot, pane, "#describe-pane-search", "nginx")
        await until(pilot, lambda: "1/" in _pane_title(pane), label="pane counter shown")
        # First escape: search state cleared, pane still open.
        await pilot.press("escape")
        await until(pilot, lambda: "1/" not in _pane_title(pane), label="counter cleared")
        assert pane.display is True
        # Second escape: now the App closes the pane.
        await pilot.press("escape")
        await until(pilot, lambda: not pane.display, label="pane closed")
        assert pane.display is False


async def test_describe_pane_escape_closes_search_not_pane() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        pane = await _open_pane(pilot, app)
        await pilot.press("slash")
        await until(
            pilot,
            lambda: pane.query_one("#describe-pane-search", Input).display,
            label="pane search input visible",
        )
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not pane.query_one("#describe-pane-search", Input).display,
            label="pane search input closed",
        )
        # Search input closed; the pane itself stays open.
        assert pane.display is True
        await pilot.press("escape")
        await until(pilot, lambda: not pane.display, label="pane closed")
        assert pane.display is False
