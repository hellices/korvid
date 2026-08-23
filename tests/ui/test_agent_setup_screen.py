"""Tests for the :ai agent setup wizard (plan 4 slice 2, Task 7).

Flow (researched from opencode/crush/OpenClaw): provider -> auth ->
fetch models -> filterable model list (typed fallback) -> test -> save.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from textual.app import App
from textual.widgets import Input, OptionList, Static

from korvid.agent.setup import AgentConfigurator, AgentSettings, DeviceLoginPrompt
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen

from .waits import until


class FakeConfigurator(AgentConfigurator):
    def __init__(self, test_error: str | None = None, models: list[str] | None = None) -> None:
        self.calls: list[Any] = []
        self.test_error = test_error
        self.models: list[str] = models or []

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

    async def list_models(self, settings: AgentSettings) -> list[str]:
        self.calls.append(("list_models", settings))
        return list(self.models)

    async def save(self, settings: AgentSettings) -> None:
        self.calls.append(("save", settings))


class _Host(App[None]):
    def __init__(self, configurator: FakeConfigurator, current_profile: str | None = None) -> None:
        super().__init__()
        self.configurator = configurator
        self.current_profile = current_profile
        self.result: AgentSettings | str | None = "unset"

    def on_mount(self) -> None:
        def _done(res: AgentSettings | None) -> None:
            self.result = res

        self.push_screen(
            AgentSetupScreen(self.configurator, current_profile=self.current_profile),
            callback=_done,
        )


def _select(app: App[None], option_id: str, wanted: str) -> None:
    """Highlight the option whose id (or prompt) equals `wanted`."""
    ol = app.screen.query_one(option_id, OptionList)
    for i in range(ol.option_count):
        opt = ol.get_option_at_index(i)
        if opt.id == wanted or str(opt.prompt) == wanted:
            ol.highlighted = i
            return
    raise AssertionError(f"option {wanted!r} not in {option_id}")


def _kinds(cfg: FakeConfigurator) -> list[str]:
    return [c[0] if isinstance(c, tuple) else c for c in cfg.calls]


async def _pump(pilot: Any, n: int = 8) -> None:
    for _ in range(n):
        await pilot.pause()


async def test_ollama_path_tests_saves_and_dismisses() -> None:
    """No models from the API -> typed model input fallback with defaults."""
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")  # pick provider
        await pilot.press("enter")  # accept base_url default
        await _pump(pilot)  # fetch models (empty) -> fallback input
        await pilot.press("enter")  # accept model default
        await _pump(pilot)
        assert isinstance(app.result, AgentSettings)
        assert app.result.provider == "ollama"
        assert app.result.auth_method == "none"
        assert app.result.base_url == "http://localhost:11434"
        assert app.result.model == "llama3"
        assert _kinds(cfg) == ["list_models", "test", "save"]


async def test_model_list_offers_fetched_models() -> None:
    """Models returned by the API appear in a selectable list."""
    cfg = FakeConfigurator(models=["llama3", "mistral"])
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")  # base_url default
        await _pump(pilot)
        model_list = app.screen.query_one("#setup-model-list", OptionList)
        assert model_list.display is True
        _select(app, "#setup-model-list", "mistral")
        await pilot.press("enter")
        await _pump(pilot)
        assert isinstance(app.result, AgentSettings)
        assert app.result.model == "mistral"


async def test_model_list_typing_filters_options() -> None:
    cfg = FakeConfigurator(models=["gpt-4o", "gpt-4o-mini", "claude-sonnet-4"])
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await _pump(pilot)
        await pilot.press("c", "l", "a")  # filter input is focused
        await pilot.pause()
        model_list = app.screen.query_one("#setup-model-list", OptionList)
        prompts = [
            str(model_list.get_option_at_index(i).prompt) for i in range(model_list.option_count)
        ]
        assert prompts == ["claude-sonnet-4"]
        await pilot.press("enter")  # accept the single highlighted match
        await _pump(pilot)
        assert isinstance(app.result, AgentSettings)
        assert app.result.model == "claude-sonnet-4"


async def test_github_copilot_logs_in_then_lists_models() -> None:
    """Copilot: device login first, then models fetched with the new token."""

    class LoginThenModels(FakeConfigurator):
        async def finish_device_login(self) -> None:
            await super().finish_device_login()
            self.models = ["claude-sonnet-4", "gpt-4o"]

    cfg = LoginThenModels()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "github-copilot")
        await pilot.press("enter")
        await _pump(pilot)
        # login before any model/base_url question
        kinds = _kinds(cfg)
        assert kinds[: kinds.index("finish") + 1] == ["list_models", "begin", "finish"]
        _select(app, "#setup-model-list", "claude-sonnet-4")
        await pilot.press("enter")
        await _pump(pilot)
        assert isinstance(app.result, AgentSettings)
        assert app.result.provider == "github-copilot"
        assert app.result.auth_method == "device-login"
        assert app.result.base_url is None
        assert app.result.model == "claude-sonnet-4"


async def test_github_copilot_skips_login_when_already_authenticated() -> None:
    """If model listing already works, don't force a new device login."""
    cfg = FakeConfigurator(models=["gpt-4o"])
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "github-copilot")
        await pilot.press("enter")
        await _pump(pilot)
        assert "begin" not in cfg.calls
        model_list = app.screen.query_one("#setup-model-list", OptionList)
        assert model_list.display is True


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
        await _pump(pilot)
        device = app.screen.query_one("#setup-device-code", Static)
        text = str(device.render())
        assert "ABCD-1234" in text
        assert "github.com/login/device" in text


