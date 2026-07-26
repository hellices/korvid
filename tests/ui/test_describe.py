"""Tests for Task 5: Describe view (d key)."""

from __future__ import annotations

import asyncio
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import PodSummary
from korvid.ui.app import EventsFetcher, KorvidApp

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))

_DEFAULT_TEST_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "po": _PODS_META,
    "pod": _PODS_META,
}

_POD_MANIFEST = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "my-pod",
        "namespace": "default",
        "managedFields": [{"manager": "kubectl", "operation": "Apply"}],
    },
    "spec": {"containers": [{"name": "app", "image": "nginx:latest"}]},
    "status": {"phase": "Running"},
}

_EVENTS_LIST = [
    {
        "type": "Normal",
        "reason": "Pulled",
        "lastTimestamp": "2024-01-01T00:00:00Z",
        "message": "Successfully pulled image",
        "involvedObject": {"name": "my-pod"},
    }
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


def fake_source(pods: list[PodSummary]) -> Any:
    async def source(kind: str, scope: str) -> Any:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


class _FnEvents(EventsFetcher):
    """Adapts a plain `(namespace, name)` coroutine fn to the fetcher ABC."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._fn(namespace, name)  # type: ignore[no-any-return]  # test fakes return list[dict]


def make_describe_app(
    pods: list[PodSummary],
    *,
    get_manifest: Any = None,
    get_events: Any = None,
) -> KorvidApp:
    store = ResourceStore()
    if get_events is not None and not isinstance(get_events, EventsFetcher):
        get_events = _FnEvents(get_events)
    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, fake_source(pods)),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_manifest=get_manifest,
        get_events=get_events,
    )


def _rendered_plain(widget: Any) -> str:
    """Render a Static widget's content to plain text via a Rich console."""
    from rich.console import Console

    console = Console(width=400, force_terminal=False, legacy_windows=False)
    with console.capture() as capture:
        console.print(widget.content)
    return capture.get()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_d_opens_describe_screen_with_pod_name_and_events() -> None:
    """Pressing d with a selected pod opens DescribeScreen with pod name and EVENTS section."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return list(_EVENTS_LIST)

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        # DescribeScreen should be pushed as a modal
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        content = _rendered_plain(screen.query_one("#describe-body"))
        assert "my-pod" in str(content)
        assert "EVENTS" in str(content)


async def test_managed_fields_stripped_from_describe() -> None:
    """managedFields is stripped from the YAML dump."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        content = _rendered_plain(screen.query_one("#describe-body"))
        assert "managedFields" not in content


async def test_events_rendered_in_describe_screen() -> None:
    """Events section shows the event reason when events exist."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return list(_EVENTS_LIST)

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        content = _rendered_plain(screen.query_one("#describe-body"))
        assert "Pulled" in content


async def test_no_events_shows_no_events_placeholder() -> None:
    """When event list is empty, '<no events>' is shown."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, DescribeScreen)
        content = _rendered_plain(screen.query_one("#describe-body"))
        assert "<no events>" in content


async def test_esc_dismisses_describe_screen() -> None:
    """Pressing Esc on DescribeScreen pops the modal."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DescribeScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        # Modal should be dismissed; main app screen should be back
        assert not isinstance(app.screen, DescribeScreen)


async def test_q_in_describe_screen_dismisses_not_quit_app() -> None:
    """Pressing q on DescribeScreen dismisses the modal, does NOT quit the app."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DescribeScreen)
        await pilot.press("q")
        await pilot.pause(0.2)
        # Modal dismissed, app still running (not exited)
        assert not isinstance(app.screen, DescribeScreen)
        # app should still be running (not exited)
        assert app.is_running


async def test_get_manifest_403_shows_rbac_notification_no_modal() -> None:
    """ApiStatusError(403) from get_manifest shows RBAC notification and does not push modal."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(403, "Forbidden")

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        # DescribeScreen should NOT be mounted
        assert not isinstance(app.screen, DescribeScreen)
        # Notification should contain "RBAC"
        notifications = [n.message for n in app._notifications]
        assert any("RBAC" in m for m in notifications)


async def test_no_row_selected_shows_warning_no_crash() -> None:
    """d with empty table shows a warning notification and does not crash."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return dict(_POD_MANIFEST)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, DescribeScreen)
        notifications = [n.message for n in app._notifications]
        assert len(notifications) > 0


async def test_describe_unavailable_when_no_get_manifest() -> None:
    """d with no get_manifest injected does nothing (or notifies unavailable)."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    app = make_describe_app([_pod("my-pod")], get_manifest=None, get_events=None)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, DescribeScreen)


async def test_describe_non_pod_kind_skips_events() -> None:
    """Events filter only by name, so non-pod kinds must not fetch events."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    events_calls: list[str] = []

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return {"kind": "Deployment", "metadata": {"name": name, "namespace": namespace}}

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        events_calls.append(name)
        return list(_EVENTS_LIST)

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        app.current_kind = "deployments"
        await pilot.press("d")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DescribeScreen)
        assert events_calls == []
        content = _rendered_plain(app.screen.query_one("#describe-body"))
        assert "<no events>" in content


