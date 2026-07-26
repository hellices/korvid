"""Secret viewer tests (issue #39): masked-by-default, per-key reveal,
audit-logged reveal/copy, binary handling, agent-context masking."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.secret_screen import SecretScreen

from .waits import until

_SECRETS_META = ResourceMeta("Secret", "secrets", "", "v1", True, ())
_ALIASES = {"secrets": _SECRETS_META, "secret": _SECRETS_META}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


_BINARY_PAYLOAD = bytes(range(256))

_SECRET_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": "db-creds",
        "namespace": "default",
        "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": (
                '{"data":{"password":"' + _b64("hunter2") + '"}}'
            ),
        },
    },
    "type": "Opaque",
    "data": {
        "password": _b64("hunter2"),
        "keystore.bin": base64.b64encode(_BINARY_PAYLOAD).decode(),
    },
    "stringData": {"note": "plain-note"},
}


def _secret_summary() -> Summary:
    return GenericSummary(name="db-creds", namespace="default", kind="Secret", created="")


def make_secret_app(
    *,
    audit: AuditLog | None = None,
    manifest: dict[str, Any] | None = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "secrets":
            yield ("ADDED", _secret_summary())
        while True:
            await asyncio.sleep(0.01)

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        copied: dict[str, Any] = json.loads(
            json.dumps(manifest if manifest is not None else _SECRET_MANIFEST)
        )
        return copied

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest,
        audit=audit,
    )


def _rendered_plain(widget: Any) -> str:
    from rich.console import Console

    console = Console(width=400, force_terminal=False, legacy_windows=False)
    with console.capture() as capture:
        console.print(widget.content)
    return capture.get()


def _screen_text(screen: SecretScreen) -> str:
    from textual.widgets import DataTable

    table = screen.query_one(DataTable)
    parts: list[str] = []
    for row_key in list(table.rows):
        for cell in table.get_row(row_key):
            parts.append(str(cell))
    return "\n".join(parts)


async def _open_secret_screen(pilot: Any, app: KorvidApp) -> SecretScreen:
    from korvid.ui.widgets.resource_table import ResourceTable

    await pilot.press("colon")
    for ch in "secrets":
        await pilot.press(ch)
    await pilot.press("enter")
    await until(pilot, lambda: app.current_kind == "secrets", label="secrets view")
    await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="secret row")
    await pilot.press("d")
    await until(pilot, lambda: isinstance(app.screen, SecretScreen), label="SecretScreen")
    screen = app.screen
    assert isinstance(screen, SecretScreen)
    return screen


def _audit_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Opening + masked-by-default
# ---------------------------------------------------------------------------


async def test_d_on_secret_opens_secret_screen_masked(tmp_path: Path) -> None:
    """`d` on a Secret opens the viewer with every value masked — no base64
    and no decoded plaintext anywhere on the screen."""
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        text = _screen_text(screen)
        assert "password" in text
        assert "keystore.bin" in text
        assert "note" in text
        assert MASK_PLACEHOLDER in text
        assert "hunter2" not in text
        assert _b64("hunter2") not in text
        assert "plain-note" not in text


async def test_reveal_decodes_and_audits(tmp_path: Path) -> None:
    """`x` on a row decodes the value inline and writes an audit record
    naming the secret and the key."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_secret_app(audit=AuditLog(audit_path))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        # Cursor starts on the first row: keystore.bin/password sorted → keystore.bin first
        # Move to the password row deterministically via key order.
        keys = screen.row_keys()
        password_index = keys.index(("password", "data"))
        for _ in range(password_index):
            await pilot.press("down")
        await pilot.press("x")
        await until(pilot, lambda: "hunter2" in _screen_text(screen), label="revealed value")
        await until(pilot, lambda: len(_audit_entries(audit_path)) == 2, label="audit entries")
        entries = _audit_entries(audit_path)
        # Same contract as cluster writes: an intent record persisted before
        # the disclosure, then the outcome after it happened.
        assert [e["outcome"] for e in entries] == ["intent", "success"]
        for entry in entries:
            assert entry["action"] == "secret-reveal"
            assert entry["kind"] == "secrets"
            assert entry["namespace"] == "default"
            assert entry["name"] == "db-creds"
            assert "password" in entry["detail"]
            # timestamp present (who/when/which key)
            assert entry["timestamp"]


