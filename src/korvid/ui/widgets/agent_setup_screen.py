"""AgentSetupScreen: in-TUI agent setup wizard (`:ai`, plan 4 slice 2)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from korvid.agent.setup import AgentConfigurator, AgentSettings

_PROVIDERS = ("github-copilot", "openai-compat", "azure", "ollama")

# provider -> (auth_method, default base_url, default model); azure asks for
# auth separately and requires base_url/model, so its defaults are empty.
_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "github-copilot": ("device-login", "", "gpt-4o"),
    "openai-compat": ("api_key", "https://api.openai.com/v1", "gpt-4o-mini"),
    "azure": ("", "", ""),
    "ollama": ("none", "http://localhost:11434/v1", "llama3"),
}


class AgentSetupScreen(ModalScreen[AgentSettings | None]):
    """Staged wizard: provider -> (azure auth) -> fields -> device login -> test."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("r", "retry", "Retry test", show=False),
    ]

    DEFAULT_CSS = """
    AgentSetupScreen {
        align: center middle;
    }
    AgentSetupScreen Vertical {
        width: 60;
        max-width: 90%;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    AgentSetupScreen OptionList {
        height: auto;
        max-height: 8;
    }
    AgentSetupScreen #setup-status {
        color: $warning;
    }
    """

    def __init__(
        self,
        configurator: AgentConfigurator,
        apply_settings: Callable[[AgentSettings], bool] | None = None,
    ) -> None:
        super().__init__()
        self._configurator = configurator
        self._apply_settings = apply_settings
        self._provider = ""
        self._auth_method = ""
        self._settings: AgentSettings | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Agent setup — pick a provider", id="setup-title")
            yield OptionList(*_PROVIDERS, id="setup-provider")
            yield OptionList("api_key", "entra", id="setup-auth")
            yield Input(id="setup-base-url", placeholder="base URL (empty for default)")
            yield Input(id="setup-model", placeholder="model")
            yield Input(id="setup-api-key-env", placeholder="env var holding the API key")
            yield Static(id="setup-device-code")
            yield Static(id="setup-status")

    def on_mount(self) -> None:
        for widget_id in (
            "#setup-auth",
            "#setup-base-url",
            "#setup-model",
            "#setup-api-key-env",
            "#setup-device-code",
        ):
            self.query_one(widget_id).display = False
        provider_list = self.query_one("#setup-provider", OptionList)
        provider_list.highlighted = 0
        provider_list.focus()

    # ------------------------------------------------------------------
    # Stage transitions
    # ------------------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        choice = str(event.option.prompt)
        if event.option_list.id == "setup-provider":
            self._provider = choice
            self.query_one("#setup-provider").display = False
            if choice == "azure":
                self.query_one("#setup-title", Static).update("Azure auth method")
                auth_list = self.query_one("#setup-auth", OptionList)
                auth_list.display = True
                auth_list.highlighted = 0
                auth_list.focus()
                return
            self._auth_method = _DEFAULTS[choice][0]
            self._show_fields()
        elif event.option_list.id == "setup-auth":
            self._auth_method = choice
            self.query_one("#setup-auth").display = False
            self._show_fields()

    def _show_fields(self) -> None:
        _, base_url, model = _DEFAULTS[self._provider]
        self.query_one("#setup-title", Static).update(f"{self._provider} — connection")
        base_input = self.query_one("#setup-base-url", Input)
        model_input = self.query_one("#setup-model", Input)
        base_input.value = base_url
        model_input.value = model
        base_input.display = True
        model_input.display = True
        if self._auth_method == "api_key":
            self.query_one("#setup-api-key-env").display = True
        base_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "setup-base-url":
            self.query_one("#setup-model", Input).focus()
        elif event.input.id == "setup-model":
            if self._auth_method == "api_key":
                self.query_one("#setup-api-key-env", Input).focus()
            else:
                self._advance()
        elif event.input.id == "setup-api-key-env":
            self._advance()

    def _advance(self) -> None:
        base_url = self.query_one("#setup-base-url", Input).value.strip() or None
        model = self.query_one("#setup-model", Input).value.strip()
        api_key_env = self.query_one("#setup-api-key-env", Input).value.strip() or None
        if not model:
            self._status("Model is required")
            return
        self._settings = AgentSettings(
            provider=self._provider,
            auth_method=self._auth_method,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
        )
        if self._provider == "github-copilot":
            self.run_worker(self._device_login(), exclusive=True)
        else:
            self.run_worker(self._probe(), exclusive=True)

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def _device_login(self) -> None:
        device = self.query_one("#setup-device-code", Static)
        try:
            prompt = await self._configurator.begin_device_login()
            device.display = True
            device.update(f"Enter code {prompt.user_code} at {prompt.verification_uri}")
            await self._configurator.finish_device_login()
        except Exception as exc:  # login errors must not crash the app
            self._status(f"Login failed: {exc}")
            return
        device.display = False
        await self._probe()

    async def _probe(self) -> None:
        settings = self._settings
        if settings is None:
            return
        self._status("Testing connection…")
        try:
            await self._configurator.test(settings)
        except Exception as exc:  # keep the wizard open on probe failure
            self._status(f"Test failed: {exc} — press r to retry, Esc to cancel")
            return
        if self._apply_settings is not None and not self._apply_settings(settings):
            # The app refused the swap (busy turn / rebuild failure): stay
            # open and do NOT persist, so a restart cannot silently activate
            # a configuration that never took effect.
            self._status("Apply failed — press r to retry, Esc to cancel")
            return
        try:
            await self._configurator.save(settings)
        except Exception as exc:  # keep the wizard open on save failure
            self._status(f"Save failed: {exc} — press r to retry, Esc to cancel")
            return
        self.dismiss(settings)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _status(self, text: str) -> None:
        self.query_one("#setup-status", Static).update(text)

    def action_retry(self) -> None:
        if self._settings is None:
            return
        # Re-read the still-visible inputs so an edit after a failed probe is
        # actually tested (device login is not repeated for Copilot).
        base_url = self.query_one("#setup-base-url", Input).value.strip() or None
        model = self.query_one("#setup-model", Input).value.strip()
        api_key_env = self.query_one("#setup-api-key-env", Input).value.strip() or None
        if not model:
            self._status("Model is required")
            return
        self._settings = dataclasses.replace(
            self._settings, base_url=base_url, model=model, api_key_env=api_key_env
        )
        self.run_worker(self._probe(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(None)
