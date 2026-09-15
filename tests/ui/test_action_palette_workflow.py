"""Action Palette wiring: guarded opening and exact existing-route dispatch.

Issue #388 task 6. The palette modal (task 5) and its catalog derivation
(task 2) are already reviewed; what is under test here is the *app* half:
`Ctrl-P` replaces Textual's implicit system palette, the open is refused on
every protected or transient surface, the entry list is rebuilt after the
modal is dismissed, and a selection is routed through the app's existing
action/command routes exactly once.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Input, OptionList

from korvid.core.config import KorvidConfig, ViewConfig
from korvid.core.portforward import ForwardRegistry
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.columns import CustomColumn
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.tools.proposals import ProposalStore
from korvid.ui import action_palette as palette_domain
from korvid.ui.action_availability import (
    AGENT_UNAVAILABLE,
    ActionAvailability,
    AvailabilityCode,
    UnavailableReason,
)
from korvid.ui.action_palette import AppActionInvocation, PaletteEntry
from korvid.ui.app import KorvidApp
from korvid.ui.context_switch_coordinator import ContextSwitchResult
from korvid.ui.widgets import action_palette as palette_modal
from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.pulse import PulseScreen
from korvid.ui.widgets.resource_table import ResourceTable
from tests.app_factory import build_test_app

from .agent_session_fakes import FakeSession
from .test_adaptive_footer import _rows_listed
from .test_adaptive_footer import make_app as make_navigation_app
from .test_app import _pod, make_app
from .test_integration_controller import FakeMCP
from .test_proposals_ui import Recorder as ProposalRecorder
from .test_proposals_ui import _submit as _submit_proposal
from .test_proposals_ui import make_app as make_proposals_app
from .test_telepresence import FakeTelepresence
from .test_write_ops import Recorder, _to_view
from .test_write_ops import make_app as make_write_app
from .waits import until


def _build_app(rows: Sequence[Summary] | None = None, **kwargs: Any) -> KorvidApp:
    """A minimal app whose optional capabilities the caller chooses.

    `rows` is what the watch feeds the pods view; the default single pod
    keeps every existing caller unchanged, and an explicit `[]` is how a
    test asks what the palette says about an empty table.
    """
    store = ResourceStore()
    listed = [_pod("web")] if rows is None else list(rows)

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for row in listed:
            yield ("ADDED", row)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        **kwargs,
    )


def _app_with_integrations() -> KorvidApp:
    return _build_app(mcp=FakeMCP(), telepresence=FakeTelepresence())


def _app_without_agent() -> KorvidApp:
    return _build_app(agent_available=False)


async def _loaded(pilot: Any, app: KorvidApp) -> ResourceTable:
    table = app.query_one(ResourceTable)
    await until(pilot, lambda: table.row_count == 1, label="pod loaded")
    return table


def _focused(app: KorvidApp) -> Widget | None:
    """`app.focused`, read through a call so narrowing cannot leak.

    A test that asserts focus twice in one flow (`... is second`, later
    `... is prompt`) otherwise trips mypy's `comparison-overlap`: the
    first identity assert narrows the attribute expression to that widget
    type for the rest of the function, and the second comparison then
    looks impossible even though focus really did move between them.
    """
    return app.focused


async def _open_palette(pilot: Any) -> ActionPaletteScreen:
    await pilot.press("ctrl+p")
    await until(
        pilot,
        lambda: isinstance(pilot.app.screen, ActionPaletteScreen),
        label="palette open",
    )
    screen = pilot.app.screen
    assert isinstance(screen, ActionPaletteScreen)
    return screen


async def _type_query(pilot: Any, query: str) -> None:
    for character in query:
        await pilot.press("space" if character == " " else character)


async def _select_palette_entry(pilot: Any, query: str, expected_id: str) -> None:
    screen = await _open_palette(pilot)
    await _type_query(pilot, query)
    options = screen.query_one(OptionList)
    await until(
        pilot,
        lambda: options.option_count > 0 and options.get_option_at_index(0).id == expected_id,
        label=f"{expected_id} ranked first",
    )
    await pilot.press("enter")


def _entry(entry_id: str, availability: ActionAvailability) -> PaletteEntry:
    return PaletteEntry(
        id=entry_id,
        title="Help",
        description="Help",
        category="Global",
        trigger="?",
        search_terms=(),
        declaration_order=0,
        availability=availability,
        invocation=AppActionInvocation("help"),
    )


# ---------------------------------------------------------------------------
# Opening: Ctrl-P is korvid's own binding, not Textual's system palette
# ---------------------------------------------------------------------------


def test_textual_system_command_palette_is_disabled() -> None:
    """Textual binds `Ctrl-P` to its own command palette implicitly; korvid
    owns that key, so the stock palette must be switched off rather than
    shadowed by a competing binding."""
    assert KorvidApp.ENABLE_COMMAND_PALETTE is False


async def test_ctrl_p_opens_palette_and_escape_restores_table_focus() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        table = await _loaded(pilot, app)
        table.focus()
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.screen is app.screen_stack[0],
            label="palette closed",
        )
        assert app.focused is table


async def test_palette_opens_from_the_log_split_and_agent_surfaces() -> None:
    """The palette is the keyboard-first entry point for every ordinary
    workspace surface, not only the bare table (issue #388).

    Each leg asserts the surface is genuinely in the state it claims before
    pressing `Ctrl-P`: an open log pane, a real second pane owning focus,
    and the Agent prompt `Input` holding the keyboard. Otherwise a leg could
    "pass" against a workspace that never changed.
    """
    app = _build_app(agent_session=FakeSession(), agent_model_name="test-model")
    async with app.run_test() as pilot:
        await _loaded(pilot, app)

        await app._logs.open_pane("default", [("web", "app")])
        log_pane = app.query_one(LogPane)
        await until(pilot, lambda: log_pane.display, label="log pane open")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")

        await pilot.press("ctrl+w", "v")  # split workspace
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split panes exist")
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: app.focused is second, label="new pane focused")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert app.focused is second

        await pilot.press("ctrl+a")  # agent panel
        await until(pilot, lambda: app._agent_panel.display, label="agent panel open")
        # A session is wired, so opening the panel focuses its prompt: this
        # leg really exercises "an Input that is not the command or filter
        # bar owns the keyboard when Ctrl-P is pressed".
        prompt = app._agent_panel.query_one("#agent-input", Input)
        await until(pilot, lambda: app.focused is prompt, label="agent prompt focused")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert _focused(app) is prompt


async def test_palette_opens_over_the_inline_describe_pane() -> None:
    """The agent-shared describe *pane* is an ordinary workspace surface -
    it is mounted on the base screen, not pushed - so the palette opens over
    it exactly as it does over the table (task 6 review)."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        pane = app._describe_pane
        pane.show("pods/default/web", {"kind": "Pod", "metadata": {"name": "web"}}, [])
        await until(pilot, lambda: pane.display, label="describe pane shown")
        assert len(app.screen_stack) == 1
        assert app._actions.binding_enabled("open_action_palette") is True
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert pane.display


async def test_palette_refuses_to_open_over_the_describe_modal() -> None:
    """The describe *screen* is a modal (what `d` opens): stacking the
    palette over it is exactly what the surface guard forbids."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app.push_screen(DescribeScreen("pods/default/web", {"kind": "Pod"}, []))
        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe modal")
        screen = app.screen
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is screen
        assert len(app.screen_stack) == 2
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_ctrl_p_over_an_approval_dialog_changes_nothing() -> None:
    """The security case, pressed rather than reasoned about: `Ctrl-P` is a
    priority binding, so it reaches the app even while an approval dialog is
    up. Nothing may move - not the screen, not the focus, and no palette."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app.push_screen(ConfirmScreen("Delete pod default/web?", "delete pods/default/web"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval open")
        screen = app.screen
        focused = app.focused
        stack = list(app.screen_stack)
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is screen
        assert app.focused is focused
        assert list(app.screen_stack) == stack
        assert not any(isinstance(s, ActionPaletteScreen) for s in app.screen_stack)
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_ctrl_p_cannot_cover_an_approval_the_real_write_path_opened(
    tmp_path: Path,
) -> None:
    """The same guard against a dialog korvid itself raised.

    The test above pushes a `ConfirmScreen` directly, which proves the
    policy but not the wiring around a live write. Here `Ctrl-D` runs the
    production delete flow: dry-run, preview, approval. `Ctrl-P` while that
    dialog waits must leave the screen, the focus and the pending write
    exactly as they were - the keystroke that could dismiss or re-target an
    approval is the one that must never reach it (security invariant).
    """
    audit_path = tmp_path / "audit.jsonl"
    recorder = Recorder()
    app = make_write_app(recorder, audit_path)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count > 0, label="pods loaded")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation open",
        )
        screen = app.screen
        focused = app.focused
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is screen
        assert app.focused is focused
        assert not any(isinstance(item, ActionPaletteScreen) for item in app.screen_stack)
        assert app._actions.binding_enabled("open_action_palette") is False
        # The dialog is still the only thing that can complete this write.
        assert recorder.calls == []
        assert not audit_path.exists()
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.screen is app.screen_stack[0],
            label="approval dismissed",
        )
        assert recorder.calls == []
        assert not audit_path.exists()


