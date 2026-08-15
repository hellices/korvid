"""Approval dialogs for write operations (spec §5 #4, §6.2).

ConfirmScreen resolves True only from a user keystroke; the layered variant
(require_name) demands typing the resource name exactly. ReplicasPrompt asks
for a replica count.
"""

from textual.app import App, ComposeResult
from textual.widgets import Static

from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


async def test_y_confirms() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(ConfirmScreen("Delete pod default/web-1", "delete pods/web-1"), _done)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.result is True


async def test_n_declines_with_false() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None, app: HostApp = app) -> None:
            app.result = v

        await app.push_screen(ConfirmScreen("Delete", "delete pods/x"), _done)
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.result is False


async def test_escape_dismisses_with_none() -> None:
    """Esc is a dismissal, not a decision: callers that must distinguish
    'declined' from 'walked away' (external proposals stay pending on
    dismissal) get None; every other caller treats None as falsy."""
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None, app: HostApp = app) -> None:
            app.result = v

        await app.push_screen(ConfirmScreen("Delete", "delete pods/x"), _done)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


async def test_dialog_shows_exact_operation() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConfirmScreen("Delete pod", "DELETE pods/web-1 in default"))
        await pilot.pause()
        texts = " ".join(str(s.content) for s in app.screen.query(Static))
        assert "DELETE pods/web-1 in default" in texts


async def test_require_name_blocks_y_shortcut() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete node", "delete nodes/worker-1", require_name="worker-1"), _done
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.result == "unset"  # y alone must not confirm


async def test_require_name_confirms_on_exact_match() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete node", "delete nodes/worker-1", require_name="worker-1"), _done
        )
        await pilot.pause()
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is True


async def test_require_name_rejects_wrong_name() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete node", "delete nodes/worker-1", require_name="worker-1"), _done
        )
        await pilot.pause()
        for ch in "wrong":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"  # stays open, not confirmed


async def test_replicas_prompt_returns_int() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: int | None) -> None:
            app.result = v

        await app.push_screen(ReplicasPrompt("deployments/web", current=2), _done)
        await pilot.pause()
        await pilot.press("5")
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == 5


async def test_replicas_prompt_escape_cancels() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: int | None) -> None:
            app.result = v

        await app.push_screen(ReplicasPrompt("deployments/web", current=2), _done)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


async def test_replicas_prompt_rejects_non_numeric() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: int | None) -> None:
            app.result = v

        await app.push_screen(ReplicasPrompt("deployments/web", current=2), _done)
        await pilot.pause()
        await pilot.press("backspace")  # clear the prefilled value
        await pilot.press("x")  # rejected by the integer input
        await pilot.press("enter")  # empty value -> not a number -> stays open
        await pilot.pause()
        assert app.result == "unset"  # stays open


async def test_replicas_prompt_prefills_current_value() -> None:
    from textual.widgets import Input

    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(ReplicasPrompt("deployments/web", current=3))
        await pilot.pause()
        assert app.screen.query_one(Input).value == "3"


async def test_replicas_prompt_unknown_current() -> None:
    from textual.widgets import Input

    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(ReplicasPrompt("deployments/web", current=None))
        await pilot.pause()
        assert app.screen.query_one(Input).value == ""
        labels = " ".join(str(s.render()) for s in app.screen.query(Static))
        assert "unknown" in labels


async def test_y_buffered_before_dialog_visible_is_discarded() -> None:
    """A 'y' typed while the caller's pre-checks ran (RBAC round trip) is
    created before the dialog exists and must never approve the operation;
    a fresh 'y' afterwards still confirms."""
    from textual import events

    app = HostApp()
    results: list[bool | None] = []
    async with app.run_test() as pilot:
        # Created before the dialog exists: same situation as a keystroke
        # buffered in the input queue during a stalled permission check.
        stale = events.Key("y", "y")
        await pilot.pause()

        dialog = ConfirmScreen("Delete pod default/web-1", "delete pods/web-1")
        await app.push_screen(dialog, results.append)
        await pilot.pause()
        dialog.post_message(stale)
        await pilot.pause()
        assert results == []  # stale keystroke discarded
        await pilot.press("y")
        await pilot.pause()
        assert results == [True]


async def test_require_name_discards_keys_buffered_before_dialog() -> None:
    """The typed-name variant applies the same stale-key cutoff: a resource
    name plus Enter buffered while the caller's pre-checks ran must never
    type into the input or approve the delete; fresh typing still works."""
    from textual import events

    app = HostApp()
    results: list[bool | None] = []
    async with app.run_test() as pilot:
        # Typed (and timestamped) before the dialog exists, delivered after.
        stale = [events.Key(ch, ch) for ch in "worker-1"] + [events.Key("enter", None)]
        await pilot.pause()

        dialog = ConfirmScreen("Delete node", "delete nodes/worker-1", require_name="worker-1")
        await app.push_screen(dialog, results.append)
        await pilot.pause()
        target = app.focused
        assert target is not None
        for key in stale:
            target.post_message(key)
        await pilot.pause()
        assert results == []  # neither typed nor submitted
        from textual.widgets import Input

        assert dialog.query_one(Input).value == ""
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert results == [True]


