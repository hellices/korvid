# Copy to the Clipboard (#519) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One key, `y`, copies the open describe view or pane, or else the selected resource's name (`namespace/name` when it has one), through Textual's OSC 52 `copy_to_clipboard` (#519).

**Architecture:** `ResourceInspectController`, which already owns the read-only "look at this" keys, gains `copy()`. It reads the open describe pane's displayed text and the selected row through `InspectSurface` (now with `describe_text()` and `copy_to_clipboard()`) and `ViewState`. The app binds `y` (id `copy`); the modal `DescribeScreen`, where app bindings do not fire, binds the same id to its own `action_copy`, so one `:keys` remap moves both.

**Tech Stack:** Python 3.11+, Textual 8, pytest with Textual pilots, mypy `--strict`, ruff, tach.

## Global Constraints

- **Masked stays masked:** the copy is the text the view displays (`DescribePane.body_text`, `describe_body_text` of the manifest the screen was given), never the manifest behind it. A Secret reaches the describe views only masked (`mask_secret_manifest` on the agent path; `d` sends Secrets to the Secret viewer), and the Secret viewer's audited `c` stays the only way to copy a Secret value.
- **The notice never repeats the content:** it says what kind of thing was copied.
- **Remappable:** the app binding has id `copy`, and so does the `DescribeScreen` binding, because Textual applies a keymap by id to every node.
- **Size limits:** `src/korvid/ui/app.py` is at 1496 of 1500 lines and `scripts/check_source_size.py` is not edited, so `action_describe`'s docstring is shortened to one line to make room for `action_copy` beside it.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the issue

- **The key is `y`.** It was free at the app level and on every main-screen widget, and `y` (yank) is the usual copy key in terminal tools.
- **The describe pane comes first.** With the agent's non-modal describe pane open, `y` copies it, as `/`, `n` and `N` act on that pane first.
- **The app binding is hidden from the footer** (`show=False`), as other secondary keys are; help, the palette and `docs/keybindings.md` list it. The modal `DescribeScreen` shows it in its own footer.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/ui/resource_inspect_controller.py` | modify | `InspectSurface.describe_text`/`copy_to_clipboard`, `copy()`, the `copy` probe |
| `src/korvid/ui/read_availability.py` | modify | `copy_reason` |
| `src/korvid/ui/app_surfaces.py` | modify | `AppInspectSurface` implementations |
| `src/korvid/ui/app_bindings.py`, `action_policy.py`, `app.py` | modify | the `y` binding, its help group, its owner, `action_copy` |
| `src/korvid/ui/widgets/describe_screen.py` | modify | the modal's `y` (id `copy`) and `action_copy` |
| `tests/ui/test_resource_inspect_controller.py`, `tests/ui/test_clipboard_copy.py`, `tests/ui/test_action_policy.py` | modify / create | unit, end-to-end and vocabulary tests |
| `docs/keybindings.md`, `docs/release-notes/unreleased.md` | modify | the key, masking and the OSC 52 caveat |

---

### Task 1: The controller

- [ ] **Step 1: Write the failing tests.** Give `FakeSurface` a settable `describe_body: str | None` and a `copied: list[str]`, implementing the two new surface methods, then append:

```python
# ---------------------------------------------------------------------------
# copy (#519)
# ---------------------------------------------------------------------------


def test_copy_takes_the_open_describe_pane_whole() -> None:
    h = Harness()
    h.surface.describe_body = "apiVersion: v1\nkind: Pod\n\nEVENTS\n"

    h.controller.copy()

    assert h.surface.copied == ["apiVersion: v1\nkind: Pod\n\nEVENTS\n"]
    assert h.ui.notifications == [("Copied the describe pane to the clipboard", "information")]


@pytest.mark.parametrize(
    ("selected", "copied", "what"),
    [
        (("default", "api-1"), "default/api-1", "namespace/name"),
        (("", "node-1"), "node-1", "name"),
    ],
    ids=["namespaced", "cluster-scoped"],
)
def test_copy_takes_the_selected_rows_name_without_a_pane_open(
    selected: tuple[str, str], copied: str, what: str
) -> None:
    h = Harness(selected=selected)

    h.controller.copy()

    assert h.surface.copied == [copied]
    assert h.ui.notifications == [
        (f"Copied the selected resource's {what} to the clipboard", "information")
    ]


def test_copy_never_repeats_what_it_copied() -> None:
    h = Harness(selected=("payments", "ledger-7c9f"))

    h.controller.copy()

    assert not any("ledger" in message for message, _ in h.ui.notifications)


