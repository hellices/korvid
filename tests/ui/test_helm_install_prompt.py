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


async def test_dotted_release_name_is_accepted() -> None:
    """Release names are DNS-1123 subdomains: dots are legal (`team.web`),
    so upgrades of such releases must not be rejected by local validation."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-release", Input).value = "team.web"
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)
        assert app.result.release == "team.web"


async def test_overlong_release_name_blocks_submit() -> None:
    """Helm caps release names at 53 characters."""
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-release", Input).value = "a" * 54
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_release_name_at_53_chars_is_accepted() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-release", Input).value = "a" * 53
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)


async def test_dotted_namespace_blocks_submit() -> None:
    """Namespaces are DNS-1123 labels: unlike release names, no dots."""
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-namespace", Input).value = "team.web"
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_overlong_namespace_blocks_submit() -> None:
    """Namespaces cap at 63 characters (DNS-1123 label limit)."""
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-namespace", Input).value = "n" * 64
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        assert app.screen is prompt


async def test_namespace_at_63_chars_is_accepted() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        app.screen.query_one("#helm-namespace", Input).value = "n" * 63
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)


async def test_upgrade_mode_defaults_to_reusing_current_values() -> None:
    """`helm upgrade` resets custom values to chart defaults unless
    `--reuse-values` is passed: the wizard's safe default for an existing
    release is to keep its current overrides."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, namespace="prod", release="web")
        await _opened(app, pilot)
        assert app.screen.query_one("#helm-values", Select).value == "reuse current values"
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)
        assert app.result.reuse_values is True
        assert app.result.edit_values is False


async def test_install_mode_offers_no_reuse_option() -> None:
    """A fresh install has no current values to reuse."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app)
        await _opened(app, pilot)
        select = app.screen.query_one("#helm-values", Select)
        assert select.value == VALUES_MODES[0]
        labels = [str(prompt) for prompt, _ in select._options]
        assert "reuse current values" not in labels
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="prompt dismissed")
        assert isinstance(app.result, HelmReleaseChoices)
        assert app.result.reuse_values is False


# ---------------------------------------------------------------------------
# Required values + README (issue #151): chart metadata surfaces in the wizard
# ---------------------------------------------------------------------------

_SCHEMA: "dict[str, object]" = {
    "required": ["mode"],
    "properties": {
        "mode": {"type": "string", "enum": ["daemonset", "deployment", "statefulset", ""]},
    },
}


async def _open_with_info(
    app: HostApp,
    *,
    schema: "dict[str, object] | None" = _SCHEMA,
    readme: str = "# Chart README\nSet mode before installing.",
    schema_error: bool = False,
    schema_calls: "list[str] | None" = None,
) -> HelmInstallPrompt:
    async def get_schema(chart: str, version: str) -> "dict[str, object] | None":
        if schema_calls is not None:
            schema_calls.append(version)
        if schema_error:
            raise RuntimeError("boom")
        return schema

    async def get_readme(chart: str, version: str) -> str:
        return readme

    prompt = HelmInstallPrompt(
        _CHART,
        namespace="default",
        release=None,
        get_schema=get_schema,
        get_readme=get_readme,
    )

    def _done(v: object) -> None:
        app.result = v

    await app.push_screen(prompt, _done)
    return prompt


async def test_required_values_from_the_schema_render_in_the_wizard() -> None:
    """A chart shipping values.schema.json gets a Required values section:
    field path plus its enum/type - the pre-install answer to 'what must I
    set?' (issue #151)."""
    app = HostApp()
    async with app.run_test() as pilot:
        await _open_with_info(app)
        await _opened(app, pilot)
        await until(
            pilot,
            lambda: "mode" in str(app.screen.query_one("#helm-required", Static).render()),
            label="required section rendered",
        )
        text = str(app.screen.query_one("#helm-required", Static).render())
        assert "daemonset" in text  # the enum names the valid choices


async def test_wizard_without_schema_shows_no_required_section() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open_with_info(app, schema=None)
        await _opened(app, pilot)
        await until(
            pilot,
            lambda: not app.screen.query_one("#helm-required", Static).display,
            label="required section hidden",
        )


async def test_schema_fetch_failure_degrades_silently() -> None:
    """The schema is advisory: a fetch failure must not break the wizard or
    block submitting."""
    app = HostApp()
    calls: list[str] = []
    async with app.run_test() as pilot:
        await _open_with_info(app, schema_error=True, schema_calls=calls)
        await _opened(app, pilot)
        # The failing provider must actually have been invoked - only then
        # does the submit below prove the failure did not break anything.
        await until(pilot, lambda: bool(calls), label="schema provider invoked")
        app.screen.query_one("#helm-release", Input).focus()
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="submitted")
        assert isinstance(app.result, HelmReleaseChoices)