async def test_checklist_shows_completed_steps() -> None:
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.pause()
        steps = str(app.screen.query_one("#setup-steps", Static).render())
        assert "ollama" in steps
        assert "✓" in steps


async def test_probe_failure_keeps_screen_open_and_shows_error() -> None:
    cfg = FakeConfigurator(test_error="connection refused")
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await _pump(pilot)
        await pilot.press("enter")  # model default
        await _pump(pilot)
        assert app.result == "unset"  # not dismissed
        status = app.screen.query_one("#setup-status", Static)
        assert "connection refused" in str(status.render())
        assert "save" not in _kinds(cfg)


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
        await _pump(pilot)  # fetch models (empty) -> fallback input
        model = app.screen.query_one("#setup-model", Input)
        model.value = "gpt-4o"
        model.focus()
        await pilot.press("enter")
        await _pump(pilot)
        assert isinstance(app.result, AgentSettings)
        assert app.result.auth_method == "entra"
        assert app.result.model == "gpt-4o"


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
        await _pump(pilot)
        await pilot.press("enter")
        await _pump(pilot)
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
            self.result: AgentSettings | str | None = "unset"

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
        await _pump(pilot)
        await pilot.press("enter")
        await _pump(pilot)
        assert applied  # runtime swap happened before the failing save
        assert app.result == "unset"  # not dismissed
        text = str(app.screen.query_one("#setup-status", Static).render())
        assert "disk full" in text
        assert "applied" in text.lower()
        assert "revert" in text.lower()


async def test_retry_uses_edited_inputs() -> None:
    """After a failed probe, Ctrl+R must test the currently visible input
    values, not the snapshot captured on the original submission."""
    cfg = FakeConfigurator(test_error="boom")
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")
        await pilot.press("enter")
        await _pump(pilot)
        await pilot.press("enter")
        await _pump(pilot)
        cfg.test_error = None
        model_input = app.screen.query_one("#setup-model", Input)
        model_input.value = "edited-model"
        model_input.focus()
        # The real shortcut must work even while an Input is focused.
        await pilot.press("ctrl+r")
        await _pump(pilot)
        tested = [c[1] for c in cfg.calls if isinstance(c, tuple) and c[0] == "test"]
        assert tested[-1].model == "edited-model"


