"""ResizePrompt collects per-container requests/limits for in-place pod
resize (issue #27). Prefilled with current values; submit returns only the
quantities that actually changed (strategic merge keeps the rest)."""

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from korvid.ui.widgets.resize_prompt import ResizePrompt

_CONTAINERS = [
    (
        "app",
        {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "250m", "memory": "256Mi"},
        },
    ),
    ("sidecar", {"requests": {"cpu": "50m"}}),
]


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


async def _open(app: HostApp) -> ResizePrompt:
    prompt = ResizePrompt("pods/web-1 in default", containers=_CONTAINERS)

    def _done(v: object) -> None:
        app.result = v

    await app.push_screen(prompt, _done)
    return prompt


async def test_inputs_prefilled_with_current_values() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await pilot.pause()
        values = {i.id: i.value for i in app.screen.query(Input)}
        assert values["resize-0-requests-cpu"] == "100m"
        assert values["resize-0-limits-memory"] == "256Mi"
        # unset quantities show empty (= keep as-is)
        assert values["resize-1-requests-memory"] == ""
        assert values["resize-1-limits-cpu"] == ""


async def test_submit_returns_only_changed_quantities() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await pilot.pause()
        app.screen.query_one("#resize-0-requests-cpu", Input).value = "200m"
        app.screen.query_one("#resize-1-requests-cpu", Input).value = "75m"
        app.screen.query_one("#resize-0-requests-cpu", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == {
            "app": {"requests": {"cpu": "200m"}},
            "sidecar": {"requests": {"cpu": "75m"}},
        }


async def test_escape_cancels_with_none() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


async def test_invalid_quantity_blocks_submit() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await pilot.pause()
        app.screen.query_one("#resize-0-requests-cpu", Input).value = "lots"
        app.screen.query_one("#resize-0-requests-cpu", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_submit_with_no_changes_stays_open() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await pilot.pause()
        app.screen.query_one("#resize-0-requests-cpu", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_clearing_a_field_keeps_current_value() -> None:
    """Empty input means 'keep as-is', not 'remove the quantity'."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await pilot.pause()
        app.screen.query_one("#resize-0-requests-cpu", Input).value = ""
        app.screen.query_one("#resize-0-limits-cpu", Input).value = "500m"
        app.screen.query_one("#resize-0-limits-cpu", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == {"app": {"limits": {"cpu": "500m"}}}