async def test_preview_lines_rendered() -> None:
    """Dry-run preview lines (issue #19) appear in the dialog."""
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen(
                "Scale deployments/web?",
                "PATCH deployments/web/scale: replicas 3 -> 5",
                preview=["~ spec.replicas: 3 -> 5"],
            )
        )
        await pilot.pause()
        node = app.screen.query_one(".confirm-preview", Static)
        assert "~ spec.replicas: 3 -> 5" in str(node.render())
        assert "dry-run" in str(node.render())


async def test_no_preview_widget_without_preview() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConfirmScreen("Delete pod", "DELETE pods/web-1"))
        await pilot.pause()
        assert not app.screen.query(".confirm-preview")


async def test_empty_preview_reports_no_changes() -> None:
    """A dry-run that produced no visible change is itself information."""
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen("Scale deployments/web?", "replicas 3 -> 3", preview=[])
        )
        await pilot.pause()
        node = app.screen.query_one(".confirm-preview", Static)
        assert "no changes" in str(node.render())


async def test_long_preview_body_is_scrollable() -> None:
    """A resize on a multi-container pod can produce more preview/operation
    lines than a short terminal shows; the dialog body must scroll so every
    requested change stays reviewable before approval."""
    from textual.containers import VerticalScroll

    app = HostApp()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(
            "Resize pods/web-1",
            "resize pods/web-1",
            preview=[f"~ line {i}" for i in range(200)],
        )
        await app.push_screen(screen)
        await pilot.pause()
        body = screen.query_one(VerticalScroll)
        assert body.allow_vertical_scroll


# ---------------------------------------------------------------------------
# Protected contexts (issue #83)
# ---------------------------------------------------------------------------


async def test_protected_context_blocks_y_shortcut() -> None:
    """In a protected context a bare `y` must never confirm a write."""
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete pod", "delete pods/web-1", protected_context="prod-eu"), _done
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.result == "unset"


async def test_protected_context_confirms_on_context_name() -> None:
    """Typing the protected context name exactly confirms the write."""
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete pod", "delete pods/web-1", protected_context="prod-eu"), _done
        )
        await pilot.pause()
        for ch in "prod-eu":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is True


async def test_protected_context_rejects_wrong_name() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete pod", "delete pods/web-1", protected_context="prod-eu"), _done
        )
        await pilot.pause()
        for ch in "nope":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"


async def test_protected_context_shows_banner() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen("Delete pod", "delete pods/web-1", protected_context="prod-eu")
        )
        await pilot.pause()
        texts = " ".join(str(s.content) for s in app.screen.query(Static))
        assert "PROTECTED" in texts
        assert "prod-eu" in texts


async def test_protected_context_keeps_resource_name_gate() -> None:
    """When require_name is also set, the resource name stays the typed gate
    (already the strongest layer) and the protected banner is still shown."""
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen(
                "Delete node",
                "delete nodes/worker-1",
                require_name="worker-1",
                protected_context="prod-eu",
            ),
            _done,
        )
        await pilot.pause()
        texts = " ".join(str(s.content) for s in app.screen.query(Static))
        assert "PROTECTED" in texts
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is True


async def test_ctrl_n_declines_with_a_require_name_gate() -> None:
    """With a typed gate the `n` key is input text, so an explicit decline
    control must exist — Esc alone would leave no way to deny (external
    proposals stay pending on dismissal)."""
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete node x", "delete nodes/x", require_name="x"), _done
        )
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.result is False


async def test_ctrl_n_declines_with_a_protected_context_gate() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen("Delete pod", "delete pods/x", protected_context="prod"), _done
        )
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.result is False


async def test_managed_note_renders_as_a_warning_line() -> None:
    """Issue #119: a write on a helm/operator-managed object shows an
    ownership banner above the preview — information before approval, the
    keystroke gate itself unchanged."""
    app = HostApp()
    async with app.run_test() as pilot:
        note = "managed by helm release web/nginx — change the chart values instead"
        await app.push_screen(
            ConfirmScreen(
                "Delete deployments/nginx?",
                "DELETE deployments/nginx in web",
                managed_note=note,
                preview=["- deployment nginx"],
            ),
            lambda v: None,
        )
        await pilot.pause()
        banner = app.screen.query_one(".confirm-managed", Static)
        assert note in str(banner.render())