async def test_apply_failure_keeps_wizard_open_and_skips_save() -> None:
    """If the app cannot swap the runtime (busy turn / rebuild failure), the
    wizard must stay open and must not persist the new configuration."""
    cfg = FakeConfigurator()

    class ApplyHost(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.result: AgentSettings | str | None = "unset"

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
        await _pump(pilot)
        await pilot.press("enter")
        await _pump(pilot)
        assert app.result == "unset"  # not dismissed
        assert "save" not in _kinds(cfg)  # config untouched
        status = app.screen.query_one("#setup-status", Static)
        assert "Apply failed" in str(status.render())


async def test_ollama_provider_suggests_the_small_profile() -> None:
    """Local Ollama endpoints usually serve 3B-14B models: with no profile
    configured, the wizard saves the reduced capability profile for them
    (issue #71)."""
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")  # pick provider
        await pilot.press("enter")  # accept base_url default
        # No models from the API -> typed model input fallback.
        await until(
            pilot,
            lambda: app.screen.query_one("#setup-model", Input).display,
            label="model input shown",
        )
        await pilot.press("enter")  # accept model default
        # app.result starts as the "unset" sentinel, so wait for the actual
        # AgentSettings the dismiss callback produces — not merely non-None.
        await until(
            pilot,
            lambda: isinstance(app.result, AgentSettings),
            label="wizard result",
        )
        assert isinstance(app.result, AgentSettings)
        assert app.result.profile == "small"


async def test_cloud_providers_keep_the_full_profile() -> None:
    cfg = FakeConfigurator()
    app = _Host(cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, AgentSetupScreen)
        screen._provider = "openai-compat"
        screen._auth_method = "api_key"
        settings = screen._draft_settings("gpt-4o-mini")
        assert settings.profile == "full"


async def test_explicit_full_profile_survives_the_ollama_wizard() -> None:
    """`agent.profile: full` is a deliberate choice — reopening `:ai` for an
    Ollama endpoint must preserve it instead of silently overriding it with
    the `small` suggestion, which is only for an unset profile."""
    cfg = FakeConfigurator()
    app = _Host(cfg, current_profile="full")
    async with app.run_test() as pilot:
        await pilot.pause()
        _select(app, "#setup-provider", "ollama")
        await pilot.press("enter")  # pick provider
        await pilot.press("enter")  # accept base_url default
        await until(
            pilot,
            lambda: app.screen.query_one("#setup-model", Input).display,
            label="model input shown",
        )
        await pilot.press("enter")  # accept model default
        # app.result starts as the "unset" sentinel, so wait for the actual
        # AgentSettings the dismiss callback produces — not merely non-None.
        await until(
            pilot,
            lambda: isinstance(app.result, AgentSettings),
            label="wizard result",
        )
        assert isinstance(app.result, AgentSettings)
        assert app.result.profile == "full"


async def test_explicit_small_profile_survives_a_cloud_provider() -> None:
    """The preservation rule is symmetric: an explicit `small` is kept even
    when the wizard would otherwise draft `full` for a cloud provider."""
    cfg = FakeConfigurator()
    app = _Host(cfg, current_profile="small")
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, AgentSetupScreen)
        screen._provider = "openai-compat"
        screen._auth_method = "api_key"
        settings = screen._draft_settings("gpt-4o-mini")
        assert settings.profile == "small"


class _HostWithSettings(App[None]):
    def __init__(self, configurator: FakeConfigurator, current_settings: AgentSettings) -> None:
        super().__init__()
        self.configurator = configurator
        self.current_settings = current_settings

    def on_mount(self) -> None:
        self.push_screen(
            AgentSetupScreen(self.configurator, current_settings=self.current_settings)
        )