async def test_version_change_reloads_the_required_values() -> None:
    """The version field stays editable after mount: the Required values
    section must describe the version the install will actually use, so a
    version edit clears it and refetches the schema for the new version."""
    app = HostApp()
    calls: list[str] = []
    async with app.run_test() as pilot:
        await _open_with_info(app, schema_calls=calls)
        await _opened(app, pilot)
        await until(
            pilot,
            lambda: app.screen.query_one("#helm-required", Static).display,
            label="initial section rendered",
        )
        assert calls == ["18.1.0"]
        version = app.screen.query_one("#helm-version", Input)
        version.focus()
        version.value = "18.2.0"
        # the stale section is hidden immediately, then refetched (debounced)
        await until(
            pilot,
            lambda: not app.screen.query_one("#helm-required", Static).display,
            label="stale section cleared",
        )
        await until(pilot, lambda: "18.2.0" in calls, label="schema refetched")
        await until(
            pilot,
            lambda: app.screen.query_one("#helm-required", Static).display,
            label="section back for the new version",
        )


async def test_f1_opens_the_chart_readme() -> None:
    """The chart's README opens in a scrollable modal from the wizard
    (issue #151) - prerequisites and mandatory settings without leaving."""
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open_with_info(app)
        await _opened(app, pilot)
        await pilot.press("f1")
        await until(pilot, lambda: app.screen is not prompt, label="readme screen open")
        from textual.widgets import Static as _Static

        body = " ".join(str(w.render()) for w in app.screen.query(_Static))
        assert "Set mode before installing" in body
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is prompt, label="back to wizard")


async def test_wizard_without_info_providers_behaves_as_before() -> None:
    """No injected providers (degraded session): the wizard renders and
    submits exactly as before - no required section, README key inert."""
    app = HostApp()
    async with app.run_test() as pilot:
        prompt = await _open(app)
        await _opened(app, pilot)
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen is prompt  # no README to show, key does nothing
        app.screen.query_one("#helm-release", Input).focus()
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="submitted")


async def test_in_flight_stale_schema_cannot_resurface_during_the_debounce() -> None:
    """A version edit hides the section and debounces the refetch - but the
    *previous* version's still-in-flight fetch must not complete during
    that window and re-display stale required values."""
    import asyncio

    gate = asyncio.Event()
    calls: list[str] = []
    completed: list[str] = []

    async def gated_schema(chart: str, version: str) -> "dict[str, object] | None":
        calls.append(version)
        await gate.wait()
        completed.append(version)
        return _SCHEMA

    app = HostApp()
    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_schema=gated_schema)

    def _done(v: object) -> None:
        app.result = v

    async with app.run_test() as pilot:
        await app.push_screen(prompt, _done)
        await _opened(app, pilot)
        await until(pilot, lambda: bool(calls), label="initial fetch started")
        version = app.screen.query_one("#helm-version", Input)
        version.focus()
        version.value = "18.2.0"  # edit while the mount fetch is in flight
        # the edit was handled (debounce armed) before the stale fetch lands
        await until(
            pilot,
            lambda: prompt._schema_debounce is not None,
            label="edit handled, debounce armed",
        )
        gate.set()  # the stale fetch completes inside the debounce window
        # observable state, not wall-clock: the *stale* fetch has finished
        # (its worker resumed past the gate) and the section stayed hidden.
        await until(pilot, lambda: "18.1.0" in completed, label="stale fetch completed")
        assert not app.screen.query_one("#helm-required", Static).display
        # the debounced refetch eventually renders the new version's schema
        await until(pilot, lambda: "18.2.0" in completed, label="refetched")
        await until(
            pilot,
            lambda: app.screen.query_one("#helm-required", Static).display,
            label="fresh section rendered",
        )


async def test_rapid_f1_presses_open_a_single_readme_screen() -> None:
    """Two F1 presses while a slow `helm show readme` is in flight must not
    stack two README modals - one Escape must land back on the wizard."""
    import asyncio

    gate = asyncio.Event()

    async def slow_readme(chart: str, version: str) -> str:
        await gate.wait()
        return "# README"

    app = HostApp()
    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_readme=slow_readme)

    def _done(v: object) -> None:
        app.result = v

    async with app.run_test() as pilot:
        await app.push_screen(prompt, _done)
        await _opened(app, pilot)
        await pilot.press("f1")
        await pilot.press("f1")  # second press while the first fetch hangs
        gate.set()
        await until(pilot, lambda: app.screen is not prompt, label="readme open")
        # every README worker has finished: a late duplicate cannot be
        # in flight anymore when the stack is inspected.
        await until(
            pilot,
            lambda: (
                not any(
                    w.group == "helm-chart-readme" and not w.is_finished for w in prompt.workers
                )
            ),
            label="readme workers finished",
        )
        from korvid.ui.widgets.helm_install import ChartReadmeScreen

        stacked = [s for s in app.screen_stack if isinstance(s, ChartReadmeScreen)]
        assert len(stacked) == 1
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is prompt, label="back on the wizard")