# ---------------------------------------------------------------------------
# Command rows answer to their own owners' capabilities
# ---------------------------------------------------------------------------


def _row(app: KorvidApp, entry_id: str) -> PaletteEntry:
    entries = {entry.id: entry for entry in app._palette_entries()}
    return entries[entry_id]


async def test_command_rows_are_unavailable_without_their_capability() -> None:
    """No MCP controller and no telepresence CLI wired: pressing `:mcp` or
    `:tp` only earns a refusal, so the palette must not advertise them."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._mcp is None
        assert app._integrations.telepresence_available is False
        for entry_id in ("command:mcp", "command:tp"):
            row = _row(app, entry_id)
            assert row.availability.invocable is False
            reason = row.availability.reason
            assert reason is not None
            assert reason.code is AvailabilityCode.MISSING_CAPABILITY
        # The agent *is* available here, so its rows stay runnable.
        assert _row(app, "command:ai").availability.invocable is True
        assert _row(app, "command:model").availability.invocable is True
        # And the row itself says it cannot run, rather than leaving the
        # user to infer it: the rendered second line carries the owner's
        # reason, and Enter on the row runs nothing.
        screen = await _open_palette(pilot)
        await _type_query(pilot, "mcp")
        options = screen.query_one(OptionList)
        await until(
            pilot,
            lambda: options.option_count > 0 and options.get_option_at_index(0).id == "command:mcp",
            label="mcp ranked first",
        )
        prompt = str(options.get_option_at_index(0).prompt).splitlines()
        assert prompt[1].startswith("Unavailable: ")
        await pilot.press("enter")
        assert app.screen is screen


async def test_command_rows_are_enabled_once_their_capability_is_wired() -> None:
    app = _app_with_integrations()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app._mcp is not None, label="app composed")
        await pilot.pause()
        assert _row(app, "command:mcp").availability.invocable is True
        assert _row(app, "command:tp").availability.invocable is True


async def test_the_picker_command_rows_report_their_missing_collaborator() -> None:
    """`:ns` opens the namespace picker and `:ctx` the context picker; each
    handler refuses outright when the collaborator it lists through was
    never wired. A base `make_app` session has neither, so both rows carry
    their handler's own sentence, silently (#388 round 8)."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        app._list_namespaces = None
        before = len(app._notifications)
        namespace_row = _row(app, "command:ns")
        assert namespace_row.availability.invocable is False
        assert namespace_row.availability.reason is not None
        assert namespace_row.availability.reason.message == "Namespace listing unavailable"
        context_row = _row(app, "command:ctx")
        assert context_row.availability.invocable is False
        assert context_row.availability.reason is not None
        assert (
            context_row.availability.reason.message == "Context switching unavailable in this build"
        )
        assert len(app._notifications) == before


async def test_the_namespace_row_follows_a_listing_wired_after_the_palette_ran() -> None:
    """The answer is re-read for every catalog derivation, not frozen at
    wiring time: the same session flips as its listing comes and goes."""
    app = _build_app()  # wires a namespace listing
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert _row(app, "command:ns").availability.invocable is True
        app._list_namespaces = None
        assert _row(app, "command:ns").availability.invocable is False
        assert _row(app, "command:ns").availability.reason is not None


