"""AgentSetupScreen: in-TUI agent setup wizard (`:ai`, plan 4 slice 2).

Flow modelled on opencode/crush onboarding research: provider -> auth ->
fetch models from the API -> filterable model list (typed input fallback)
-> live connection test -> save. Completed steps stay visible as a
checklist so the wizard feels conversational rather than form-like.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.agent.setup import AgentConfigurator, AgentSettings

# provider -> (auth_method, default base_url, default model); azure asks for
# auth separately and requires base_url/model, so its defaults are empty.
_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "github-copilot": ("device-login", "", "gpt-4o"),
    "openai-compat": ("api_key", "https://api.openai.com/v1", "gpt-4o-mini"),
    "azure": ("", "", ""),
    "ollama": ("none", "http://localhost:11434", "llama3"),
}

_PROVIDER_LABELS: dict[str, str] = {
    "github-copilot": "github-copilot — sign in with GitHub (no API key)",
    "openai-compat": "openai-compat — OpenAI-compatible API (API key)",
    "azure": "azure — Azure OpenAI (Entra ID or API key)",
    "ollama": "ollama — local models, native API (no auth)",
}

# Registry aliases (providers/registry.py) that all resolve to the wizard's
# openai-compat entry: settings configured under an alias must still
# pre-highlight and prefill the wizard on reconnect (issue #167).
_OPENAI_COMPAT_ALIASES = frozenset({"openai", "vllm", "github", "anthropic", "claude"})


def _canonical_provider(name: str | None) -> str | None:
    """The wizard entry a configured provider name maps onto, or None."""
    if not name:
        return None
    lowered = name.strip().lower()
    if lowered in _DEFAULTS:
        return lowered
    if lowered in _OPENAI_COMPAT_ALIASES:
        return "openai-compat"
    return None


class AgentSetupScreen(ModalScreen["AgentSettings | None"]):
    """Conversational wizard: one question at a time + completed-step checklist."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+r", "retry", "Retry test", show=False),
    ]

    DEFAULT_CSS = """
    AgentSetupScreen {
        align: center middle;
    }
    AgentSetupScreen Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    AgentSetupScreen OptionList {
        height: auto;
        max-height: 10;
    }
    AgentSetupScreen #setup-steps {
        color: $success;
    }
    AgentSetupScreen #setup-status {
        color: $warning;
    }
    """

    def __init__(
        self,
        configurator: AgentConfigurator,
        apply_settings: Callable[[AgentSettings], bool] | None = None,
        current_profile: str | None = None,
        current_settings: AgentSettings | None = None,
    ) -> None:
        super().__init__()
        self._configurator = configurator
        self._apply_settings = apply_settings
        # Explicitly configured capability profile (None = unset): an
        # explicit choice is preserved; only an unset profile receives the
        # Ollama `small` suggestion (issue #71).
        self._current_profile = current_profile
        # Kept settings from a configured (possibly :ai off'd) agent
        # (issue #167): the wizard starts from them so reconnecting is
        # confirm-through, not re-entry. Registry aliases normalize onto
        # the wizard's canonical entries for highlighting/prefilling.
        self._current_settings = current_settings
        self._current_canonical = _canonical_provider(
            current_settings.provider if current_settings is not None else None
        )
        self._provider = ""
        self._auth_method = ""
        self._base_url: str | None = None
        self._api_key_env: str | None = None
        self._models: list[str] = []
        self._settings: AgentSettings | None = None
        self._done_steps: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="setup-steps")
            yield Static("Which AI provider would you like to use?", id="setup-title")
            yield OptionList(
                *(Option(_PROVIDER_LABELS[p], id=p) for p in _DEFAULTS),
                id="setup-provider",
            )
            yield OptionList("api_key", "entra", id="setup-auth")
            yield Input(id="setup-base-url", placeholder="base URL (empty for default)")
            yield Input(id="setup-api-key-env", placeholder="env var holding the API key")
            yield Input(id="setup-model-filter", placeholder="type to filter — Enter to select")
            yield OptionList(id="setup-model-list")
            yield Input(id="setup-model", placeholder="model")
            yield Static(id="setup-device-code")
            yield Static(id="setup-status")

    def on_mount(self) -> None:
        for widget_id in (
            "#setup-auth",
            "#setup-base-url",
            "#setup-api-key-env",
            "#setup-model-filter",
            "#setup-model-list",
            "#setup-model",
            "#setup-device-code",
        ):
            self.query_one(widget_id).display = False
        provider_list = self.query_one("#setup-provider", OptionList)
        provider_list.highlighted = 0
        if self._current_canonical is not None:
            provider_list.highlighted = list(_DEFAULTS).index(self._current_canonical)
        provider_list.focus()

    # ------------------------------------------------------------------
    # Checklist / status helpers
    # ------------------------------------------------------------------

    def _mark_done(self, step: str) -> None:
        self._done_steps.append(step)
        lines = "\n".join(f"✓ {s}" for s in self._done_steps)
        self.query_one("#setup-steps", Static).update(lines)

    def _ask(self, question: str) -> None:
        self.query_one("#setup-title", Static).update(question)

    def _status(self, text: str) -> None:
        self.query_one("#setup-status", Static).update(text)

    # ------------------------------------------------------------------
    # Stage transitions
    # ------------------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == "setup-provider":
            self._provider = event.option.id or str(event.option.prompt)
            self.query_one("#setup-provider").display = False
            self._mark_done(f"Provider: {self._provider}")
            if self._provider == "azure":
                self._ask("How should korvid authenticate with Azure?")
                auth_list = self.query_one("#setup-auth", OptionList)
                auth_list.display = True
                auth_list.highlighted = 0
                current = self._current_settings
                if (
                    self._current_canonical == "azure"
                    and current is not None
                    and current.auth_method == "entra"
                ):
                    # Confirm-through reconnect must not silently switch
                    # the retained Entra flow to api_key (issue #167).
                    auth_list.highlighted = 1
                auth_list.focus()
                return
            self._auth_method = _DEFAULTS[self._provider][0]
            current = self._current_settings
            if (
                current is not None
                and self._current_canonical == self._provider
                and current.auth_method
            ):
                # Confirm-through reconnect keeps the retained auth method:
                # a no-auth endpoint (local vLLM) must not be reset to
                # api_key and prompted for a nonexistent key env.
                self._auth_method = current.auth_method
            self._after_auth_method()
        elif event.option_list.id == "setup-auth":
            self._auth_method = str(event.option.prompt)
            self.query_one("#setup-auth").display = False
            self._mark_done(f"Auth: {self._auth_method}")
            self._after_auth_method()
        elif event.option_list.id == "setup-model-list":
            self._choose_model(str(event.option.prompt))

    def _after_auth_method(self) -> None:
        if self._provider == "github-copilot":
            self.run_worker(self._copilot_connect(), exclusive=True)
            return
        _, base_url, _ = _DEFAULTS[self._provider]
        current = self._current_settings
        if current is not None and self._current_canonical == self._provider and current.base_url:
            # Same provider as the kept settings: start from the kept
            # endpoint, not the provider default (issue #167 reconnect).
            base_url = current.base_url
        self._ask(f"Where is your {self._provider} endpoint?")
        base_input = self.query_one("#setup-base-url", Input)
        base_input.value = base_url
        base_input.display = True
        base_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "setup-base-url":
            self._base_url = event.input.value.strip() or None
            self._mark_done(f"Endpoint: {self._base_url or 'default'}")
            if self._auth_method == "api_key":
                self._ask("Which environment variable holds your API key?")
                env_input = self.query_one("#setup-api-key-env", Input)
                current = self._current_settings
                if (
                    current is not None
                    and self._current_canonical == self._provider
                    and current.api_key_env
                ):
                    env_input.value = current.api_key_env
                env_input.display = True
                env_input.focus()
            else:
                self.run_worker(self._fetch_models(), exclusive=True)
        elif event.input.id == "setup-api-key-env":
            self._api_key_env = event.input.value.strip() or None
            self._mark_done(f"API key env: {self._api_key_env or '(none)'}")
            self.run_worker(self._fetch_models(), exclusive=True)
        elif event.input.id == "setup-model-filter":
            model_list = self.query_one("#setup-model-list", OptionList)
            if model_list.highlighted is not None and model_list.option_count:
                option = model_list.get_option_at_index(model_list.highlighted)
                self._choose_model(str(option.prompt))
        elif event.input.id == "setup-model":
            model = event.input.value.strip()
            if not model:
                self._status("Model is required")
                return
            self._choose_model(model)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "setup-model-filter":
            return
        event.stop()
        needle = event.value.strip().lower()
        matches = [m for m in self._models if needle in m.lower()]
        self._populate_model_list(matches)

    def on_key(self, event: events.Key) -> None:
        # Let ↑/↓ drive the model list while the filter input keeps focus
        # (crush/opencode-style type-to-filter picker).
        if event.key not in ("up", "down"):
            return
        filter_input = self.query_one("#setup-model-filter", Input)
        if not filter_input.display or not filter_input.has_focus:
            return
        event.stop()
        model_list = self.query_one("#setup-model-list", OptionList)
        if not model_list.option_count:
            return
        current = model_list.highlighted or 0
        delta = 1 if event.key == "down" else -1
        model_list.highlighted = (current + delta) % model_list.option_count

    # ------------------------------------------------------------------
    # Model step
    # ------------------------------------------------------------------

    def _draft_settings(self, model: str) -> AgentSettings:
        return AgentSettings(
            provider=self._provider,
            auth_method=self._auth_method,
            base_url=self._base_url,
            model=model,
            api_key_env=self._api_key_env,
            # An explicitly configured profile always wins; otherwise local
            # Ollama endpoints usually serve 3B-14B models, so suggest the
            # reduced capability profile (issue #71).
            profile=self._current_profile or ("small" if self._provider == "ollama" else "full"),
            options=self._current_settings.options if self._current_settings is not None else {},
        )

    def _show_model_step(self, models: list[str]) -> None:
        self._models = models
        default_model = _DEFAULTS[self._provider][2]
        current = self._current_settings
        if current is not None and self._current_canonical == self._provider and current.model:
            # Reconnect flow (issue #167): the kept model beats the default.
            default_model = current.model
        if models:
            self._ask(f"Choose a model ({len(models)} available)")
            self.query_one("#setup-model-filter", Input).display = True
            model_list = self.query_one("#setup-model-list", OptionList)
            model_list.display = True
            self._populate_model_list(models)
            if default_model in models:
                model_list.highlighted = models.index(default_model)
            self.query_one("#setup-model-filter", Input).focus()
        else:
            self._ask("Which model should korvid use?")
            model_input = self.query_one("#setup-model", Input)
            model_input.value = default_model
            model_input.display = True
            model_input.focus()

    def _populate_model_list(self, models: list[str]) -> None:
        model_list = self.query_one("#setup-model-list", OptionList)
        model_list.clear_options()
        model_list.add_options([Option(m) for m in models])
        if models:
            model_list.highlighted = 0

    def _choose_model(self, model: str) -> None:
        self.query_one("#setup-model-filter", Input).display = False
        self.query_one("#setup-model-list", OptionList).display = False
        self._mark_done(f"Model: {model}")
        self._settings = self._draft_settings(model)
        self.run_worker(self._probe(), exclusive=True)

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def _copilot_connect(self) -> None:
        """Copilot: reuse an existing login when possible, else device login,
        then offer the models the API actually serves."""
        self._status("Checking for an existing GitHub login…")
        models = await self._configurator.list_models(self._draft_settings(""))
        if models:
            self._mark_done("GitHub login (already signed in)")
            self._status("")
            self._show_model_step(models)
            return
        device = self.query_one("#setup-device-code", Static)
        try:
            prompt = await self._configurator.begin_device_login()
            device.display = True
            device.update(f"Enter code {prompt.user_code} at {prompt.verification_uri}")
            self._status("Waiting for authorization…")
            await self._configurator.finish_device_login()
        except Exception as exc:  # login errors must not crash the app
            self._status(f"Login failed: {exc}")
            return
        device.display = False
        self._mark_done("GitHub login")
        self._status("Fetching available models…")
        models = await self._configurator.list_models(self._draft_settings(""))
        self._status("")
        self._show_model_step(models)

    async def _fetch_models(self) -> None:
        self._status("Fetching available models…")
        models = await self._configurator.list_models(self._draft_settings(""))
        self._status("")
        self._show_model_step(models)

    async def _probe(self) -> None:
        settings = self._settings
        if settings is None:
            return
        self._status("Testing connection…")
        try:
            await self._configurator.test(settings)
        except Exception as exc:  # keep the wizard open on probe failure
            self._status(f"Test failed: {exc} — press Ctrl+R to retry, Esc to cancel")
            return
        applied = False
        if self._apply_settings is not None:
            if not self._apply_settings(settings):
                # The app refused the swap (busy turn / rebuild failure): stay
                # open and do NOT persist, so a restart cannot silently activate
                # a configuration that never took effect.
                self._status("Apply failed — press Ctrl+R to retry, Esc to cancel")
                return
            applied = True
        try:
            await self._configurator.save(settings)
        except Exception as exc:  # keep the wizard open on save failure
            if applied:
                # The runtime already swapped: warn that the change is live
                # now but will revert to the previous settings on restart.
                self._status(
                    f"Applied, but save failed: {exc} — settings will revert on "
                    "restart. Press Ctrl+R to retry, Esc to cancel"
                )
            else:
                self._status(f"Save failed: {exc} — press Ctrl+R to retry, Esc to cancel")
            return
        self.dismiss(settings)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_retry(self) -> None:
        if self._settings is None:
            return
        # Re-read the still-visible inputs so an edit after a failed probe is
        # actually tested (device login is not repeated for Copilot; a model
        # picked from the list is kept unless the fallback input is visible).
        updates: dict[str, str | None] = {}
        base_input = self.query_one("#setup-base-url", Input)
        if base_input.display:
            updates["base_url"] = base_input.value.strip() or None
        env_input = self.query_one("#setup-api-key-env", Input)
        if env_input.display:
            updates["api_key_env"] = env_input.value.strip() or None
        model_input = self.query_one("#setup-model", Input)
        if model_input.display:
            model = model_input.value.strip()
            if not model:
                self._status("Model is required")
                return
            updates["model"] = model
        self._settings = dataclasses.replace(self._settings, **updates)  # type: ignore[arg-type]  # str|None matches each field
        self.run_worker(self._probe(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(None)
