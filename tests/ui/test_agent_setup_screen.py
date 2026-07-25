"""Tests for the :ai agent setup wizard (plan 4 slice 2, Task 7)."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.widgets import Input, OptionList, Static

from korvid.agent.setup import AgentConfigurator, AgentSettings, DeviceLoginPrompt
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen


class FakeConfigurator(AgentConfigurator):
    def __init__(self, test_error: str | None = None) -> None:
        self.calls: list[Any] = []
        self.test_error = test_error

    async def begin_device_login(self) -> DeviceLoginPrompt:
        self.calls.append("begin")
        return DeviceLoginPrompt("ABCD-1234", "https://github.com/login/device")

    async def finish_device_login(self) -> None:
        self.calls.append("finish")

    async def test(self, settings: AgentSettings) -> str:
        self.calls.append(("test", settings))
        if self.test_error:
            raise RuntimeError(self.test_error)
        return "ok"

    async def save(self, settings: AgentSettings) -> None:
        self.calls.append(("save", settings))


class _Host(App[None]):
    def __init__(self, configurator: FakeConfigurator) -> None:
        super().__init__()
        self.configurator = configurator
        self.result: AgentSettings | None | str = "unset"

    def on_mount(self) -> None:
        def _done(res: AgentSettings | None) -> None:
            self.result = res

        self.push_screen(AgentSetupScreen(self.configurator), callback=_done)


def _select(app: App[None], option_id: str, prompt: str) -> None:
    ol = app.screen.query_one(option_id, OptionList)
    for i in range(ol.option_count):
        if str(ol.get_option_at_index(i).prompt) == prompt:
            ol.highlighted = i
            return
    raise AssertionError(f"option {prompt!r} not in {option_id}")


async def test_ollama_path_tests_saves_and_dismisses() -> None:
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")  # pick provider
        await pilot.press("enter")  # accept base_url default
        await pilot.press("enter")  # accept model default
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.result, AgentSettings)
        assert app.result.provider == "ollama"
        assert app.result.auth_method == "none"
        assert app.result.base_url == "http://localhost:11434/v1"
        assert app.result.model == "llama3"
        kinds = [c[0] if isinstance(c, tuple) else c for c in cfg.calls]
        assert kinds == ["test", "save"]


async def test_github_copilot_path_runs_device_login() -> None:
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "github-copilot")
        await pilot.press("enter")
        await pilot.press("enter")  # base_url (empty -> None)
        await pilot.press("enter")  # model default gpt-4o
        for _ in range(8):
            await pilot.pause()
        assert "begin" in cfg.calls
        assert "finish" in cfg.calls
        assert isinstance(app.result, AgentSettings)
        assert app.result.provider == "github-copilot"
        assert app.result.auth_method == "device-login"
        assert app.result.base_url is None


async def test_device_code_shown_during_login() -> None:
    class SlowConfigurator(FakeConfigurator):
        async def finish_device_login(self) -> None:
            import asyncio

            self.calls.append("finish")
            await asyncio.Event().wait()  # never completes

    cfg = SlowConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "github-copilot")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        device = app.screen.query_one("#setup-device-code", Static)
        text = str(device.render())
        assert "ABCD-1234" in text
        assert "github.com/login/device" in text


async def test_probe_failure_keeps_screen_open_and_shows_error() -> None:
    cfg = FakeConfigurator(test_error="connection refused")
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert app.result == "unset"  # not dismissed
        status = app.screen.query_one("#setup-status", Static)
        assert "connection refused" in str(status.render())
        kinds = [c[0] if isinstance(c, tuple) else c for c in cfg.calls]
        assert "save" not in kinds


async def test_azure_offers_auth_choice() -> None:
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "azure")
        await pilot.press("enter")
        await pilot.pause()
        auth = app.screen.query_one("#setup-auth", OptionList)
        assert auth.display is True
        _select(app, "#setup-auth", "entra")
        await pilot.press("enter")
        await pilot.pause()
        inp = app.screen.query_one("#setup-base-url", Input)
        assert inp.display is True
        inp.value = "https://foo.openai.azure.com/openai/v1"
        await pilot.press("enter")
        model = app.screen.query_one("#setup-model", Input)
        model.value = "gpt-4o"
        model.focus()
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.result, AgentSettings)
        assert app.result.auth_method == "entra"


async def test_escape_dismisses_none() -> None:
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None
        assert cfg.calls == []


async def test_save_failure_shows_error_and_keeps_screen_open() -> None:
    class SaveFailConfigurator(FakeConfigurator):
        async def save(self, settings: AgentSettings) -> None:
            raise RuntimeError("disk full")

    cfg = SaveFailConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert app.result == "unset"  # not dismissed
        status = app.screen.query_one("#setup-status", Static)
        assert "disk full" in str(status.render())


async def test_save_failure_after_apply_warns_about_restart_revert() -> None:
    """When the runtime swap already succeeded, a save failure must tell the
    user the settings are active now but will revert on restart."""

    class SaveFailConfigurator(FakeConfigurator):
        async def save(self, settings: AgentSettings) -> None:
            raise RuntimeError("disk full")

    cfg = SaveFailConfigurator()
    applied: list[AgentSettings] = []

    # NB: not a _Host subclass — Textual dispatches on_mount once per class in
    # the MRO, so subclassing would push two screens.
    class _ApplyHost(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.result: AgentSettings | None | str = "unset"

        def on_mount(self) -> None:
            def _done(res: AgentSettings | None) -> None:
                self.result = res

            def _apply(settings: AgentSettings) -> bool:
                applied.append(settings)
                return True

            self.push_screen(
                AgentSetupScreen(cfg, apply_settings=_apply),
                callback=_done,
            )

    app = _ApplyHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert applied  # runtime swap happened before the failing save
        assert app.result == "unset"  # not dismissed
        text = str(app.screen.query_one("#setup-status", Static).render())
        assert "disk full" in text
        assert "applied" in text.lower()
        assert "revert" in text.lower()


async def test_retry_uses_edited_inputs() -> None:
    """After a failed probe, `r` must test the currently visible input values,
    not the snapshot captured on the original submission."""
    cfg = FakeConfigurator(test_error="boom")
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        cfg.test_error = None
        model_input = app.screen.query_one("#setup-model", Input)
        model_input.value = "edited-model"
        model_input.focus()
        # The real shortcut must work even while an Input is focused.
        await pilot.press("ctrl+r")
        for _ in range(6):
            await pilot.pause()
        tested = [c[1] for c in cfg.calls if isinstance(c, tuple) and c[0] == "test"]
        assert tested[-1].model == "edited-model"


async def test_apply_failure_keeps_wizard_open_and_skips_save() -> None:
    """If the app cannot swap the runtime (busy turn / rebuild failure), the
    wizard must stay open and must not persist the new configuration."""
    cfg = FakeConfigurator()

    class ApplyHost(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.result: AgentSettings | None | str = "unset"

        def on_mount(self) -> None:
            def _done(res: AgentSettings | None) -> None:
                self.result = res

            self.push_screen(AgentSetupScreen(cfg, apply_settings=lambda s: False), callback=_done)

    app = ApplyHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert app.result == "unset"  # not dismissed
        assert not any(
            isinstance(c, tuple) and c[0] == "save" for c in cfg.calls
        )  # config untouched
        status = app.screen.query_one("#setup-status", Static)
        assert "Apply failed" in str(status.render())