async def test_the_context_row_is_enabled_once_the_kubeconfig_seams_are_wired() -> None:
    """With the listing, the probe and the swap all composed, `:ctx` really
    opens its picker - so the row is offered."""

    def list_contexts() -> tuple[list[str], str | None]:
        return ["ctx-a", "ctx-b"], "ctx-a"

    async def probe(name: str) -> None:
        return None

    async def switch(name: str | None) -> ContextSwitchResult:
        return ContextSwitchResult(
            pod_resize_supported=False, provider_hint=None, context_namespace=None
        )

    app = _build_app(list_contexts=list_contexts, probe_context=probe, switch_context=switch)
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        before = len(app._notifications)
        assert _row(app, "command:ctx").availability == ActionAvailability.enabled()
        assert _row(app, "command:ns").availability == ActionAvailability.enabled()
        assert len(app._notifications) == before


async def test_the_namespace_picker_key_still_notifies_what_the_row_reports() -> None:
    """The probe is silent; the typed command keeps its own warning and its
    detailed behaviour (the picker never opens)."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        app._list_namespaces = None
        await app._workspace_ctl.show_namespace_picker()
        await until(
            pilot,
            lambda: any(n.message == "Namespace listing unavailable" for n in app._notifications),
            label="the namespace picker command notified",
        )
        assert len(app.screen_stack) == 1


async def test_the_forward_list_row_reports_a_build_without_a_registry() -> None:
    """`:pf` lists the forwards a session already started, so its one
    composition question is whether this build carries a registry at all.

    A session wired without one refuses `:pf` outright, and the palette has
    to say so with the owner's own wording rather than advertise a command
    that only earns a warning - silently, because deriving the catalog is a
    probe (#388 round 10).
    """
    app = _build_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._forwards is None
        before = len(app._notifications)
        row = _row(app, "command:pf")
        assert row.availability.invocable is False
        reason = row.availability.reason
        assert reason is not None
        assert reason.code is AvailabilityCode.MISSING_CAPABILITY
        assert reason.message == "Port-forward unavailable in this build"
        assert len(app._notifications) == before


async def test_the_forward_list_row_is_offered_once_a_registry_is_wired() -> None:
    """With a registry composed, `:pf` really opens its screen - so the row
    is offered, and it stays offered with nothing selected and no `kubectl`
    reachable, because `open_list` reads neither."""
    app = _build_app(forwards=ForwardRegistry())
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        before = len(app._notifications)
        assert _row(app, "command:pf").availability == ActionAvailability.enabled()
        assert len(app._notifications) == before


async def test_the_forward_list_row_is_revalidated_after_the_palette_closes() -> None:
    """The answer belongs to the moment it is asked, not to the render.

    A capability can appear or vanish while the modal is open, so the row
    the user selected is re-derived and re-judged afterwards: a `:pf`
    selection made against a registry that is gone by the time the palette
    dismisses must notify the owner's refusal and route nothing.
    """
    app = _build_app(forwards=ForwardRegistry())
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert _row(app, "command:pf").availability == ActionAvailability.enabled()
        app._forwards = None
        assert _row(app, "command:pf").availability.invocable is False
        before = len(app._notifications)
        await app._palette_selected("command:pf")
        await until(
            pilot,
            lambda: any(
                n.message == "Port-forward unavailable in this build" for n in app._notifications
            ),
            label="the stale forward-list selection refused with the owner's wording",
        )
        assert len(app._notifications) == before + 1
        assert len(app.screen_stack) == 1


async def test_agent_command_rows_are_unavailable_without_the_agent() -> None:
    """`:ai`/`:model` availability is composed at the wiring (the agent
    controller is at its reviewed size cap, so the answer is assembled from
    its existing `available` flag and the shared reason rather than added as
    a method on it - task 6 re-review)."""
    app = _app_without_agent()
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app._notifications)
        for entry_id in ("command:ai", "command:model"):
            row = _row(app, entry_id)
            assert row.availability.invocable is False
            assert row.availability.reason == AGENT_UNAVAILABLE
        # Deriving the catalog is a silent probe: it asks, it never tells.
        assert len(app._notifications) == before


async def test_agent_command_rows_are_enabled_with_the_agent() -> None:
    app = _build_app(agent_session=FakeSession(), agent_model_name="test-model")
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._agent_ui.available is True
        for entry_id in ("command:ai", "command:model"):
            assert _row(app, entry_id).availability == ActionAvailability.enabled()


async def test_the_agent_row_explains_the_capability_not_the_router_fallback() -> None:
    """The two wordings differ on purpose, so nothing should assert parity.

    Without the [agent] extra no owner claims `:ai`, so `CommandRouter` falls
    through to its generic "nothing routed this" report - a *routing* outcome
    phrased for a mistyped command. The palette is answering a different
    question ("why is this row greyed out?"), and says what is actually
    missing. Each stays the right sentence for its own moment.
    """
    app = _app_without_agent()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _row(app, "command:ai").availability.reason == AGENT_UNAVAILABLE
        await pilot.press("colon")
        for character in "ai":
            await pilot.press(character)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("Unknown resource or command: ai" in n.message for n in app._notifications),
            label="router fallback reported",
        )
        assert not any(n.message == AGENT_UNAVAILABLE.message for n in app._notifications)


@pytest.mark.parametrize(
    ("command", "entry_id"), [("ai", "command:ai"), ("model", "command:model")]
)
async def test_the_palette_refuses_every_command_the_real_router_cannot_run(
    command: str, entry_id: str
) -> None:
    """Different sentences, one verdict: neither agent command can run.

    The test above pins that the two *wordings* are allowed to differ. What
    must never differ is the answer underneath them: if typing `:ai` or
    `:model` only earns a refusal from the real `CommandRouter` - because
    no agent owner claimed the command in this composition - then the
    palette must not offer that row as runnable. An enabled row would
    promise a route the app does not have.
    """
    app = _app_without_agent()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("colon")
        for character in command:
            await pilot.press(character)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any(
                f"Unknown resource or command: {command}" in n.message for n in app._notifications
            ),
            label=f":{command} refused by the router",
        )
        row = _row(app, entry_id)
        assert row.availability.invocable is False
        assert row.availability.reason == AGENT_UNAVAILABLE


# ---------------------------------------------------------------------------
# `:proposals` answers to its own inbox
# ---------------------------------------------------------------------------


def _proposals_app(store: ProposalStore | None, tmp_path: Path) -> KorvidApp:
    """A real app whose external-proposal inbox is `store` (None = off)."""
    return make_proposals_app(ProposalRecorder(), tmp_path / "palette-audit.jsonl", store)


async def test_the_proposals_row_is_unavailable_without_the_feature(tmp_path: Path) -> None:
    """`mcp.write_proposals` off is the first thing `open_review` refuses,
    so the row carries that same sentence - and deriving the catalog stays
    a silent probe (issue #388, round 6)."""
    app = _proposals_app(None, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app._notifications)
        row = _row(app, "command:proposals")
        assert row.availability.invocable is False
        assert row.availability.reason == UnavailableReason(
            AvailabilityCode.MISSING_CAPABILITY,
            "External write proposals are disabled (set mcp.write_proposals: true)",
        )
        assert len(app._notifications) == before


async def test_the_proposals_row_reports_an_empty_inbox(tmp_path: Path) -> None:
    """Enabled but empty is the second refusal, and it is an information
    toast rather than a warning - the row keeps that severity."""
    app = _proposals_app(ProposalStore(), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app._notifications)
        row = _row(app, "command:proposals")
        assert row.availability.invocable is False
        assert row.availability.reason == UnavailableReason(
            AvailabilityCode.NO_SELECTION, "No pending write proposals", severity="information"
        )
        assert len(app._notifications) == before


async def test_the_proposals_row_is_enabled_with_a_proposal_waiting(tmp_path: Path) -> None:
    """A pending proposal and no open review: `:proposals` would open the
    dialog, so the palette offers it."""
    store = ProposalStore()
    app = _proposals_app(store, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "is pending user review" in await _submit_proposal(app)
        assert store.pending() != []
        assert _row(app, "command:proposals").availability == ActionAvailability.enabled()


async def test_deriving_the_proposals_row_never_expires_the_inbox(tmp_path: Path) -> None:
    """Building the catalog is a probe, not a read (issue #388, round 7).

    A stale inbox answers the row the same way an empty one does, but the
    derivation must not be what *makes* it stale: the sweep that retires a
    proposal notifies the store's subscribers and hands the app an expiry to
    audit, and opening (or re-deriving) the palette must spend no I/O on an
    inbox it is only describing. The proposal is still pending afterwards,
    so `:proposals`, `:ctx` and shutdown settle and audit it as before.
    """
    clock = [0.0]
    store = ProposalStore(ttl=10.0, clock=lambda: clock[0])
    app = _proposals_app(store, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit_proposal(app)
        pending = store.pending()[0]
        clock[0] = 100.0
        before = len(app._notifications)

        row = _row(app, "command:proposals")

        assert row.availability.invocable is False
        assert row.availability.reason == UnavailableReason(
            AvailabilityCode.NO_SELECTION, "No pending write proposals", severity="information"
        )
        assert len(app._notifications) == before
        # `expire_all` never sweeps, so it can only return a proposal the
        # derivation left in its pending state.
        assert [p.id for p in store.expire_all(reason="context switched")] == [pending.id]


async def test_an_emptied_inbox_refuses_the_selected_proposals_row(tmp_path: Path) -> None:
    """The same re-resolution every row gets, driven by real state.

    The palette is opened while a proposal is pending, so the row is
    offered; the proposal is then cancelled (an external MCP client can do
    that at any moment) while the modal is up. On dismissal the app
    re-derives the catalog, finds the owner now refusing, and says so with
    the owner's own wording - opening no review.
    """
    store = ProposalStore()
    app = _proposals_app(store, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit_proposal(app)
        palette = await _open_palette(pilot)
        assert _row(app, "command:proposals").availability.invocable is True
        pending = store.pending()[0]
        assert store.cancel(pending.id, session_id="sess-1") is True
        # Re-derived from the same live owner the dismissal will consult.
        assert _row(app, "command:proposals").availability.invocable is False
        palette.dismiss("command:proposals")
        await until(
            pilot,
            lambda: any("No pending write proposals" in n.message for n in app._notifications),
            label="the emptied inbox reported",
        )
        note = next(n for n in app._notifications if "No pending write proposals" in n.message)
        # The palette's own refusal path, not a dispatched `:proposals`:
        # owner reason text is notified literally (`markup=False`).
        assert note.markup is False
        assert note.severity == "information"
        assert app.screen is app.screen_stack[0]
        assert not isinstance(app.screen, ConfirmScreen)


# ---------------------------------------------------------------------------
# `Ctrl-S` answers to the buffer behind the pane
# ---------------------------------------------------------------------------


async def test_the_log_save_row_reports_an_empty_buffer() -> None:
    """A visible-but-empty log pane is a `Ctrl-S` that saves nothing.

    The binding stays enabled - pane visibility is all it ever gated on,
    and that is unchanged - so the row is offered with the owner's reason
    attached rather than disappearing. One streamed line is enough to make
    it invocable again (issue #388, round 6).
    """
    app = _build_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app._logs.open_pane("default", [("web", "app")])
        log_pane = app.query_one(LogPane)
        await until(pilot, lambda: log_pane.display, label="log pane open")
        before = len(app._notifications)
        row = _row(app, "action:log_save")
        assert row.availability.binding_enabled is True
        assert row.availability.reason == UnavailableReason(
            AvailabilityCode.NO_SELECTION, "Log buffer is empty — nothing to save"
        )
        # The other pane-local toggles answer only to the pane itself.
        assert _row(app, "action:log_wrap").availability == ActionAvailability.enabled()
        assert len(app._notifications) == before
        buffer = app._logs.buffer
        assert buffer is not None
        buffer.append(LogLine(pod="web", container="app", text="hello", timestamp=None))
        assert _row(app, "action:log_save").availability == ActionAvailability.enabled()
        await app._logs.cancel_tasks()


async def _idle_stream(
    namespace: str, pod: str, container: str, **_: Any
) -> AsyncIterator[LogLine]:
    """A wired log stream that opens and then simply stays open."""
    await asyncio.Event().wait()
    yield LogLine(pod=pod, container=container, text="", timestamp=None)  # pragma: no cover


async def test_the_logs_row_reports_a_full_log_pane() -> None:
    """Four pods are already streaming, so `l` on a fifth only reports the
    cap. The palette offered the row as runnable; now it carries the
    owner's bounded reason, and the probe stays silent.
    """
    pods = [_pod(f"web-{index}") for index in range(5)]
    app = _build_app(rows=pods, stream_logs=_idle_stream)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 5, label="pods loaded")
        streaming = [("default", f"web-{index}", "") for index in range(4)]
        await app._logs.open_agent_logs("default", streaming)
        log_pane = app.query_one(LogPane)
        await until(pilot, lambda: log_pane.display, label="log pane open")
        table.move_cursor(row=4)
        await until(
            pilot,
            lambda: str(table.get_row_at(table.cursor_row)[0]) == "web-4",
            label="the pod that is not streaming is selected",
        )
        before = len(app._notifications)
        row = _row(app, "action:logs")
        assert row.availability.binding_enabled is True
        assert row.availability.reason is not None
        assert row.availability.reason.message == "Log pane caps at 4 pods — Esc closes all"
        assert len(app._notifications) == before
        await app._logs.cancel_tasks()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_the_palette_closes_its_door_once_the_app_is_exiting() -> None:
    """`_accepting_input()` is the one place that reads Textual's exit flag;
    this pins its observable effect rather than the attribute."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._accepting_input() is True
        assert app._actions.binding_enabled("open_action_palette") is True
        app.exit()
        assert app._accepting_input() is False
        assert app._actions.binding_enabled("open_action_palette") is False
        assert app._actions.availability("open_action_palette").reason == UnavailableReason(
            AvailabilityCode.PROTECTED_UI, "korvid is shutting down"
        )


# ---------------------------------------------------------------------------
# Dispatch: one route, once, after the modal is gone
# ---------------------------------------------------------------------------


async def test_palette_action_dispatches_once_after_dismissal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("web")])
    calls = 0
    original = app.action_help

    def counted_help() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(app, "action_help", counted_help)
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await _select_palette_entry(pilot, "help", "action:help")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        assert calls == 1
        assert not any(isinstance(screen, ActionPaletteScreen) for screen in app.screen_stack)


async def test_palette_command_posts_the_typed_pulse_route_once() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await _select_palette_entry(pilot, "pulse", "command:pulse")
        await until(pilot, lambda: isinstance(app.screen, PulseScreen), label="pulse open")
        assert len([s for s in app.screen_stack if isinstance(s, PulseScreen)]) == 1


async def test_palette_enter_never_reaches_an_approval_dialog(tmp_path: Path) -> None:
    """The Enter that runs a palette entry must not also answer the approval
    dialog that entry opens: the palette consumes the keystroke, and the
    dialog is confirmed only by a fresh user keystroke (security invariant).

    The audit file is the second witness: korvid's audit is fail-closed and
    written around the write itself, so a file that never appears is proof
    that no write was attempted, not merely that the recorder was missed.
    """
    audit_path = tmp_path / "audit.jsonl"
    recorder = Recorder()
    app = make_write_app(recorder, audit_path)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count > 0,
            label="pods loaded",
        )
        await _select_palette_entry(pilot, "delete resource", "action:delete_resource")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="approval dialog open",
        )
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert recorder.calls == []
        assert not audit_path.exists()
        # Walking away from the dialog is still the no-op it always was.
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.screen is app.screen_stack[0],
            label="approval dismissed",
        )
        assert recorder.calls == []
        assert not audit_path.exists()


async def test_palette_enter_cannot_satisfy_a_write_parameter_prompt(tmp_path: Path) -> None:
    """Scale asks for a count before it asks for approval, and `Enter` alone
    submits that prompt - it is prefilled with the current replica count so
    a deliberate Enter keeps it (see `ReplicasPrompt`). That makes it the
    sharpest test of the same invariant as the approval dialog: if the
    palette's selecting Enter leaked into the modal it opened, the prompt
    would submit itself and the flow would already be at the confirmation,
    one keystroke from a write nobody typed a number for.
    """
    audit_path = tmp_path / "audit.jsonl"
    recorder = Recorder()
    app = make_write_app(recorder, audit_path)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count > 0,
            label="pods loaded",
        )
        await _to_view(pilot, "deployments")
        await _select_palette_entry(pilot, "scale resource", "action:scale_resource")
        await until(
            pilot,
            lambda: isinstance(app.screen, ReplicasPrompt),
            label="replicas prompt open",
        )
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ReplicasPrompt)
        # Still waiting for a number, not already past it.
        assert screen.query_one(Input).value == "3"
        assert not any(isinstance(item, ConfirmScreen) for item in app.screen_stack)
        assert recorder.calls == []
        assert not audit_path.exists()
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.screen is app.screen_stack[0],
            label="replicas prompt dismissed",
        )
        assert recorder.calls == []
        assert not audit_path.exists()


# ---------------------------------------------------------------------------
# Re-resolution: the list is rebuilt, the id re-checked, nothing stale runs
# ---------------------------------------------------------------------------


async def test_a_stale_selection_notifies_and_dispatches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The screen returns only a stable id; the app re-derives fresh entries
    after dismissal, so an entry that disappeared meanwhile cannot run."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        screen = await _open_palette(pilot)
        await _type_query(pilot, "help")
        options = screen.query_one(OptionList)
        await until(
            pilot,
            lambda: options.option_count > 0 and options.get_option_at_index(0).id == "action:help",
            label="help ranked first",
        )
        monkeypatch.setattr(app, "_palette_entries", list)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("no longer available" in n.message for n in app._notifications),
            label="stale-selection notice",
        )
        assert not isinstance(app.screen, HelpScreen)


async def test_an_entry_that_became_unavailable_reports_its_owner_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Availability is re-checked against freshly derived entries, and the
    owner's own wording is what the user sees - no second vocabulary."""
    app = make_app([_pod("web")])
    reason = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a resource first")
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        screen = await _open_palette(pilot)
        await _type_query(pilot, "help")
        options = screen.query_one(OptionList)
        await until(
            pilot,
            lambda: options.option_count > 0 and options.get_option_at_index(0).id == "action:help",
            label="help ranked first",
        )
        monkeypatch.setattr(
            app,
            "_palette_entries",
            lambda: [
                _entry("action:help", ActionAvailability(binding_enabled=True, reason=reason))
            ],
        )
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any(reason.message in n.message for n in app._notifications),
            label="owner reason notified",
        )
        assert not isinstance(app.screen, HelpScreen)


async def test_a_selection_is_rechecked_against_the_view_that_is_on_screen_now() -> None:
    """The same re-resolution, driven by real state instead of a patch.

    The palette is opened on the pods view, where `hint_details` applies;
    the workspace then navigates to nodes while the modal is up. The entry
    still exists after dismissal - so this is not the "disappeared" path -
    but the view it belongs to is gone, and the answer the user sees is the
    policy's own wording for that, with nothing dispatched.
    """
    app = make_navigation_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        palette = await _open_palette(pilot)
        # The *binding* is what the view gates: on pods, `hint_details` is
        # bound and dispatchable (whether this particular row has a hint to
        # open is the owner's separate question, tested above).
        assert app._actions.availability("hint_details").binding_enabled is True
        await app._workspace_ctl.navigate("nodes", "default")
        await until(pilot, lambda: app.current_kind == "nodes", label="nodes view active")
        palette.dismiss("action:hint_details")
        await until(
            pilot,
            lambda: any("Not available in this view" in n.message for n in app._notifications),
            label="stale action refused",
        )
        assert app.screen is app.screen_stack[0]
        assert {entry.id for entry in app._palette_entries()} >= {"action:hint_details"}


# ---------------------------------------------------------------------------
# Import boundary: the palette searches and routes, it never writes
# ---------------------------------------------------------------------------


#: Modules a palette module must never import: the write path itself, the
#: audit log behind it, the coordinator that owns the approval perimeter,
#: the controller that composes the flows, and the probe built over them.
_FORBIDDEN_MODULES = {
    "korvid.k8s.writes",
    "korvid.core.audit",
    "korvid.ui.write_coordinator",
    "korvid.ui.resource_write_controller",
    "korvid.ui.write_availability",
}
_FORBIDDEN_SYMBOLS = {
    "WriteOps",
    "WriteCoordinator",
    "ResourceWriteController",
    "WriteAvailability",
    "AuditLog",
}


def _imported_names(source: str, package: str) -> set[str]:
    """Every module and symbol `source` imports, as absolute dotted names.

    Relative imports are resolved against `package`, because
    `from .resource_write_controller import ...` and
    `from korvid.ui.resource_write_controller import ...` name the same
    module: a boundary asserted only against the absolute spelling could be
    re-entered through the shorter one. `from . import x` (no module) and
    `from ..k8s.writes import x` (parent package) resolve the same way.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            anchor = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
            base = f"{anchor}.{node.module}" if node.module else anchor
        else:
            base = node.module or ""
        names.add(base)
        names.update(f"{base}.{alias.name}" for alias in node.names)
        names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize(
    ("source", "package", "expected"),
    [
        (
            "from .resource_write_controller import ResourceWriteController\n",
            "korvid.ui",
            "korvid.ui.resource_write_controller",
        ),
        ("from . import write_coordinator\n", "korvid.ui", "korvid.ui.write_coordinator"),
        (
            "from ..resource_write_controller import RESTARTABLE\n",
            "korvid.ui.widgets",
            "korvid.ui.resource_write_controller",
        ),
        ("from ...core.audit import AuditLog\n", "korvid.ui.widgets", "korvid.core.audit"),
        ("import korvid.k8s.writes\n", "korvid.ui", "korvid.k8s.writes"),
        (
            "from korvid.ui.write_availability import WriteAvailability\n",
            "korvid.ui",
            "korvid.ui.write_availability",
        ),
    ],
)
def test_the_import_scanner_resolves_every_spelling_of_a_forbidden_module(
    source: str, package: str, expected: str
) -> None:
    """The boundary below is only as strong as this resolver: a sibling or
    parent relative import must report the same absolute module an
    equivalent absolute import would, so neither spelling can slip past."""
    assert expected in _imported_names(source, package)
    assert _imported_names(source, package) & _FORBIDDEN_MODULES


def test_palette_modules_do_not_import_write_implementations() -> None:
    """Neither palette module may reach a write, an audit or a dry-run.

    The palette's only route to an action is the app's own
    `run_action`/`parse_command` - the same route a key takes, with the
    same approval and audit behind it. A direct import of a write
    implementation here would be the first step of a second route that
    skips it, so the boundary is asserted structurally rather than trusted.
    """
    for module in (palette_domain, palette_modal):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        imported = _imported_names(source, str(module.__package__))
        assert not imported & _FORBIDDEN_MODULES, f"{module.__name__} imports a write module"
        assert not imported & _FORBIDDEN_SYMBOLS, f"{module.__name__} imports a write symbol"
        for symbol in sorted(_FORBIDDEN_SYMBOLS | _FORBIDDEN_MODULES):
            assert symbol not in source, f"{module.__name__} names {symbol}"


# ---------------------------------------------------------------------------
# Protected and transient surfaces refuse the open
# ---------------------------------------------------------------------------


async def test_palette_refuses_to_open_while_the_command_bar_is_editing() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("colon")
        await until(pilot, lambda: app._command_bar.display, label="command bar open")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, ActionPaletteScreen)
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_palette_refuses_to_open_while_the_filter_bar_is_editing() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("slash")
        await until(pilot, lambda: app._filter_bar.display, label="filter bar open")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, ActionPaletteScreen)
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_palette_refuses_to_open_over_a_modal() -> None:
    """`Ctrl-P` is a priority binding, so it fires over any screen: the
    policy - not the screen stack - is what keeps it off a modal."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert len(app.screen_stack) == 2
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_palette_refuses_to_open_during_a_context_switch() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        app._ctx._switching = True
        try:
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert not isinstance(app.screen, ActionPaletteScreen)
            assert app._actions.binding_enabled("open_action_palette") is False
        finally:
            app._ctx._switching = False


async def test_the_direct_action_refuses_even_when_dispatch_was_bypassed() -> None:
    """`check_action` already refuses the key, but the action itself must
    not be a back door for any caller that reaches it another way."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        app.action_open_action_palette()
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert len(app.screen_stack) == 2


# ---------------------------------------------------------------------------
# Owner text is data, not markup
# ---------------------------------------------------------------------------


async def test_an_owner_reason_is_notified_literally_not_as_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability reasons quote install hints like `pip install korvid[mcp]`.
    Rendered as Rich content markup, `[mcp]` is a style tag: the text the
    user needs would silently disappear (or restyle the toast). The palette
    notifies the owner's message literally, exactly as the owners' own
    handlers do with `markup=False` (task 6 re-review)."""
    message = "MCP unavailable — install korvid[mcp] to enable it"
    reason = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, message)
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        monkeypatch.setattr(
            app,
            "_palette_entries",
            lambda: [
                _entry("action:help", ActionAvailability(binding_enabled=True, reason=reason))
            ],
        )
        await app._palette_selected("action:help")
        await until(
            pilot,
            lambda: any(n.message == message for n in app._notifications),
            label="owner reason notified",
        )
        notification = next(n for n in app._notifications if n.message == message)
        assert notification.markup is False
        # Why it matters: as markup, the bracketed extra name is parsed away.
        assert Content.from_markup(message).plain != message
        assert Content(notification.message).plain == message


# ---------------------------------------------------------------------------
# Read actions: a row that cannot do anything says so, silently
# ---------------------------------------------------------------------------


_READ_MANIFEST = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "web", "namespace": "default"},
    "spec": {"containers": [{"name": "app", "image": "nginx:latest"}]},
}