async def test_readme_for_a_stale_version_is_discarded() -> None:
    """The version field stays editable while `helm show readme` runs: a
    fetch finishing for an old version must not push documentation over a
    wizard now configured for a different one."""
    import asyncio

    gate = asyncio.Event()
    completed: list[str] = []

    async def slow_readme(chart: str, version: str) -> str:
        await gate.wait()
        completed.append(version)
        return f"# README {version}"

    app = HostApp()
    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_readme=slow_readme)

    def _done(v: object) -> None:
        app.result = v

    async with app.run_test() as pilot:
        await app.push_screen(prompt, _done)
        await _opened(app, pilot)
        await pilot.press("f1")  # fetch starts for 18.1.0
        version = app.screen.query_one("#helm-version", Input)
        version.value = "18.2.0"  # edited while the fetch hangs
        gate.set()
        await until(pilot, lambda: bool(completed), label="stale fetch completed")
        await until(
            pilot,
            lambda: (
                not any(
                    w.group == "helm-chart-readme" and not w.is_finished for w in prompt.workers
                )
            ),
            label="readme worker finished",
        )
        from korvid.ui.widgets.helm_install import ChartReadmeScreen

        assert not any(isinstance(s, ChartReadmeScreen) for s in app.screen_stack)


async def test_schema_fetch_landing_without_the_section_does_not_raise() -> None:
    """The schema section is advisory: losing the widget must not crash.

    `on_mount` starts the fetch, and the worker resumes after an await into a
    tree that may only be partly composed - `#helm-version` mounted, the
    `#helm-required` section not yet. Querying it unguarded raises `NoMatches`
    inside the worker, which Textual surfaces as `WorkerFailed`; observed on a
    Windows CI run of the full UI suite, where it failed
    `test_install_render_failure_edit_values_and_retry`.
    """

    async def schema(chart: str, version: str) -> "dict[str, object] | None":
        return _SCHEMA

    app = HostApp()
    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_schema=schema)

    def _done(v: object) -> None:
        app.result = v

    async with app.run_test() as pilot:
        await app.push_screen(prompt, _done)
        await _opened(app, pilot)
        # Stand in for the section not being composed yet when the fetch lands.
        await prompt.query_one("#helm-required", Static).remove()

        await prompt._load_required_values(prompt._schema_seq)


async def test_schema_fetch_without_the_version_field_does_not_raise() -> None:
    """The same worker reads `#helm-version` before its await.

    `on_mount` starts it, so that read shares the partly-composed window with
    the section read below it - the crash just lands a few lines earlier.
    """

    async def schema(chart: str, version: str) -> "dict[str, object] | None":
        return _SCHEMA

    app = HostApp()
    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_schema=schema)

    def _done(v: object) -> None:
        app.result = v

    async with app.run_test() as pilot:
        await app.push_screen(prompt, _done)
        await _opened(app, pilot)
        await prompt.query_one("#helm-version", Input).remove()

        await prompt._load_required_values(prompt._schema_seq)


async def test_starting_the_schema_load_before_compose_does_not_raise() -> None:
    """`on_mount` starts the load synchronously.

    Textual does not guarantee a screen's composed children are queryable by
    the time `on_mount` runs, and this read is not inside the worker, so losing
    the race raises straight out of `on_mount` and takes the app down rather
    than merely failing a worker.
    """

    async def schema(chart: str, version: str) -> "dict[str, object] | None":
        return _SCHEMA

    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_schema=schema)

    prompt._start_schema_load()

    assert prompt._schema_version_requested is None, "no version can be read before compose"


async def test_version_change_without_the_section_does_not_raise() -> None:
    """`Input.Changed` fires at mount time for the prefilled version.

    The early return that absorbs that echo relies on the mount-time fetch
    having recorded the version, which cannot happen if the field was not
    composed yet - so the handler can reach the section hide with the section
    still missing.
    """

    async def schema(chart: str, version: str) -> "dict[str, object] | None":
        return _SCHEMA

    app = HostApp()
    prompt = HelmInstallPrompt(_CHART, namespace="default", release=None, get_schema=schema)

    def _done(v: object) -> None:
        app.result = v

    async with app.run_test() as pilot:
        await app.push_screen(prompt, _done)
        await _opened(app, pilot)
        await prompt.query_one("#helm-required", Static).remove()
        prompt._schema_version_requested = None  # the mount-time fetch never described one

        prompt.query_one("#helm-version", Input).value = "19.0.0"
        await until(pilot, lambda: prompt._schema_debounce is not None, label="debounce armed")