async def test_no_managed_note_no_banner_widget() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen("Delete pods/web-1?", "DELETE pods/web-1 in default"),
            lambda v: None,
        )
        await pilot.pause()
        assert not app.screen.query(".confirm-managed")


async def test_managed_note_does_not_change_the_approval_gate() -> None:
    """The banner warns, never blocks: plain y still confirms."""
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: bool | None) -> None:
            app.result = v

        await app.push_screen(
            ConfirmScreen(
                "Scale deployments/web?",
                "PATCH deployments/web/scale: 3→0 in default",
                managed_note="managed by operator kafka-operator.v0.38 (CSV)",
            ),
            _done,
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.result is True


# ---------------------------------------------------------------------------
# Graph-derived impact section (issue #283)
# ---------------------------------------------------------------------------

_IMPACT_LINES = (
    "graph-derived impact (advisory):",
    "  delete apps/Deployment/prod/web",
    "  known direct dependents (may be affected): 1",
    "    - apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]",
    "  graph coverage: complete",
)


async def test_impact_section_renders_above_the_dry_run_preview() -> None:
    """The advisory section is additional context for the dry-run diff, so it
    must read before it, not replace or follow it."""
    from textual.containers import VerticalScroll

    app = HostApp()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            preview=["- apps/Deployment prod/web"],
            impact_lines=_IMPACT_LINES,
        )
        await app.push_screen(screen)
        await pilot.pause()
        children = list(screen.query_one(VerticalScroll).children)
        impact = screen.query_one(".confirm-impact", Static)
        preview = screen.query_one(".confirm-preview", Static)
        rendered = str(impact.render())
        assert children.index(impact) < children.index(preview)
        assert "graph-derived impact (advisory):" in rendered
        assert "known direct dependents (may be affected): 1" in rendered
        assert "graph coverage: complete" in rendered
        assert "dry-run" in str(preview.render())


async def test_only_the_impact_title_line_is_bold() -> None:
    """The title is the section heading; the facts under it are body text.
    Bolding every line (a `Text` *base* style applies to appended spans too)
    makes an advisory section shout louder than the operation itself."""
    app = HostApp()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            impact_lines=_IMPACT_LINES,
        )
        await app.push_screen(screen)
        await pilot.pause()
        text = screen._impact_text()
        assert text.style == ""
        bold = [span for span in text.spans if "bold" in str(span.style)]
        assert len(bold) == 1
        assert text.plain[bold[0].start : bold[0].end] == _IMPACT_LINES[0]


async def test_no_impact_widget_without_impact_lines() -> None:
    """No snapshot means no section at all - distinct from an empty one."""
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen("Delete pod", "DELETE pods/web-1", preview=["- pod prod/web-1"])
        )
        await pilot.pause()
        assert not app.screen.query(".confirm-impact")


async def test_impact_lines_render_cluster_markup_literally() -> None:
    """A resource named `[bold red]web[/]` must not style the dialog."""
    app = HostApp()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            impact_lines=(
                "graph-derived impact (advisory):",
                "    - Pod/prod/[bold red]web[/] via owned_by (declared) at"
                " metadata.ownerReferences[0]",
            ),
        )
        await app.push_screen(screen)
        await pilot.pause()
        impact = screen.query_one(".confirm-impact", Static)
        rendered = str(impact.render())
        assert impact._render_markup is False
        assert "[bold red]web[/]" in rendered


async def test_impact_section_does_not_relax_the_typed_name_gate() -> None:
    """An impact section is context, not consent: the typed-name gate still
    owns the decision."""
    app = HostApp()
    results: list[bool | None] = []
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen(
                "Delete node",
                "delete nodes/worker-1",
                require_name="worker-1",
                impact_lines=_IMPACT_LINES,
            ),
            results.append,
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert results == []
        # The stray "y" landed as ordinary text in the typed-name field (the
        # pinned behavior per test_ctrl_n_declines_with_a_require_name_gate:
        # under a typed gate, y/n are input text, not shortcuts) - clear it
        # before typing the real name the gate demands.
        await pilot.press("backspace")
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert results == [True]


async def test_impact_section_does_not_relax_the_stale_key_cutoff() -> None:
    """A `y` created while the impact snapshot was loading predates the
    dialog and must never approve it."""
    from textual import events

    app = HostApp()
    results: list[bool | None] = []
    async with app.run_test() as pilot:
        stale = events.Key("y", "y")
        await pilot.pause()
        dialog = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            impact_lines=_IMPACT_LINES,
        )
        await app.push_screen(dialog, results.append)
        await pilot.pause()
        dialog.post_message(stale)
        await pilot.pause()
        assert results == []
        await pilot.press("y")
        await pilot.pause()
        assert results == [True]