async def _read_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    return dict(_READ_MANIFEST)


async def _read_relationship_objects(
    meta: ResourceMeta, namespace: str | None
) -> list[GenericSummary]:
    return []


def _reading_app(rows: Sequence[Summary] | None = None) -> KorvidApp:
    """An app with every read capability composed.

    `d` has a manifest fetcher and `g` a relationship lister, so what the
    palette reports about those rows is about the *table* - what is
    selected, what the row is - rather than about a capability this
    composition never had.
    """
    return _reading_app_with(rows)


def _reading_app_with(rows: Sequence[Summary] | None = None, **kwargs: Any) -> KorvidApp:
    return _build_app(
        rows,
        get_manifest=_read_manifest,
        list_relationship_objects=_read_relationship_objects,
        **kwargs,
    )


def _entry_for(app: KorvidApp, entry_id: str) -> PaletteEntry:
    """One freshly derived palette row, judged by the real wiring."""
    return next(entry for entry in app._palette_entries() if entry.id == entry_id)


def _unready_pod(name: str) -> Summary:
    """A Running pod that is not fully ready - `pod_needs_hint` is True."""
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="0/1",
        restarts=0,
        node=None,
        qos="-",
    )


async def test_describe_and_relationships_refuse_an_empty_table() -> None:
    """Both keys stop at the selected row, so with nothing selected the
    palette must say so instead of advertising a no-op."""
    app = _reading_app([])
    async with app.run_test() as pilot:
        await pilot.pause()
        for entry_id in ("action:describe", "action:relationships"):
            reason = _entry_for(app, entry_id).availability.reason
            assert reason is not None, entry_id
            assert reason.message == "No resource selected"


