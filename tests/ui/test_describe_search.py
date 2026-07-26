"""Tests for issue #42: search (/) inside describe and YAML views."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text

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


# ---------------------------------------------------------------------------
# DescribeScreen (modal) interaction tests
# ---------------------------------------------------------------------------


async def test_slash_opens_search_input_in_describe_screen() -> None:
    from textual.widgets import Input

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DescribeScreen)
        await pilot.press("slash")
        await pilot.pause(0.05)
        assert app.screen.query_one("#describe-search", Input).display is True


async def test_submit_shows_counter_and_n_advances() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        await pilot.press("slash")
        await pilot.pause(0.05)
        for ch in "nginx":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        assert "1/" in str(screen.sub_title)
        await pilot.press("n")
        await pilot.pause(0.05)
        assert "2/" in str(screen.sub_title)


async def test_shift_n_wraps_backwards() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        await pilot.press("slash")
        for ch in "nginx":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        total = int(str(screen.sub_title).split("/")[1])
        await pilot.press("N")
        await pilot.pause(0.05)
        assert str(screen.sub_title) == f"{total}/{total}"


async def test_escape_closes_search_input_before_screen() -> None:
    from textual.widgets import Input

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        await pilot.press("slash")
        for ch in "nginx":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        await pilot.press("slash")
        await pilot.pause(0.05)
        # First escape: close the input and clear the counter, keep the screen.
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert isinstance(app.screen, DescribeScreen)
        assert app.screen.query_one("#describe-search", Input).display is False
        assert str(app.screen.sub_title) == ""
        # Second escape: dismiss the screen itself.
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, DescribeScreen)


async def test_no_match_shows_no_counter() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        await pilot.press("slash")
        for ch in "zzz":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        assert str(screen.sub_title) == ""


# ---------------------------------------------------------------------------
# DescribePane (non-modal, agent-shared) tests
# ---------------------------------------------------------------------------


async def test_slash_routes_to_describe_pane_search_when_open() -> None:
    from textual.widgets import Input

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        pane = app.query_one(DescribePane)
        pane.show("Pod: my-pod", dict(_POD_MANIFEST), list(_EVENTS_LIST))
        await pilot.pause(0.05)
        await pilot.press("slash")
        await pilot.pause(0.05)
        assert pane.query_one("#describe-pane-search", Input).display is True


async def test_describe_pane_search_counter_and_n_navigation() -> None:
    from textual.widgets import Static

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        pane = app.query_one(DescribePane)
        pane.show("Pod: my-pod", dict(_POD_MANIFEST), list(_EVENTS_LIST))
        await pilot.pause(0.05)
        await pilot.press("slash")
        for ch in "nginx":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        title = str(pane.query_one("#describe-pane-title", Static).content)
        assert "1/" in title
        await pilot.press("n")
        await pilot.pause(0.05)
        title = str(pane.query_one("#describe-pane-title", Static).content)
        assert "2/" in title


async def test_describe_pane_escape_closes_search_not_pane() -> None:
    from textual.widgets import Input

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        pane = app.query_one(DescribePane)
        pane.show("Pod: my-pod", dict(_POD_MANIFEST), list(_EVENTS_LIST))
        await pilot.pause(0.05)
        await pilot.press("slash")
        await pilot.pause(0.05)
        await pilot.press("escape")
        await pilot.pause(0.05)
        # Search input closed; the pane itself stays open.
        assert pane.query_one("#describe-pane-search", Input).display is False
        assert pane.display is True
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert pane.display is False