async def test_reconnect_prefills_azure_auth_method() -> None:
    """Azure + Entra kept settings must pre-highlight the auth choice: a
    confirm-through reconnect must not silently switch to api_key
    (review on #180)."""
    settings = AgentSettings(
        provider="azure",
        auth_method="entra",
        base_url="https://my.openai.azure.com",
        model="gpt-4o",
    )
    app = _HostWithSettings(FakeConfigurator(), settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        provider_list = app.screen.query_one("#setup-provider", OptionList)
        assert provider_list.highlighted is not None
        assert provider_list.get_option_at_index(provider_list.highlighted).id == "azure"
        await pilot.press("enter")  # accept azure → auth step
        auth_list = app.screen.query_one("#setup-auth", OptionList)
        assert auth_list.display is True
        assert auth_list.highlighted is not None
        assert str(auth_list.get_option_at_index(auth_list.highlighted).prompt) == "entra"


async def test_reconnect_normalizes_registry_provider_aliases() -> None:
    """Settings configured with a registry alias (openai, vllm, github,
    anthropic, claude) must map onto the wizard's openai-compat entry and
    still prefill the endpoint (review on #180)."""
    settings = AgentSettings(
        provider="openai",
        auth_method="api_key",
        base_url="https://api.my-proxy.example/v1",
        model="gpt-4o-mini",
        api_key_env="MY_KEY",
    )
    app = _HostWithSettings(FakeConfigurator(), settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        provider_list = app.screen.query_one("#setup-provider", OptionList)
        assert provider_list.highlighted is not None
        assert provider_list.get_option_at_index(provider_list.highlighted).id == "openai-compat"
        await pilot.press("enter")  # accept openai-compat → endpoint step
        base = app.screen.query_one("#setup-base-url", Input)
        assert base.value == "https://api.my-proxy.example/v1"


async def test_reconnect_preserves_a_no_auth_method() -> None:
    """A no-auth OpenAI-compatible endpoint (e.g. local vLLM) must keep
    auth_method='none' on confirm-through — never reset to api_key and
    prompt for a nonexistent key env (review on #180)."""
    settings = AgentSettings(
        provider="vllm",
        auth_method="none",
        base_url="http://localhost:8000/v1",
        model="qwen",
    )
    app = _HostWithSettings(FakeConfigurator(), settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # accept openai-compat (alias-normalized)
        await pilot.press("enter")  # accept the kept endpoint
        await _pump(pilot)
        screen = app.screen
        assert isinstance(screen, AgentSetupScreen)
        assert screen._auth_method == "none"
        env_input = screen.query_one("#setup-api-key-env", Input)
        assert env_input.display is False  # never asked for a key env


def test_agent_settings_options_are_copy_safe_and_immutable() -> None:
    nested = {"region": "apac"}
    models = ["llama3"]
    source: dict[str, object] = {
        "tenant": "platform",
        "nested": nested,
        "models": models,
    }
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434",
        model="llama3",
        options=source,
    )

    source["tenant"] = "mutated"
    nested["region"] = "emea"
    models.append("qwen3")

    assert dict(settings.options) == {
        "tenant": "platform",
        "nested": {"region": "apac"},
        "models": ("llama3",),
    }
    assert isinstance(settings.options, MappingProxyType)
    assert isinstance(settings.options["nested"], MappingProxyType)
    with pytest.raises(TypeError, match="mappingproxy"):
        settings.options["new"] = "value"  # type: ignore[index]  # immutability is the test
    with pytest.raises(TypeError, match="mappingproxy"):
        settings.options["nested"]["region"] = "emea"  # type: ignore[index]  # immutability is the test


def test_agent_settings_deep_freezes_tuple_elements() -> None:
    """Finding #4: tuple elements containing mutable dicts must become
    mapping proxies; nested mutation must be rejected."""
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434",
        model="llama3",
        options={"items": ({"key": "val"}, {"nested": {"deep": True}})},
    )
    items = settings.options["items"]
    assert isinstance(items, tuple)
    assert isinstance(items[0], MappingProxyType)
    assert isinstance(items[1], MappingProxyType)
    assert isinstance(items[1]["nested"], MappingProxyType)
    with pytest.raises(TypeError, match="mappingproxy"):
        items[0]["key"] = "mutated"  # type: ignore[index]  # immutability is the test
    with pytest.raises(TypeError, match="mappingproxy"):
        items[1]["nested"]["deep"] = False  # type: ignore[index]  # immutability is the test


async def test_reconnect_preserves_current_options_in_drafted_settings() -> None:
    settings = AgentSettings(
        provider="openai",
        auth_method="api_key",
        base_url="https://api.my-proxy.example/v1",
        model="gpt-4o-mini",
        api_key_env="MY_KEY",
        options={
            "tenant": "platform",
            "features": {"region": "apac"},
            "fallbacks": ["gpt-4o-mini"],
        },
    )
    app = _HostWithSettings(FakeConfigurator(), settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, AgentSetupScreen)
        screen._provider = "openai-compat"
        screen._auth_method = "api_key"
        screen._base_url = "https://api.my-proxy.example/v1"
        screen._api_key_env = "MY_KEY"
        drafted = screen._draft_settings("gpt-4o")
        assert dict(drafted.options) == {
            "tenant": "platform",
            "features": {"region": "apac"},
            "fallbacks": ("gpt-4o-mini",),
        }
