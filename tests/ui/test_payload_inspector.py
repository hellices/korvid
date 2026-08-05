"""Tests for the final redacted provider-payload inspector."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from korvid.agent.outbound import OutboundPolicy, OutboundSnapshot
from korvid.core.private_export import default_payload_export_dir
from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen
from tests.ui.waits import until


class HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


def _snapshot() -> OutboundSnapshot:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system context: diagnose Kubernetes"},
        {
            "role": "user",
            "content": (
                "screen context: view=pods namespace=default "
                "Authorization: Bearer raw-authorization-credential"
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "get_logs",
                        "arguments": '{"pod":"api"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "tool result: crashloop password=sentinel-raw-secret",
        },
    ]
    return (
        OutboundPolicy(max_request_chars=20_000)
        .prepare(
            "ollama",
            messages,
            [],
            iteration=2,
        )
        .snapshot
    )


def test_default_payload_export_dir_honors_xdg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert default_payload_export_dir() == tmp_path / "xdg" / "korvid" / "agent-payloads"


def test_default_payload_export_dir_uses_user_data_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert (
        default_payload_export_dir()
        == Path.home() / ".local" / "share" / "korvid" / "agent-payloads"
    )


def test_payload_inspector_bindings_offer_only_close_and_export() -> None:
    bindings = [
        (binding.key, binding.action, binding.show)
        for binding in PayloadInspectorScreen.BINDINGS
        if isinstance(binding, Binding)
    ]

    assert bindings == [
        ("escape", "dismiss", True),
        ("q", "dismiss", False),
        ("e", "export", True),
    ]


async def test_payload_inspector_renders_exact_sanitized_export_without_markup() -> None:
    snapshot = _snapshot()
    app = HostApp()
    screen = PayloadInspectorScreen(snapshot)

    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await until(
            pilot,
            lambda: isinstance(app.screen, PayloadInspectorScreen),
            label="payload inspector open",
        )
        body = screen.query_one("#payload-json", Static)
        rendered = str(body.content)

        assert body._render_markup is False
        assert rendered == snapshot.export_json()
        assert '"model": "ollama"' in rendered
        assert '"iteration": 2' in rendered
        assert "messages[1].content" in rendered
        assert "authorization-value" in rendered
        assert "system context: diagnose Kubernetes" in rendered
        assert "screen context: view=pods namespace=default" in rendered
        assert "tool result: crashloop" in rendered
        assert "sentinel-raw-secret" not in rendered
        assert "raw-authorization-credential" not in rendered


@pytest.mark.parametrize("key", ["escape", "q"])
async def test_payload_inspector_close_keys_dismiss(key: str) -> None:
    app = HostApp()
    dismissed: list[None] = []

    async with app.run_test() as pilot:
        await app.push_screen(
            PayloadInspectorScreen(_snapshot()),
            lambda result: dismissed.append(result),
        )
        await pilot.press(key)
        await until(pilot, lambda: dismissed == [None], label=f"{key} dismissal")
        assert dismissed == [None]


async def test_payload_inspector_does_not_persist_until_export(tmp_path: Path) -> None:
    app = HostApp()

    async with app.run_test() as pilot:
        await app.push_screen(PayloadInspectorScreen(_snapshot(), export_dir=tmp_path))
        await until(
            pilot,
            lambda: isinstance(app.screen, PayloadInspectorScreen),
            label="payload inspector open",
        )
        assert list(tmp_path.iterdir()) == []


async def test_payload_inspector_exports_privately_with_collision_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    export_dir = default_payload_export_dir()
    export_dir.mkdir(parents=True)
    existing = export_dir / "korvid-agent-payload-20260805-004820.json"
    existing.write_text("existing\n", encoding="utf-8")
    snapshot = _snapshot()
    app = HostApp()
    screen = PayloadInspectorScreen(snapshot)

    with mock.patch("korvid.ui.widgets.payload_inspector.datetime") as clock:
        clock.now.return_value = datetime(2026, 8, 5, 0, 48, 20, tzinfo=UTC)
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            with mock.patch.object(screen, "notify") as notify:
                await pilot.press("e")
                await until(pilot, lambda: notify.called, label="export notification")

    exported = export_dir / "korvid-agent-payload-20260805-004820-1.json"
    assert existing.read_text(encoding="utf-8") == "existing\n"
    assert exported.read_text(encoding="utf-8") == snapshot.export_json()
    assert str(exported) in str(notify.call_args.args[0])