async def test_describe_and_relationships_are_invocable_with_a_row_selected() -> None:
    app = _reading_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert _entry_for(app, "action:describe").availability.invocable is True
        assert _entry_for(app, "action:relationships").availability.invocable is True


async def test_relationships_reports_a_composition_without_a_loader() -> None:
    """`g` notifies "Relationships unavailable in this session" when no
    loader was composed; the palette row says exactly that."""
    app = _build_app(get_manifest=_read_manifest)
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        reason = _entry_for(app, "action:relationships").availability.reason
        assert reason is not None
        assert reason.message == "Relationships unavailable in this session"


async def test_describe_reports_a_composition_without_a_manifest_fetcher() -> None:
    app = _build_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        reason = _entry_for(app, "action:describe").availability.reason
        assert reason is not None
        assert reason.message == "Describe unavailable"


async def test_hint_details_refuses_a_row_that_has_no_hint() -> None:
    """`h` opens the detail overlay only for a row the hint strip flagged;
    a healthy pod has nothing to expand."""
    app = _reading_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        reason = _entry_for(app, "action:hint_details").availability.reason
        assert reason is not None
        assert "hint" in reason.message.casefold()


async def test_hint_details_is_invocable_for_a_row_that_has_one() -> None:
    app = _reading_app([_unready_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert _entry_for(app, "action:hint_details").availability.invocable is True


async def test_search_next_refuses_a_closed_pane_while_search_prev_still_sorts() -> None:
    """`n` does nothing with no pane open, but `N` falls back to sorting by
    name - a real effect - so only `n` is refused."""
    app = _reading_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        reason = _entry_for(app, "action:log_search_next").availability.reason
        assert reason is not None
        assert _entry_for(app, "action:log_search_prev").availability.invocable is True


async def test_search_rows_follow_the_open_panes_active_search() -> None:
    """With the describe pane open, `n`/`N` step through *its* hits, so both
    are refused until a search has actually matched something."""
    app = _reading_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        pane = app.query_one(DescribePane)
        pane.show("Pod: web", dict(_READ_MANIFEST), [])
        await until(pilot, lambda: pane.display, label="describe pane visible")
        assert _entry_for(app, "action:log_search_next").availability.invocable is False
        assert _entry_for(app, "action:log_search_prev").availability.invocable is False
        await pilot.press("slash")
        await _type_query(pilot, "nginx")
        await pilot.press("enter")
        await until(pilot, lambda: pane.has_search_hits, label="search hits")
        assert _entry_for(app, "action:log_search_next").availability.invocable is True
        assert _entry_for(app, "action:log_search_prev").availability.invocable is True


async def test_probing_the_read_actions_notifies_nothing() -> None:
    """Every probe is synchronous and silent: the states that make the real
    keys warn produce palette reasons without a single notification."""
    app = _reading_app([])
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app._notifications)
        for _ in range(3):
            app._palette_entries()
        await _open_palette(pilot)
        assert len(app._notifications) == before


async def test_the_describe_key_still_notifies_what_the_probe_only_reported() -> None:
    """The probe stays silent; the keypress keeps its own warning."""
    app = _reading_app([])
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app._notifications)
        assert _entry_for(app, "action:describe").availability.reason is not None
        assert len(app._notifications) == before
        await pilot.press("d")
        await until(
            pilot,
            lambda: any("No resource selected" in n.message for n in app._notifications),
            label="describe key notified",
        )