def test_copy_with_nothing_to_copy_copies_nothing() -> None:
    h = Harness(selected=(None, None))

    h.controller.copy()

    assert h.surface.copied == []


def test_copy_probe_needs_an_open_pane_or_a_selected_row() -> None:
    h = Harness(selected=(None, None))
    refused = h.controller.unavailable_reason("copy")

    h.surface.describe_body = "kind: Pod\n"
    with_pane = h.controller.unavailable_reason("copy")
    h.surface.describe_body = None
    h.view.selected = ("default", "api-1")
    with_row = h.controller.unavailable_reason("copy")

    assert refused is not None
    assert refused.code is AvailabilityCode.NO_SELECTION
    assert (with_pane, with_row) == (None, None)
    assert h.surface.copied == []
```

- [ ] **Step 2:** Run `uv run --frozen pytest -p no:tach tests/ui/test_resource_inspect_controller.py -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.** Add to `InspectSurface`:

```python
    @abstractmethod
    def describe_text(self) -> str | None:
        """The open describe pane's plain text, exactly as shown; None while closed.

        What `y` copies, so it is the masked text the pane displays, never
        the manifest behind it. Tolerates the widget being gone.
        """

    @abstractmethod
    def copy_to_clipboard(self, text: str) -> None:
        """Hand *text* to the terminal clipboard (an OSC 52 sequence)."""
```

Add `copy_reason` to `read_availability.py`:

```python
def copy_reason(*, pane: bool, selected: bool) -> UnavailableReason | None:
    """Why `y` has nothing to copy, or None.

    `ResourceInspectController.copy` takes the open describe pane first,
    then the selected row's name; with neither there is nothing to copy.
    """
    return None if pane or selected else NO_SELECTION
```

Answer `unavailable_reason("copy")` with `copy_reason(pane=self._surface.describe_text() is not None, selected=self._view.selected_ns_name(notify=False)[1] is not None)`, and add:

```python
    def copy(self) -> None:
        """`y`: copy the open describe pane, or else the selected row's name.

        A namespaced row is copied as `namespace/name`. The notice says what
        was copied, never the text itself.
        """
        text = self._surface.describe_text()
        if text is not None:
            self._surface.copy_to_clipboard(text)
            self._ui.notify("Copied the describe pane to the clipboard")
            return
        namespace, name = self._view.selected_ns_name()
        if name is None:
            return
        self._surface.copy_to_clipboard(f"{namespace}/{name}" if namespace else name)
        what = "namespace/name" if namespace else "name"
        self._ui.notify(f"Copied the selected resource's {what} to the clipboard")
```

Implement the surface in `AppInspectSurface`:

```python
    def describe_text(self) -> str | None:
        try:
            pane = self._app._describe_pane
        except NoMatches:
            return None
        return pane.body_text if pane.display else None

    def copy_to_clipboard(self, text: str) -> None:
        self._app.copy_to_clipboard(text)
```

- [ ] **Step 4:** Run the tests again. Expected: all pass. Then commit.

### Task 2: The key, end to end

- [ ] **Step 1: Write the failing tests:** create `tests/ui/test_clipboard_copy.py`:

```python
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
```

- [ ] **Step 2: Implement.** In `app_bindings.py`, add `Binding("y", "copy", "Copy name or describe text", show=False, id="copy")` after `d`, and `"copy": ("Table", "Describe")` to `ACTION_HELP_GROUPS`; add `"copy"` to `_INSPECT_REASON_ACTIONS` in `action_policy.py` (and to the expected set in `test_composed_action_reasons_ask_each_owner_about_its_own_actions`). In `app.py`, shorten `action_describe`'s docstring to one line and add `action_copy`, which calls `self._inspect.copy()`. In `DescribeScreen`, add `Binding("y", "copy", "Copy", show=True, id="copy")` and:

```python
    def action_copy(self) -> None:
        """Copy the view's text, exactly as shown, without repeating it."""
        self.app.copy_to_clipboard(describe_body_text(self._manifest, self._events))
        self.notify("Copied the describe view to the clipboard")
```

- [ ] **Step 3:** Run `uv run --frozen pytest -p no:tach tests/ui -q`. Expected: all pass. Then commit.

### Task 3: Document it, then run the full gate

- [ ] `docs/keybindings.md`: a `y` row under "Move and inspect", and a paragraph on masking and the OSC 52 caveat. Add a release-notes entry.
- [ ] Run `make check`. Expected: every step passes.
