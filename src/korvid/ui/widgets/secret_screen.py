"""Secret viewer modal — masked-by-default with audit-logged per-key reveal.

Implements spec §5 #9: a Secret's `data`/`stringData` entries render masked;
`x` reveals one key (base64-decoded inline), `c` copies the decoded value to
the clipboard. Both disclosures are audit-logged **fail-closed**: if the
audit entry cannot be written, the disclosure is blocked (AGENTS.md security
invariant — reading a secret value is as sensitive as a write).
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.audit import AuditLog
from korvid.core.secrets import MASK_PLACEHOLDER, reveal_value, secret_keys


class SecretScreen(ModalScreen[None]):
    """Full-screen viewer for one Secret with per-key reveal.

    The manifest is kept only inside this widget; everything rendered
    starts masked, so no secret material reaches the screen (or anything
    scraping it) until the user explicitly reveals a key.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape,q", "close_screen", "Close"),
        Binding("x", "toggle_reveal", "Reveal/hide"),
        Binding("c", "copy_value", "Copy decoded"),
    ]

    DEFAULT_CSS = """
    SecretScreen {
        align: center middle;
    }
    SecretScreen > Vertical {
        width: 90%;
        height: 90%;
        border: round $accent;
        padding: 1;
    }
    SecretScreen #secret-title {
        text-style: bold;
        margin-bottom: 1;
    }
    SecretScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        title: str,
        manifest: dict[str, Any],
        *,
        audit: AuditLog | None,
        kind: str = "secrets",
    ) -> None:
        super().__init__()
        self._title_text = title
        self._manifest = manifest
        self._audit = audit
        self._kind = kind
        meta = manifest.get("metadata")
        meta_dict: dict[str, Any] = meta if isinstance(meta, dict) else {}
        self._namespace: str | None = meta_dict.get("namespace")
        self._name: str = str(meta_dict.get("name") or "")
        self._keys: list[tuple[str, str]] = secret_keys(manifest)
        self._revealed: set[tuple[str, str]] = set()
        # Serializes state-check → audit append → cell update: without it two
        # rapid presses can both observe "hidden" while the first press's
        # audit write is pending and leave the value exposed instead of
        # toggling it back to masked.
        self._disclosure_lock = asyncio.Lock()

    def row_keys(self) -> list[tuple[str, str]]:
        """The `(key, section)` pairs in display order (top to bottom)."""
        return list(self._keys)

    def compose(self) -> ComposeResult:
        secret_type = str(self._manifest.get("type") or "Opaque")
        with Vertical():
            yield Static(
                f"{self._title_text}  [dim]type={secret_type} — "
                f"x reveal · c copy · Esc close[/dim]",
                id="secret-title",
            )
            yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("KEY", "SECTION", "VALUE")
        for key, section in self._keys:
            table.add_row(key, section, MASK_PLACEHOLDER, key=f"{section}/{key}")
        table.focus()

    # ------------------------------------------------------------------
    # Reveal / copy — both audit-logged fail-closed
    # ------------------------------------------------------------------

    def _cursor_entry(self) -> tuple[str, str] | None:
        table = self.query_one(DataTable)
        if not self._keys or table.cursor_row < 0 or table.cursor_row >= len(self._keys):
            return None
        return self._keys[table.cursor_row]

    def _raw_value(self, key: str, section: str) -> str | None:
        entries = self._manifest.get(section)
        if isinstance(entries, dict) and key in entries:
            return str(entries[key])
        return None

    async def _audit_disclosure(self, action: str, key: str, section: str) -> bool:
        """Record the disclosure before it happens; False blocks it.

        Fail-closed: a missing audit sink or a failed append means the
        secret value must not be shown or copied.
        """
        operation = action.removeprefix("secret-")  # "reveal" / "copy"
        audit = self._audit
        if audit is None:
            self.notify(
                f"Secret {operation} blocked: no audit log configured",
                severity="warning",
            )
            return False
        try:
            await asyncio.to_thread(
                audit.append,
                action=action,
                kind=self._kind,
                namespace=self._namespace,
                name=self._name,
                detail=f"key={key} section={section}",
                outcome="success",
            )
        except Exception:
            self.log.error("audit append failed; blocking secret disclosure")
            self.notify(
                f"Secret {operation} blocked: audit log unavailable",
                severity="error",
            )
            return False
        return True

    def _set_value_cell(self, key: str, section: str, value: str) -> None:
        table = self.query_one(DataTable)
        table.update_cell(f"{section}/{key}", table.ordered_columns[2].key, value)

    @work
    async def action_toggle_reveal(self) -> None:
        entry = self._cursor_entry()
        if entry is None:
            return
        key, section = entry
        async with self._disclosure_lock:
            if entry in self._revealed:
                self._revealed.discard(entry)
                self._set_value_cell(key, section, MASK_PLACEHOLDER)
                return
            raw = self._raw_value(key, section)
            if raw is None:
                return
            if not await self._audit_disclosure("secret-reveal", key, section):
                return
            revealed = reveal_value(raw, encoded=section == "data")
            self._revealed.add(entry)
            self._set_value_cell(key, section, revealed.text)

    @work
    async def action_copy_value(self) -> None:
        entry = self._cursor_entry()
        if entry is None:
            return
        key, section = entry
        async with self._disclosure_lock:
            raw = self._raw_value(key, section)
            if raw is None:
                return
            if not await self._audit_disclosure("secret-copy", key, section):
                return
            revealed = reveal_value(raw, encoded=section == "data")
            if revealed.binary:
                # There is no meaningful text form of a binary payload; copying
                # the digest summary avoids pasting garbage into a terminal.
                self.notify(f"{key} is binary — copied its digest summary", severity="warning")
            self.app.copy_to_clipboard(revealed.text)
            self.notify(f"Copied decoded value of {key!r}")

    def action_close_screen(self) -> None:
        self.app.pop_screen()