# ---------------------------------------------------------------------------
# The metric sort keys: `C` / `M` only sort where those columns exist
# ---------------------------------------------------------------------------


_METRIC_SORT_ROWS = ("action:sort_by_cpu", "action:sort_by_mem")


async def test_the_metric_sort_rows_are_refused_off_the_pods_view() -> None:
    """`C`/`M` call `sort_by("cpu"/"mem")`, which returns without touching
    the order on any view but pods - no CPU/MEM column, no metrics feed.

    The palette offered both rows as invocable there, so selecting one did
    nothing and said nothing. The owner now answers why, silently: the
    name/age keys and the sort picker are untouched, because they really
    do sort that view.
    """
    app = make_navigation_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        await app._workspace_ctl.navigate("nodes", "default")
        await until(pilot, lambda: app.current_kind == "nodes", label="nodes view active")
        before = len(app._notifications)
        for entry_id in _METRIC_SORT_ROWS:
            reason = _entry_for(app, entry_id).availability.reason
            assert reason is not None, entry_id
            assert reason.message.endswith("is not a column on this view")
        assert _entry_for(app, "action:sort_by_age").availability.invocable is True
        assert _entry_for(app, "action:sort_picker").availability.invocable is True
        assert len(app._notifications) == before


async def test_the_metric_sort_rows_are_invocable_on_the_pods_view() -> None:
    """Where the handler really sorts, the rows stay runnable."""
    app = make_navigation_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        assert app.current_kind == "pods"
        for entry_id in _METRIC_SORT_ROWS:
            assert _entry_for(app, entry_id).availability.invocable is True, entry_id


