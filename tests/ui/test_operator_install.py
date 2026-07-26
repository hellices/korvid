"""OperatorInstallPrompt collects namespace/channel/approval for an OLM
Subscription (issue #29). Prefilled from the PackageManifest's install facts;
submit validates against the known channels and approval modes."""

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from korvid.k8s.olm import PackageInstallFacts
from korvid.ui.widgets.operator_install import OperatorInstallPrompt

from .waits import until

_FACTS = PackageInstallFacts(
    package="cert-manager",
    channels=("candidate", "stable"),
    default_channel="stable",
    catalog_source="operatorhubio-catalog",
    catalog_source_namespace="olm",
)


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


async def _open(app: HostApp, facts: PackageInstallFacts = _FACTS) -> OperatorInstallPrompt:
    prompt = OperatorInstallPrompt(facts, namespace="operators")

    def _done(v: object) -> None:
        app.result = v

    await app.push_screen(prompt, _done)
    return prompt


async def _opened(app: HostApp, pilot: object) -> None:
    """Poll until the prompt's inputs are composed (no fixed sleeps)."""
    await until(pilot, lambda: bool(app.screen.query(Input)), label="prompt inputs composed")


async def test_inputs_prefilled_with_defaults() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        values = {i.id: i.value for i in app.screen.query(Input)}
        assert values["install-namespace"] == "operators"
        assert values["install-channel"] == "stable"
        assert values["install-approval"] == "Automatic"


async def test_submit_returns_choices() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#install-channel", Input).value = "candidate"
        app.screen.query_one("#install-approval", Input).value = "Manual"
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed with choices")
        assert app.result == ("operators", "candidate", "Manual")


async def test_unknown_channel_is_rejected() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#install-channel", Input).value = "nightly"
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("unknown channel" in str(n.message) for n in app._notifications),
            label="rejection notified",
        )
        assert app.result == "unset"  # still open, nothing dismissed
        assert app.screen is prompt


async def test_unknown_approval_mode_is_rejected() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#install-approval", Input).value = "Sometimes"
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("approval must be" in str(n.message) for n in app._notifications),
            label="rejection notified",
        )
        assert app.result == "unset"
        assert app.screen is prompt


async def test_blank_namespace_is_rejected() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#install-namespace", Input).value = "  "
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any(
                "namespace must not be blank" in str(n.message) for n in app._notifications
            ),
            label="rejection notified",
        )
        assert app.result == "unset"
        assert app.screen is prompt


async def test_unknown_channels_allowed_when_facts_have_none() -> None:
    """A PackageManifest with a malformed status has no channel list; the
    user-typed channel is then passed through (the server validates)."""
    facts = PackageInstallFacts(
        package="mystery",
        channels=(),
        default_channel="",
        catalog_source="cat",
        catalog_source_namespace="olm",
    )
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, facts)
        await _opened(app, pilot)
        app.screen.query_one("#install-channel", Input).value = "beta"
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert app.result == ("operators", "beta", "Automatic")


async def test_escape_cancels_with_none() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.result != "unset", label="prompt cancelled")
        assert app.result is None


async def test_enter_buffered_before_prompt_does_not_submit_defaults() -> None:
    """An Enter typed while the caller fetched the PackageManifest is
    created before the prompt exists and must not submit the wizard with
    defaults; a fresh Enter afterwards still submits."""
    from textual import events

    app = HostApp()
    async with app.run_test() as pilot:
        # Timestamped before the prompt exists, delivered after (same
        # situation as a keystroke queued during the manifest fetch).
        stale = events.Key("enter", None)
        await pilot.pause()

        await _open(app)
        await _opened(app, pilot)
        focused = app.focused
        assert focused is not None
        focused.post_message(stale)
        await pilot.pause()
        assert app.result == "unset"  # stale Enter discarded
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="fresh Enter submits")
        result: object = app.result
        assert result == ("operators", "stable", "Automatic")