async def test_reveal_toggles_back_to_masked(tmp_path: Path) -> None:
    """Pressing `x` again re-masks the value."""
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        await until(pilot, lambda: "hunter2" in _screen_text(screen), label="revealed")
        await pilot.press("x")
        await until(pilot, lambda: "hunter2" not in _screen_text(screen), label="re-masked")
        assert MASK_PLACEHOLDER in _screen_text(screen)


async def test_binary_value_reveals_as_digest(tmp_path: Path) -> None:
    """Binary payloads reveal as a size + sha256 summary, not garbage."""
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("keystore.bin", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        digest = hashlib.sha256(_BINARY_PAYLOAD).hexdigest()
        await until(pilot, lambda: digest in _screen_text(screen), label="binary digest")
        text = _screen_text(screen)
        assert f"{len(_BINARY_PAYLOAD)} bytes" in text


async def test_stringdata_value_revealed_without_decode(tmp_path: Path) -> None:
    """stringData entries are plaintext already; reveal shows them as-is."""
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("note", "stringData"))):
            await pilot.press("down")
        await pilot.press("x")
        await until(pilot, lambda: "plain-note" in _screen_text(screen), label="stringData")


# ---------------------------------------------------------------------------
# Fail-closed audit
# ---------------------------------------------------------------------------


async def test_reveal_blocked_without_audit_log() -> None:
    """No audit sink ⇒ reveal is blocked (fail-closed), value stays masked."""
    app = make_secret_app(audit=None)
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        await pilot.pause()
        assert "hunter2" not in _screen_text(screen)
        assert MASK_PLACEHOLDER in _screen_text(screen)