async def test_the_metric_sort_rows_follow_a_replace_view() -> None:
    """`replace: true` hides AGE/CPU/MEM, and `sort_by` refuses to reorder
    rows by a column nobody can see - even on pods. The row says so."""
    view = ViewConfig(columns=(CustomColumn("TEAM", "label", "team"),), replace=True)
    app = make_app([_pod("web")], config=KorvidConfig(namespace="default", views={"pods": view}))
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        for entry_id in _METRIC_SORT_ROWS:
            reason = _entry_for(app, entry_id).availability.reason
            assert reason is not None, entry_id
            assert reason.message.startswith("This view replaces the")


async def test_the_refused_metric_sort_key_still_changes_nothing() -> None:
    """The probe reports what the keypress does: pressing `C` off the pods
    view leaves the order alone and stays silent, exactly as before."""
    app = make_navigation_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        await app._workspace_ctl.navigate("nodes", "default")
        await until(pilot, lambda: app.current_kind == "nodes", label="nodes view active")
        before = len(app._notifications)
        await pilot.press("C")
        await pilot.pause()
        assert "nodes" not in app._workspace.focused.sorts
        assert len(app._notifications) == before


# ---------------------------------------------------------------------------
# `A`: a replacement view hides the AGE column the key would sort by
# ---------------------------------------------------------------------------


