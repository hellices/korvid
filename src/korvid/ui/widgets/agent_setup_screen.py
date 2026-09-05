"""AgentSetupScreen: the model-profile wizard, generated from descriptors.

Every question after the model search is rendered from data the catalog
supplies — an `AuthMethodDescriptor` and its `SetupField`s, the option
fields, and the endpoint requirement. No stage in this screen knows a
provider exists: adding one is a catalog/plugin change, never a source
edit here.

Stage order is load-bearing. The endpoint stage runs *before* the
auth-method stage because the catalog offers keyless auth only when it is
handed a non-empty endpoint; asking for the method first would ask with
`None` and never offer keyless auth to the operator it exists for.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Checkbox, Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    ConnectionAuthConfig,
    EndpointRequirement,
    ModelCatalog,
    ModelConnectionConfig,
    ModelEntry,
    SetupField,
    SetupFieldKind,
)
from korvid.ui.widgets.model_search_screen import ModelSearchScreen

#: A secret field collects the *name* of an environment variable, never a
#: value: this is the shape a name has, and the screen never resolves it.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: The capability tier is not a profile field — it is the agent's routing
#: override, persisted separately — but it is asked here so one pass
#: through the wizard answers everything a working agent needs.
_TIER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("automatic", "Automatic"),
    ("low", "Low"),
    ("high", "High"),
)


@dataclass(frozen=True, slots=True)
class SetupResult:
    """What the wizard produces: a profile plus the tier choice.

    The tier travels beside the profile rather than inside it because it
    is not transport configuration — the controller persists it as the
    agent's own setting.
    """

    profile: ModelConnectionConfig
    model_tier: str | None = None


class _Nav(Enum):
    """How a stage ended, and therefore where the wizard goes next."""

    NEXT = "next"
    BACK = "back"
    REPEAT = "repeat"
    CANCEL = "cancel"
    DONE = "done"


def _widget_for(field: SetupField, seed: object | None) -> Widget:
    """Render one declarative field.

    A `match` on the field kind is exhaustive over a closed set korvid
    owns — the one place in this screen where dispatching on a value is
    correct.
    """
    match field.kind:
        case SetupFieldKind.BOOLEAN:
            return Checkbox(field.label, value=bool(seed), id=f"field-{field.key}")
        case SetupFieldKind.CHOICE:
            return OptionList(*field.choices, id=f"field-{field.key}")
        case SetupFieldKind.INTEGER | SetupFieldKind.TEXT | SetupFieldKind.SECRET_REF:
            return Input(
                value="" if seed is None else str(seed),
                placeholder=field.help_text or field.label,
                id=f"field-{field.key}",
            )


def _read_field(field: SetupField, widget: Widget) -> tuple[object | None, str]:
    """Read one field's widget, returning `(value, error)`.

    A `None` value with an empty error means "left blank" — whether that
    is allowed is the caller's `required` check, not this function's.
    """
    if isinstance(widget, Checkbox):
        return bool(widget.value), ""
    if isinstance(widget, OptionList):
        index = widget.highlighted
        if index is None or index >= widget.option_count:
            return None, ""
        return str(widget.get_option_at_index(index).prompt), ""
    if isinstance(widget, Input):
        return _read_text_field(field, widget.value.strip())
    return None, ""


def _read_text_field(field: SetupField, raw: str) -> tuple[object | None, str]:
    if not raw:
        return None, ""
    if field.kind is SetupFieldKind.INTEGER:
        try:
            return int(raw), ""
        except ValueError:
            return None, f"{field.label} must be a whole number — {raw!r} is not."
    if field.kind is SetupFieldKind.SECRET_REF and not _ENV_NAME_RE.match(raw):
        return None, (
            f"{field.label} must be an environment variable name "
            "(A-Z, digits and underscores), not its value."
        )
    return raw, ""


def _collect_fields(
    fields: Sequence[SetupField], read: Callable[[str], Widget | None]
) -> tuple[dict[str, object] | None, str]:
    """Read a whole stage, or report the first reason it cannot be read."""
    values: dict[str, object] = {}
    for field in fields:
        widget = read(field.key)
        if widget is None:
            continue
        value, error = _read_field(field, widget)
        if error:
            return None, error
        if value is None:
            if field.required:
                return None, f"{field.label} is required."
            continue
        values[field.key] = value
    return values, ""


def _merged(
    previous: Mapping[str, object], claimed: Sequence[SetupField], collected: Mapping[str, object]
) -> dict[str, object]:
    """Overlay collected values on the values the wizard did not ask about.

    Keys no descriptor claimed belong to the operator: editing one option
    must never silently delete the rest.
    """
    claimed_keys = {field.key for field in claimed}
    merged = {key: value for key, value in previous.items() if key not in claimed_keys}
    merged.update(collected)
    return merged


class AgentSetupScreen(ModalScreen["SetupResult | None"]):
    """Conversational wizard: one descriptor-generated question at a time.

    Args:
        catalog: Answers every question the stages ask — search, endpoint
            requirement, auth methods, option fields, discovery and the
            connection probe.
        profile: The profile being edited, or None for a new one. Seeds
            every stage, so editing never silently resets a value.
        current_tier: The persisted tier override (None = Automatic).
        apply_result: Swaps the running agent onto the new profile. When
            it returns False the wizard stays open and nothing is saved.
        save_result: Persists the result. Called only after a successful
            apply, so a refused swap cannot become the configuration a
            restart activates.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+b", "back", "Back", show=True),
        Binding("ctrl+r", "retry", "Retry test", show=False),
    ]

    DEFAULT_CSS = """
    AgentSetupScreen {
        align: center middle;
    }
    AgentSetupScreen VerticalScroll {
        width: 74;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    AgentSetupScreen OptionList {
        height: auto;
        max-height: 10;
    }
    AgentSetupScreen #stage-body {
        height: auto;
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
        catalog: ModelCatalog,
        *,
        profile: ModelConnectionConfig | None = None,
        current_tier: str | None = None,
        apply_result: Callable[[SetupResult], bool] | None = None,
        save_result: Callable[[SetupResult], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._seed = profile if profile is not None else ModelConnectionConfig(model="")
        self._apply_result = apply_result
        self._save_result = save_result
        #: The persisted tier override; the wizard's own answer is a draft
        #: until it dismisses.
        self._model_tier = current_tier
        self._reference = ""
        self._endpoint: str | None = None
        self._method: AuthMethodDescriptor | None = None
        self._auth_values: dict[str, object] = {}
        self._option_fields: tuple[SetupField, ...] = ()
        self._option_values: dict[str, object] = {}
        self._done_steps: dict[str, str] = {}
        #: Resolved by whatever ends the mounted stage — a submitted input,
        #: a chosen option, or one of the navigation bindings.
        self._stage_nav: asyncio.Future[_Nav] | None = None

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="setup-steps")
            yield Static("", id="setup-title")
            yield Vertical(id="stage-body")
            # Hidden here rather than in on_mount: a screen pushed over a
            # running app mounts its children asynchronously, so on_mount
            # can run before this node exists.
            device_code = Static(id="setup-device-code")
            device_code.display = False
            yield device_code
            yield Static(id="setup-status")

    def on_mount(self) -> None:
        self.run_worker(self._run(), exclusive=True)

    # ------------------------------------------------------------------
    # Checklist / status helpers
    # ------------------------------------------------------------------

    def _mark_done(self, key: str, step: str) -> None:
        """Record a completed step, replacing any earlier answer to it."""
        self._done_steps[key] = step
        lines = "\n".join(f"✓ {text}" for text in self._done_steps.values())
        self.query_one("#setup-steps", Static).update(lines)

    def _ask(self, question: str) -> None:
        self.query_one("#setup-title", Static).update(question)

    def _status(self, text: str) -> None:
        self.query_one("#setup-status", Static).update(text)

    # ------------------------------------------------------------------
    # Stage machine
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        stages: tuple[Callable[[_Nav], Awaitable[_Nav]], ...] = (
            self._stage_model,
            self._stage_endpoint,
            self._stage_auth_method,
            self._stage_auth_fields,
            self._stage_authorize,
            self._stage_options,
            self._stage_tier,
            self._stage_finish,
        )
        index = 0
        direction = _Nav.NEXT
        while 0 <= index < len(stages):
            outcome = await stages[index](direction)
            if outcome is _Nav.CANCEL:
                self.dismiss(None)
                return
            if outcome is _Nav.DONE:
                return
            direction = outcome
            index += {_Nav.NEXT: 1, _Nav.BACK: -1, _Nav.REPEAT: 0}[outcome]
            index = max(index, 0)

    async def _mount_stage(self, question: str, widgets: Sequence[Widget]) -> None:
        body = self.query_one("#stage-body", Vertical)
        await body.remove_children()
        self._ask(question)
        self._status("")
        self.query_one("#setup-device-code", Static).display = False
        await body.mount_all(list(widgets))
        for widget in widgets:
            if widget.focusable:
                widget.focus()
                break

    async def _await_nav(self) -> _Nav:
        """Wait for the mounted stage to be submitted, or navigated away."""
        future: asyncio.Future[_Nav] = asyncio.get_running_loop().create_future()
        self._stage_nav = future
        try:
            return await future
        finally:
            self._stage_nav = None

    def _resolve_nav(self, outcome: _Nav) -> None:
        future = self._stage_nav
        if future is not None and not future.done():
            future.set_result(outcome)

    def _stage_widget(self, key: str) -> Widget | None:
        found = self.query(f"#field-{key}")
        return found.first() if found else None

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    async def _stage_model(self, direction: _Nav) -> _Nav:
        """The model search (Task 10's screen) is the first question."""
        discovered = await self._discover()
        initial = self._reference or self._seed.model
        reference = await self.app.push_screen_wait(
            ModelSearchScreen(self._catalog, initial_query=initial, discovered=discovered)
        )
        if reference is None:
            return _Nav.CANCEL
        self._reference = reference
        self._mark_done("model", f"Model: {reference}")
        return _Nav.NEXT

    async def _discover(self) -> tuple[ModelEntry, ...]:
        """Best-effort live listing. Empty is the normal offline case."""
        try:
            return await self._catalog.discover(self._draft_profile())
        except Exception:  # discovery is advisory: never an error dialog
            return ()

    async def _stage_endpoint(self, direction: _Nav) -> _Nav:
        requirement = self._catalog.endpoint_requirement(self._reference)
        if requirement is EndpointRequirement.UNSUPPORTED:
            self._endpoint = None
            self._done_steps.pop("endpoint", None)
            return direction
        seed = self._endpoint if self._endpoint is not None else self._seed.endpoint
        widget = Input(
            value=seed or "",
            placeholder="endpoint URL",
            id="setup-endpoint",
        )
        required = requirement is EndpointRequirement.REQUIRED
        question = (
            "Which endpoint should korvid connect to?"
            if required
            else "Which endpoint should korvid connect to? (blank for the provider default)"
        )
        await self._mount_stage(question, [widget])
        while True:
            outcome = await self._await_nav()
            if outcome is not _Nav.NEXT:
                return outcome
            value = self.query_one("#setup-endpoint", Input).value.strip()
            if not value and required:
                self._status("An endpoint is required for this model.")
                continue
            self._endpoint = value or None
            self._mark_done("endpoint", f"Endpoint: {self._endpoint or 'provider default'}")
            return _Nav.NEXT

    async def _stage_auth_method(self, direction: _Nav) -> _Nav:
        """Recomputed on every visit: a stale method list is a trap."""
        methods = self._catalog.auth_methods(self._reference, endpoint=self._endpoint)
        if not methods:
            self._method = None
            self._done_steps.pop("auth", None)
            return direction
        chosen = self._method.id if self._method is not None else self._seed.auth.method
        options = OptionList(
            *(Option(method.display_name, id=method.id) for method in methods),
            id="setup-auth",
        )
        await self._mount_stage("How should korvid authenticate?", [options])
        ids = [method.id for method in methods]
        options.highlighted = ids.index(chosen) if chosen in ids else 0
        outcome = await self._await_nav()
        if outcome is not _Nav.NEXT:
            return outcome
        index = options.highlighted or 0
        method = methods[min(index, len(methods) - 1)]
        if self._method is not None and self._method.id != method.id:
            self._auth_values = {}
        self._method = method
        self._mark_done("auth", f"Auth: {method.display_name}")
        return _Nav.NEXT

    async def _stage_auth_fields(self, direction: _Nav) -> _Nav:
        method = self._method
        if method is None or not method.fields:
            self._auth_values = {}
            return direction
        seeds = self._auth_seeds(method)
        widgets = [_widget_for(field, seeds.get(field.key)) for field in method.fields]
        await self._mount_stage(method.display_name, self._labelled(method.fields, widgets))
        return await self._collect_stage(method.fields, self._store_auth_values)

    def _auth_seeds(self, method: AuthMethodDescriptor) -> dict[str, object]:
        """Seed order: what this wizard already collected, then the edited
        profile (only for the same method), then the descriptor default."""
        seeds: dict[str, object] = {}
        for field in method.fields:
            if field.key in self._auth_values:
                seeds[field.key] = self._auth_values[field.key]
            elif self._seed.auth.method == method.id and field.key in self._seed.auth.settings:
                seeds[field.key] = self._seed.auth.settings[field.key]
            elif field.default is not None:
                seeds[field.key] = field.default
        return seeds

    def _store_auth_values(self, values: dict[str, object]) -> None:
        self._auth_values = values

    async def _stage_authorize(self, direction: _Nav) -> _Nav:
        """Interactive sign-in, when the catalog says the profile needs one."""
        if direction is _Nav.BACK:
            return direction
        profile = self._draft_profile()
        device = self.query_one("#setup-device-code", Static)
        try:
            prompt = await self._catalog.begin_auth(profile)
        except Exception as exc:  # sign-in errors must not crash the app
            self._status(f"Authorization failed: {exc} — press Ctrl+R to retry, Esc to cancel")
            return await self._await_nav()
        if prompt is None:
            return _Nav.NEXT
        device.display = True
        device.update(f"Enter code {prompt.user_code} at {prompt.verification_uri}")
        self._status("Waiting for authorization…")
        try:
            await self._catalog.finish_auth(profile)
        except Exception as exc:  # keep the wizard open on a failed login
            self._status(f"Login failed: {exc} — press Ctrl+R to retry, Esc to cancel")
            return await self._await_nav()
        device.display = False
        self._mark_done("authorize", "Signed in")
        self._status("")
        return _Nav.NEXT

    async def _stage_options(self, direction: _Nav) -> _Nav:
        fields = self._catalog.option_fields(self._reference)
        self._option_fields = fields
        if not fields:
            return direction
        seeds = self._option_seeds(fields)
        widgets = [_widget_for(field, seeds.get(field.key)) for field in fields]
        await self._mount_stage("Model options", self._labelled(fields, widgets))
        return await self._collect_stage(fields, self._store_option_values)

    def _option_seeds(self, fields: Sequence[SetupField]) -> dict[str, object]:
        """Profile first, descriptor default second: editing a profile must
        start from its own options or it silently resets them."""
        seeds: dict[str, object] = {}
        for field in fields:
            if field.key in self._option_values:
                seeds[field.key] = self._option_values[field.key]
            elif field.key in self._seed.options:
                seeds[field.key] = self._seed.options[field.key]
            elif field.default is not None:
                seeds[field.key] = field.default
        return seeds

    def _store_option_values(self, values: dict[str, object]) -> None:
        self._option_values = values

    async def _stage_tier(self, direction: _Nav) -> _Nav:
        options = OptionList(
            *(Option(label, id=tier_id) for tier_id, label in _TIER_OPTIONS),
            id="setup-tier",
        )
        await self._mount_stage("Which capability tier should this model use?", [options])
        ids = [tier_id for tier_id, _ in _TIER_OPTIONS]
        options.highlighted = ids.index(self._model_tier or "automatic")
        outcome = await self._await_nav()
        if outcome is not _Nav.NEXT:
            return outcome
        tier_id = ids[options.highlighted or 0]
        self._model_tier = None if tier_id == "automatic" else tier_id
        self._mark_done("tier", f"Tier: {dict(_TIER_OPTIONS)[tier_id]}")
        return _Nav.NEXT

    async def _stage_finish(self, direction: _Nav) -> _Nav:
        if direction is _Nav.BACK:
            return direction
        result = SetupResult(profile=self._draft_profile(), model_tier=self._model_tier)
        await self._mount_stage("Testing the connection…", [])
        self._status("Testing connection…")
        try:
            await self._catalog.test(result.profile)
        except Exception as exc:  # keep the wizard open on probe failure
            self._status(f"Test failed: {exc} — press Ctrl+R to retry, Esc to cancel")
            return await self._await_nav()
        applied = False
        if self._apply_result is not None:
            if not self._apply_result(result):
                # The app refused the swap (busy turn / rebuild failure):
                # stay open and do NOT persist, so a restart cannot silently
                # activate a configuration that never took effect.
                self._status("Apply failed — press Ctrl+R to retry, Esc to cancel")
                return await self._await_nav()
            applied = True
        if self._save_result is not None:
            saved = await self._persist(result, applied=applied)
            if not saved:
                return await self._await_nav()
        self.dismiss(result)
        return _Nav.DONE

    async def _persist(self, result: SetupResult, *, applied: bool) -> bool:
        save = self._save_result
        if save is None:
            return True
        try:
            await save(result)
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
            return False
        return True

    # ------------------------------------------------------------------
    # Field stage plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _labelled(fields: Sequence[SetupField], widgets: Sequence[Widget]) -> list[Widget]:
        """Pair every non-self-labelling widget with its field label."""
        rendered: list[Widget] = []
        for field, widget in zip(fields, widgets, strict=True):
            if not isinstance(widget, Checkbox):
                rendered.append(Static(field.label, classes="field-label"))
            rendered.append(widget)
        return rendered

    async def _collect_stage(
        self, fields: Sequence[SetupField], store: Callable[[dict[str, object]], None]
    ) -> _Nav:
        while True:
            outcome = await self._await_nav()
            if outcome is not _Nav.NEXT:
                return outcome
            values, error = _collect_fields(fields, self._stage_widget)
            if values is None:
                self._status(error)
                continue
            store(values)
            return _Nav.NEXT

    def _draft_profile(self) -> ModelConnectionConfig:
        method = self._method.id if self._method is not None else self._seed.auth.method
        auth_fields = self._method.fields if self._method is not None else ()
        previous_auth = self._seed.auth.settings if self._seed.auth.method == method else {}
        return ModelConnectionConfig(
            model=self._reference or self._seed.model,
            endpoint=self._endpoint if self._endpoint is not None else self._seed.endpoint,
            auth=ConnectionAuthConfig(
                method=method,
                settings=_merged(previous_auth, auth_fields, self._auth_values),
            ),
            options=_merged(self._seed.options, self._option_fields, self._option_values),
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._resolve_nav(_Nav.NEXT)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        widget_id = event.option_list.id or ""
        if widget_id.startswith("field-"):
            # One field of a multi-field stage: the stage is submitted as a
            # whole, so selecting a choice must not skip its siblings.
            return
        self._resolve_nav(_Nav.NEXT)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_back(self) -> None:
        self._resolve_nav(_Nav.BACK)

    def action_retry(self) -> None:
        self._resolve_nav(_Nav.REPEAT)

    def action_cancel(self) -> None:
        self.dismiss(None)