async def test_describe_body_renders_bracketed_text_literally() -> None:
    """Cluster-controlled text with Rich-markup-like brackets must render as-is."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return {
            "kind": "Pod",
            "metadata": {"name": name, "annotations": {"note": "[red]not-a-style[/red]"}},
        }

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DescribeScreen)
        content = _rendered_plain(app.screen.query_one("#describe-body"))
        assert "[red]not-a-style[/red]" in content


async def test_describe_unknown_kind_notifies_error() -> None:
    """get_manifest raising ValueError (unknown kind) surfaces a notification, no crash."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise ValueError(f"Unknown resource kind: {kind!r}")

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return []

    app = make_describe_app([_pod("my-pod")], get_manifest=get_manifest, get_events=get_events)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, DescribeScreen)
        notifications = [n.message for n in app._notifications]
        assert any("Unknown resource kind" in m for m in notifications)


# ---------------------------------------------------------------------------
# Visual styling (rich renderable body)
# ---------------------------------------------------------------------------


def test_describe_body_yaml_is_syntax_highlighted() -> None:
    """The YAML section renders through rich.Syntax with the ansi_dark theme."""
    from rich.console import Group
    from rich.syntax import Syntax

    from korvid.ui.widgets.describe_screen import _describe_body

    body = _describe_body(dict(_POD_MANIFEST), list(_EVENTS_LIST))
    assert isinstance(body, Group)
    syntax = body.renderables[0]
    assert isinstance(syntax, Syntax)
    assert syntax.lexer is not None
    assert "my-pod" in syntax.code


def test_warning_events_are_red() -> None:
    """Warning event lines carry a red style; Normal lines do not."""
    from rich.text import Text

    from korvid.ui.widgets.describe_screen import _render_events

    events = [
        {"type": "Normal", "reason": "Pulled", "message": "ok"},
        {"type": "Warning", "reason": "BackOff", "message": "restarting failed container"},
    ]
    rendered = _render_events(events)
    assert isinstance(rendered, Text)
    styles = [str(span.style) for span in rendered.spans]
    assert any("red" in s for s in styles)
    warning_line = next(line for line in rendered.plain.splitlines() if line.startswith("Warning"))
    assert "BackOff" in warning_line


def test_describe_body_text_stays_plain_string() -> None:
    """describe_body_text keeps a plain-str body for the agent bridge."""
    from korvid.ui.widgets.describe_screen import describe_body_text

    text = describe_body_text(dict(_POD_MANIFEST), list(_EVENTS_LIST))
    assert isinstance(text, str)
    assert "my-pod" in text
    assert "EVENTS" in text
    assert "Pulled" in text
