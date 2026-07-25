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


async def test_n_and_escape_cancel() -> None:
    for key in ("n", "escape"):
        app = HostApp()
        async with app.run_test() as pilot:

            def _done(v: bool | None, app: HostApp = app) -> None:
                app.result = v

            await app.push_screen(ConfirmScreen("Delete", "delete pods/x"), _done)
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert app.result is False


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