async def test_reveal_blocked_when_audit_append_fails(tmp_path: Path) -> None:
    """Audit write failure ⇒ the reveal is blocked, value stays masked."""

    class BrokenAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            raise OSError("disk full")

    app = make_secret_app(audit=BrokenAudit(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        await pilot.pause()
        assert "hunter2" not in _screen_text(screen)


# ---------------------------------------------------------------------------
# Copy to clipboard
# ---------------------------------------------------------------------------


async def test_copy_decoded_value_audited(tmp_path: Path) -> None:
    """`c` copies the decoded value to the clipboard and audit-logs it."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_secret_app(audit=AuditLog(audit_path))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("c")
        await until(pilot, lambda: app.clipboard == "hunter2", label="clipboard")
        entries = _audit_entries(audit_path)
        assert any(e["action"] == "secret-copy" and "password" in e["detail"] for e in entries)


async def test_copy_blocked_without_audit_log() -> None:
    """Copy is a secret disclosure too: no audit sink ⇒ blocked."""
    app = make_secret_app(audit=None)
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("c")
        await pilot.pause()
        assert app.clipboard != "hunter2"


# ---------------------------------------------------------------------------
# Dismissal
# ---------------------------------------------------------------------------


async def test_escape_closes_secret_screen(tmp_path: Path) -> None:
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        await _open_secret_screen(pilot, app)
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, SecretScreen), label="screen closed")
        assert not isinstance(app.screen, SecretScreen)


# ---------------------------------------------------------------------------
# Agent-context masking (design §7)
# ---------------------------------------------------------------------------


async def test_agent_describe_path_masks_secret_values(tmp_path: Path) -> None:
    """The agent-driven describe path renders Secrets masked: raw base64 and
    decoded values never reach the LLM-visible describe surface."""
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        manifest = json.loads(json.dumps(_SECRET_MANIFEST))
        await app._show_describe(False, "secrets/default/db-creds", manifest, [])
        from korvid.ui.widgets.describe_screen import DescribeScreen

        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe")
        body = _rendered_plain(app.screen.query_one("#describe-body"))
        assert "db-creds" in body
        assert MASK_PLACEHOLDER in body
        assert "hunter2" not in body
        assert _b64("hunter2") not in body
        assert "plain-note" not in body


async def test_screen_context_never_contains_secret_values(tmp_path: Path) -> None:
    """The screen context string sent to the LLM contains no secret values,
    even while the Secret viewer is open with values revealed."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_secret_app(audit=AuditLog(audit_path))

    captured: list[str] = []

    class FakeRuntime:
        async def run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[Any]:
            captured.append(screen_context)
            return
            yield  # pragma: no cover - makes this an async generator

    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        await until(pilot, lambda: "hunter2" in _screen_text(screen), label="revealed")
        app._agent_runtime = FakeRuntime()  # type: ignore[assignment]  # duck-typed fake
        await app._run_agent_turn("what do you see?")
        assert captured, "run_turn was not invoked"
        context = captured[0]
        assert "hunter2" not in context
        assert _b64("hunter2") not in context
        assert "plain-note" not in context


async def test_reveal_audit_records_actor(tmp_path: Path) -> None:
    """Reveal records answer *who* performed the disclosure (#39)."""
    import getpass

    audit_path = tmp_path / "audit.jsonl"
    app = make_secret_app(audit=AuditLog(audit_path))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        await until(pilot, lambda: "hunter2" in _screen_text(screen), label="revealed")
        entries = _audit_entries(audit_path)
        assert entries[0]["actor"] == getpass.getuser()


async def test_rapid_double_reveal_ends_masked(tmp_path: Path) -> None:
    """Two quick `x` presses toggle reveal→hide even while the first press's
    audit append is still pending: disclosure operations are serialized, so
    a double press can never leave the value exposed by accident."""
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        await pilot.press("x")
        await pilot.press("x")  # no wait: races the first press's audit write
        # Wait for both toggle workers to actually finish — the masked state
        # is true *before* they run, so it can't serve as the wait condition.
        await until(
            pilot,
            lambda: all(w.is_finished for w in screen.workers),
            label="both toggle workers finished",
        )
        text = _screen_text(screen)
        assert "hunter2" not in text
        assert MASK_PLACEHOLDER in text


async def test_copy_blocked_message_names_copy() -> None:
    """The fail-closed notification for `c` says the *copy* was blocked,
    not a reveal."""
    from unittest import mock

    app = make_secret_app(audit=None)
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("password", "data"))):
            await pilot.press("down")
        with mock.patch.object(screen, "notify") as notify:
            await pilot.press("c")
            await until(pilot, lambda: notify.called, label="blocked notification")
        message = str(notify.call_args[0][0])
        assert "copy" in message.lower()
        assert "reveal" not in message.lower()


async def test_copy_invalid_base64_message_does_not_claim_digest(tmp_path: Path) -> None:
    """Invalid base64 has no digest; the copy notification must not claim
    a digest summary was copied."""
    from unittest import mock

    manifest = json.loads(json.dumps(_SECRET_MANIFEST))
    manifest["data"]["broken"] = "!!! not base64 !!!"
    app = make_secret_app(audit=AuditLog(tmp_path / "audit.jsonl"), manifest=manifest)
    async with app.run_test() as pilot:
        screen = await _open_secret_screen(pilot, app)
        keys = screen.row_keys()
        for _ in range(keys.index(("broken", "data"))):
            await pilot.press("down")
        with mock.patch.object(screen, "notify") as notify:
            await pilot.press("c")
            await until(pilot, lambda: notify.call_count >= 2, label="copy notifications")
        messages = [str(call.args[0]) for call in notify.call_args_list]
        warning = messages[0]
        assert "digest" not in warning.lower()
        assert "summary" in warning.lower()
