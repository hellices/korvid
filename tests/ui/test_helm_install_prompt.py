"""HelmInstallPrompt collects release/version/namespace/values choices for a
helm install or upgrade (issue #31). Prefilled from the picked chart; submit
validates the release and namespace names locally, helm stays the final
validator for everything else."""

from textual.app import App, ComposeResult
from textual.widgets import Input, Select, Static

from korvid.k8s.helmcli import ChartHit
from korvid.ui.widgets.helm_install import VALUES_MODES, HelmInstallPrompt, HelmReleaseChoices

from .waits import until

_CHART = ChartHit(
    name="bitnami/nginx",
    version="18.1.0",
    app_version="1.27.0",
    description="NGINX Open Source",
)


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


async def _open(
    app: HostApp,
    chart: ChartHit = _CHART,
    *,
    namespace: str = "default",
    release: str | None = None,
) -> HelmInstallPrompt:
    prompt = HelmInstallPrompt(chart, namespace=namespace, release=release)

    def _done(v: object) -> None:
        app.result = v

    await app.push_screen(prompt, _done)
    return prompt


async def _opened(app: HostApp, pilot: object) -> None:
    await until(pilot, lambda: bool(app.screen.query(Input)), label="prompt inputs composed")


async def test_install_prefills_release_version_namespace() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, namespace="web-ns")
        await _opened(app, pilot)
        assert app.screen.query_one("#helm-release", Input).value == "nginx"
        assert app.screen.query_one("#helm-version", Input).value == "18.1.0"
        assert app.screen.query_one("#helm-namespace", Input).value == "web-ns"
        assert app.screen.query_one("#helm-values", Select).value == VALUES_MODES[0]


async def test_upgrade_mode_fixes_release_and_namespace() -> None:
    """Upgrading targets one existing release: its name and namespace are
    facts of the row the user selected, not free-form wizard inputs."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, namespace="prod", release="web")
        await _opened(app, pilot)
        release = app.screen.query_one("#helm-release", Input)
        namespace = app.screen.query_one("#helm-namespace", Input)
        assert release.value == "web"
        assert release.disabled
        assert namespace.value == "prod"
        assert namespace.disabled


async def test_submit_returns_choices() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert app.result == HelmReleaseChoices(
            release="nginx", version="18.1.0", namespace="default", edit_values=False
        )


async def test_blank_release_blocks_submit() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-release", Input).value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_invalid_release_name_blocks_submit() -> None:
    """Helm requires DNS-compatible release names; reject locally with a
    message instead of a cryptic server-side failure after approval."""
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-release", Input).value = "Bad Name!"
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_blank_namespace_blocks_submit() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-namespace", Input).value = "  "
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_blank_version_means_latest() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-version", Input).value = ""
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)
        assert app.result.version == ""


async def test_edit_values_choice_flows_through() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)
        assert app.result.edit_values is True


async def test_escape_dismisses_with_none() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert app.result is None