def _replacement_pods_app() -> KorvidApp:
    """A pods view whose `replace: true` columns hide NAME's neighbours.

    The same view the metric-sort rows already answer for: `sort_by`
    refuses every builtin but NAME on it, because reordering rows by a
    column the view does not render moves them with no indicator.
    """
    view = ViewConfig(columns=(CustomColumn("TEAM", "label", "team"),), replace=True)
    return make_app([_pod("web")], config=KorvidConfig(namespace="default", views={"pods": view}))


async def test_the_age_sort_row_is_refused_on_a_replacement_view() -> None:
    """`A` calls `sort_by("age")`, which a `replace: true` view discards
    without a notification - the same silence `C`/`M` were fixed for.

    The palette still offered the row as invocable, so selecting it did
    nothing and said nothing. The refusal is the bounded one the metric
    keys already use, because it is row text on a 36-column terminal.
    """
    app = _replacement_pods_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        reason = _entry_for(app, "action:sort_by_age").availability.reason
        assert reason is not None
        assert reason.message == "This view replaces the AGE column"


async def test_the_age_sort_row_is_invocable_where_the_column_is_rendered() -> None:
    """AGE is not a metrics column: every normal view has one, so the row
    stays runnable on pods *and* on a view the metric keys are refused on."""
    app = make_navigation_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        assert app.current_kind == "pods"
        assert _entry_for(app, "action:sort_by_age").availability.invocable is True
        await app._workspace_ctl.navigate("nodes", "default")
        await until(pilot, lambda: app.current_kind == "nodes", label="nodes view active")
        assert _entry_for(app, "action:sort_by_age").availability.invocable is True
        assert _entry_for(app, "action:sort_picker").availability.invocable is True


async def test_the_refused_age_sort_key_still_changes_nothing() -> None:
    """The probe reports what the keypress does: `A` on a replacement view
    leaves the order alone and stays silent, exactly as before."""
    app = _replacement_pods_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        before = len(app._notifications)
        await pilot.press("A")
        await pilot.pause()
        assert "pods" not in app._workspace.focused.sorts
        assert len(app._notifications) == before


async def test_dispatching_the_refused_age_sort_row_sorts_nothing() -> None:
    """The palette's own route is the other half of the evidence: running
    `sort_by_age` through `_palette_selected` reaches the same handler, so
    the refused row would have been a no-op there too."""
    app = _replacement_pods_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app._palette_selected("action:sort_by_age")
        await pilot.pause()
        assert "pods" not in app._workspace.focused.sorts
        assert any(n.message == "This view replaces the AGE column" for n in app._notifications)


async def test_probing_the_sort_rows_notifies_nothing() -> None:
    """Every sort probe is synchronous and silent: deriving the rows on the
    view that refuses all three says nothing at all."""
    app = _replacement_pods_app()
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        before = len(app._notifications)
        for _ in range(3):
            app._palette_entries()
        await _open_palette(pilot)
        assert len(app._notifications) == before
        assert "pods" not in app._workspace.focused.sorts
