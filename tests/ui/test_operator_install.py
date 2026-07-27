"""OperatorInstallPrompt collects namespace/channel/approval for an OLM
Subscription (issue #29). Prefilled from the PackageManifest's install facts;
submit validates against the known channels and approval modes."""

from textual.app import App, ComposeResult
from textual.widgets import Input, Select, Static

from korvid.k8s.olm import APPROVAL_MODES, PackageInstallFacts
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
        assert app.screen.query_one("#install-namespace", Input).value == "operators"
        assert app.screen.query_one("#install-channel", Select).value == "stable"
        assert app.screen.query_one("#install-approval", Select).value == "Automatic"


async def test_channel_and_approval_are_selects_with_known_options() -> None:
    """Channel/approval value sets are known (issue #62): Select widgets
    make typos structurally impossible instead of failing on submit."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        channel = app.screen.query_one("#install-channel", Select)
        approval = app.screen.query_one("#install-approval", Select)
        assert [v for _, v in channel._options] == ["candidate", "stable"]
        assert [v for _, v in approval._options] == list(APPROVAL_MODES)


async def test_submit_returns_choices() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#install-channel", Select).value = "candidate"
        app.screen.query_one("#install-approval", Select).value = "Manual"
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed with choices")
        assert app.result == ("operators", "candidate", "Manual")


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
        assert not app.screen.query("Select#install-channel")  # fallback is a text input
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
        await _open(app)
        await _opened(app, pilot)
        focused = app.focused
        assert focused is not None
        focused.post_message(stale)
        # A fresh interaction drains the queued stale event deterministically
        # before the assertion (repository convention: no raw time pauses).
        # tab+shift+tab returns focus to the namespace input, where Enter
        # submits (on a Select it would open the dropdown instead).
        await pilot.press("tab")
        await pilot.press("shift+tab")
        assert app.result == "unset"  # stale Enter discarded
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="fresh Enter submits")
        result: object = app.result
        assert result == ("operators", "stable", "Automatic")


async def test_default_channel_missing_from_list_falls_back_to_first() -> None:
    """A default channel absent from the channel list (inconsistent catalog
    status) must not break the Select: the first known channel is offered."""
    facts = PackageInstallFacts(
        package="odd",
        channels=("alpha", "beta"),
        default_channel="nightly",
        catalog_source="cat",
        catalog_source_namespace="olm",
    )
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, facts)
        await _opened(app, pilot)
        assert app.screen.query_one("#install-channel", Select).value == "alpha"


async def test_submit_button_submits_when_a_select_has_focus() -> None:
    """Enter on a focused Select opens its overlay instead of submitting;
    the explicit Install button provides a submit path from any field."""
    from textual.widgets import Button

    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#install-approval", Select).focus()
        await until(
            pilot,
            lambda: app.focused is app.screen.query_one("#install-approval", Select),
            label="approval select focused",
        )
        button = app.screen.query_one("#install-submit", Button)
        button.press()
        await until(pilot, lambda: app.result != "unset", label="button submits")
        assert app.result == ("operators", "stable", "Automatic")
