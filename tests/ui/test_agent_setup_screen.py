"""Tests for the descriptor-driven model setup wizard (Task 11).

Migrated from the vendor wizard's tests: every behaviour that file
protected and that still exists — checklist rendering, cancel semantics,
the retry binding, apply-before-persist, and the dismiss contract — keeps
a test here, restated against the profile-first flow.

Nothing in these tests names a vendor. The catalog is a fake that answers
with descriptors korvid has never heard of, which is the whole point: a
stage is rendered because a `SetupField` said so, not because the screen
recognised an id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input, OptionList, Static

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelCatalog,
    ModelConnectionConfig,
    ModelEntry,
    SetupField,
    SetupFieldKind,
)
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen, SetupResult
from korvid.ui.widgets.model_search_screen import ModelSearchScreen

from .waits import until

_REFERENCE = "acme/model-x"

_KEY_FIELD = SetupField(
    key="key",
    label="Environment variable name",
    kind=SetupFieldKind.SECRET_REF,
    required=True,
    help_text="Name of the environment variable holding the API key.",
)

_ENVIRONMENT = AuthMethodDescriptor(
    id="environment",
    display_name="Environment variable",
    fields=(_KEY_FIELD,),
)

_NONE_METHOD = AuthMethodDescriptor(id="none", display_name="No authentication")


class _FakeCatalog(ModelCatalog):
    """A catalog whose answers are data the test hands it.

    `auth_methods` mirrors the real rule — `none` is offered only when it
    is given a non-empty endpoint — because the stage ordering the screen
    must respect is exactly what that rule makes observable.
    """

    def __init__(
        self,
        *,
        auth: tuple[AuthMethodDescriptor, ...] = (_ENVIRONMENT,),
        options: tuple[SetupField, ...] = (),
        endpoint: EndpointRequirement = EndpointRequirement.OPTIONAL,
        entries: tuple[ModelEntry, ...] = (),
        discovered: tuple[ModelEntry, ...] = (),
        discover_error: Exception | None = None,
        test_error: Exception | None = None,
        device_prompt: DeviceLoginPrompt | None = None,
        finish_error: Exception | None = None,
    ) -> None:
        self._auth = auth
        self._options = options
        self._endpoint = endpoint
        self._entries = entries
        self._discovered = discovered
        self._discover_error = discover_error
        self._test_error = test_error
        self._device_prompt = device_prompt
        self._finish_error = finish_error
        self.tested: list[ModelConnectionConfig] = []
        self.discovered_for: list[ModelConnectionConfig] = []
        self.finished: list[ModelConnectionConfig] = []

    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        needle = query.strip().lower()
        return tuple(e for e in self._entries if needle in e.reference.lower())[:limit]

    def entry(self, reference: str) -> ModelEntry | None:
        return next((e for e in self._entries if e.reference == reference), None)

    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]:
        methods = [m for m in self._auth if m.id != "none" or bool(endpoint)]
        return tuple(methods)

    def option_fields(self, reference: str) -> tuple[SetupField, ...]:
        return self._options

    def endpoint_requirement(self, reference: str) -> EndpointRequirement:
        return self._endpoint

    async def discover(self, profile: ModelConnectionConfig) -> tuple[ModelEntry, ...]:
        self.discovered_for.append(profile)
        if self._discover_error is not None:
            raise self._discover_error
        return self._discovered

    async def test(self, profile: ModelConnectionConfig) -> str:
        self.tested.append(profile)
        if self._test_error is not None:
            raise self._test_error
        return "ok"

    async def begin_auth(self, profile: ModelConnectionConfig) -> DeviceLoginPrompt | None:
        return self._device_prompt

    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None:
        if self._finish_error is not None:
            raise self._finish_error
        self.finished.append(profile)
        return None


class _Host(App[None]):
    """Pushes the wizard and records what it dismisses with."""

    def __init__(
        self,
        catalog: ModelCatalog,
        *,
        profile: ModelConnectionConfig | None = None,
        current_tier: str | None = None,
        apply_result: Any = None,
        save_result: Any = None,
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._profile = profile
        self._current_tier = current_tier
        self._apply_result = apply_result
        self._save_result = save_result
        self.result: SetupResult | str | None = "unset"

    def on_mount(self) -> None:
        def _done(res: SetupResult | None) -> None:
            self.result = res

        self.push_screen(
            AgentSetupScreen(
                self._catalog,
                profile=self._profile,
                current_tier=self._current_tier,
                apply_result=self._apply_result,
                save_result=self._save_result,
            ),
            callback=_done,
        )


# ---------------------------------------------------------------------------
# Flow helpers
# ---------------------------------------------------------------------------


def _setup_screen(app: App[None]) -> AgentSetupScreen:
    screen = next(s for s in reversed(app.screen_stack) if isinstance(s, AgentSetupScreen))
    return screen


async def _wait_for(app: App[None], pilot: Any, selector: str, label: str) -> None:
    await until(pilot, lambda: bool(_setup_screen(app).query(selector)), label=label)


async def _pick_model(pilot: Any, reference: str = _REFERENCE) -> None:
    """Answer the first stage — Task 10's search screen — with a reference."""
    app = pilot.app
    await until(
        pilot,
        lambda: isinstance(app.screen, ModelSearchScreen),
        label="model search screen",
    )
    query = app.screen.query_one("#model-query", Input)
    query.value = reference
    query.focus()
    await pilot.press("enter")
    await until(
        pilot,
        lambda: not isinstance(app.screen, ModelSearchScreen),
        label="model search dismissed",
    )


async def _submit_endpoint(pilot: Any, endpoint: str) -> None:
    app = pilot.app
    await _wait_for(app, pilot, "#setup-endpoint", "endpoint stage")
    field = _setup_screen(app).query_one("#setup-endpoint", Input)
    field.value = endpoint
    field.focus()
    await pilot.press("enter")


async def _advance_to_auth_method(pilot: Any, *, endpoint: str = "") -> None:
    await _pick_model(pilot)
    await _submit_endpoint(pilot, endpoint)
    await _wait_for(pilot.app, pilot, "#setup-auth", "auth-method stage")


def _offered_method_ids(pilot: Any) -> list[str]:
    methods = _setup_screen(pilot.app).query_one("#setup-auth", OptionList)
    return [methods.get_option_at_index(i).id or "" for i in range(methods.option_count)]


async def _choose_auth_method(pilot: Any, method_id: str) -> None:
    app = pilot.app
    methods = _setup_screen(app).query_one("#setup-auth", OptionList)
    methods.highlighted = _offered_method_ids(pilot).index(method_id)
    methods.focus()
    await pilot.press("enter")


async def _go_back(pilot: Any) -> None:
    await pilot.press("ctrl+b")


async def _go_back_and_clear_the_endpoint(pilot: Any) -> None:
    await _go_back(pilot)
    await _submit_endpoint(pilot, "")
    await _wait_for(pilot.app, pilot, "#setup-auth", "auth-method stage recomputed")


async def _submit_stage(pilot: Any) -> None:
    """Submit whatever field stage is mounted (Enter on its first input)."""
    app = pilot.app
    body = _setup_screen(app).query_one("#stage-body")
    inputs = body.query(Input)
    if inputs:
        inputs.first().focus()
        await pilot.press("enter")
        return
    await pilot.press("ctrl+j")


async def _accept_tier(pilot: Any, tier_id: str = "automatic") -> None:
    app = pilot.app
    await _wait_for(app, pilot, "#setup-tier", "tier stage")
    tiers = _setup_screen(app).query_one("#setup-tier", OptionList)
    ids = [tiers.get_option_at_index(i).id or "" for i in range(tiers.option_count)]
    tiers.highlighted = ids.index(tier_id)
    tiers.focus()
    await pilot.press("enter")


async def _run_to_completion(
    pilot: Any,
    *,
    endpoint: str = "",
    method_id: str = "environment",
    key: str = "ACME_KEY",
    tier_id: str = "automatic",
) -> None:
    """Drive the whole wizard: model, endpoint, auth, fields, tier, test."""
    await _advance_to_auth_method(pilot, endpoint=endpoint)
    await _choose_auth_method(pilot, method_id)
    if method_id == "environment":
        await _wait_for(pilot.app, pilot, "#field-key", "auth field stage")
        field = _setup_screen(pilot.app).query_one("#field-key", Input)
        field.value = key
        field.focus()
        await pilot.press("enter")
    await _accept_tier(pilot, tier_id)


def _status_text(app: App[None]) -> str:
    return str(_setup_screen(app).query_one("#setup-status", Static).render())


def _steps_text(app: App[None]) -> str:
    return str(_setup_screen(app).query_one("#setup-steps", Static).render())


def _as_dict(result: SetupResult) -> dict[str, Any]:
    profile = result.profile
    return {
        "model": profile.model,
        "endpoint": profile.endpoint,
        "auth": {"method": profile.auth.method, "settings": dict(profile.auth.settings)},
        "options": dict(profile.options),
        "model_tier": result.model_tier,
    }


# ---------------------------------------------------------------------------
# Descriptor-driven stages
# ---------------------------------------------------------------------------


async def test_stages_are_generated_from_descriptors_not_hardcoded() -> None:
    """A fake catalog returning one exotic auth method with one field must
    produce exactly that prompt. Nothing in the screen may special case an
    id it has not been given."""
    catalog = _FakeCatalog(
        auth=(
            AuthMethodDescriptor(
                id="mtls-cert",
                display_name="Client certificate",
                fields=(
                    SetupField(
                        key="cert_path",
                        label="Certificate path",
                        kind=SetupFieldKind.TEXT,
                        required=True,
                    ),
                ),
            ),
        )
    )
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot)
        assert _offered_method_ids(pilot) == ["mtls-cert"]
        methods = _setup_screen(app).query_one("#setup-auth", OptionList)
        rendered = " ".join(
            str(methods.get_option_at_index(i).prompt) for i in range(methods.option_count)
        )
        assert "Client certificate" in rendered
        await _choose_auth_method(pilot, "mtls-cert")
        await _wait_for(app, pilot, "#field-cert_path", "descriptor field stage")
        title = str(_setup_screen(app).query_one("#setup-title", Static).render())
        labels = " ".join(str(s.render()) for s in _setup_screen(app).query(Static))
        assert "Client certificate" in title
        assert "Certificate path" in labels


async def test_a_required_field_blocks_progress_and_says_why() -> None:
    catalog = _FakeCatalog()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot)
        await _choose_auth_method(pilot, "environment")
        await _wait_for(app, pilot, "#field-key", "auth field stage")
        await _submit_stage(pilot)
        await until(
            pilot,
            lambda: "required" in _status_text(app).lower(),
            label="required-field reason shown",
        )
        assert "Environment variable name" in _status_text(app)
        assert _setup_screen(app).query("#field-key")  # stage kept


async def test_a_secret_ref_field_stores_the_name_and_never_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard writes the *name* of an environment variable. It must
    never write the variable's value into the profile."""
    monkeypatch.setenv("SOME_KEY", "sk-secret-value")
    catalog = _FakeCatalog()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot, key="SOME_KEY")
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        result = app.result
        assert isinstance(result, SetupResult)
        assert result.profile.auth.method == "environment"
        assert result.profile.auth.settings["key"] == "SOME_KEY"
        assert "sk-secret-value" not in json.dumps(_as_dict(result))


async def test_a_secret_ref_field_refuses_a_value_that_is_not_a_variable_name() -> None:
    catalog = _FakeCatalog()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot)
        await _choose_auth_method(pilot, "environment")
        await _wait_for(app, pilot, "#field-key", "auth field stage")
        field = _setup_screen(app).query_one("#field-key", Input)
        field.value = "sk-secret-value"
        field.focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: "environment variable name" in _status_text(app).lower(),
            label="secret-ref validation message",
        )
        assert _setup_screen(app).query("#field-key")  # stage kept


async def test_option_fields_are_seeded_from_the_edited_profile() -> None:
    """Editing must start from the profile's current options, not from
    descriptor defaults — otherwise editing silently resets them."""
    catalog = _FakeCatalog(
        auth=(_NONE_METHOD,),
        options=(
            SetupField(
                key="num_ctx", label="Context size", kind=SetupFieldKind.INTEGER, default="2048"
            ),
            SetupField(key="native_thinking", label="Native thinking", kind=SetupFieldKind.BOOLEAN),
        ),
    )
    profile = ModelConnectionConfig(
        model=_REFERENCE, options={"num_ctx": 8192, "native_thinking": True}
    )
    app = _Host(catalog, profile=profile)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _choose_auth_method(pilot, "none")
        await _wait_for(app, pilot, "#field-num_ctx", "option stage")
        screen = _setup_screen(app)
        assert screen.query_one("#field-num_ctx", Input).value == "8192"
        assert screen.query_one("#field-native_thinking", Checkbox).value is True


async def test_an_option_field_falls_back_to_its_descriptor_default() -> None:
    catalog = _FakeCatalog(
        auth=(_NONE_METHOD,),
        options=(
            SetupField(
                key="num_ctx", label="Context size", kind=SetupFieldKind.INTEGER, default="2048"
            ),
        ),
    )
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _choose_auth_method(pilot, "none")
        await _wait_for(app, pilot, "#field-num_ctx", "option stage")
        assert _setup_screen(app).query_one("#field-num_ctx", Input).value == "2048"


async def test_an_integer_field_refuses_a_non_integer_without_losing_the_stage() -> None:
    catalog = _FakeCatalog(
        auth=(_NONE_METHOD,),
        options=(SetupField(key="num_ctx", label="Context size", kind=SetupFieldKind.INTEGER),),
    )
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _choose_auth_method(pilot, "none")
        await _wait_for(app, pilot, "#field-num_ctx", "option stage")
        field = _setup_screen(app).query_one("#field-num_ctx", Input)
        field.value = "many"
        field.focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: "whole number" in _status_text(app).lower(),
            label="integer validation message",
        )
        screen = _setup_screen(app)
        assert screen.query_one("#field-num_ctx", Input).value == "many"


async def test_an_unknown_option_of_the_edited_profile_survives_the_wizard() -> None:
    """A key no descriptor claims is the operator's, not the wizard's, to
    delete: editing one option must not silently drop the others."""
    catalog = _FakeCatalog(auth=(_NONE_METHOD,))
    profile = ModelConnectionConfig(model=_REFERENCE, options={"unclaimed": "kept"})
    app = _Host(catalog, profile=profile)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _choose_auth_method(pilot, "none")
        await _accept_tier(pilot)
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        assert isinstance(app.result, SetupResult)
        assert dict(app.result.profile.options) == {"unclaimed": "kept"}


# ---------------------------------------------------------------------------
# Endpoint stage
# ---------------------------------------------------------------------------


async def test_an_endpoint_stage_appears_only_when_the_descriptor_requires_it() -> None:
    required = _Host(_FakeCatalog(endpoint=EndpointRequirement.REQUIRED))
    async with required.run_test() as pilot:
        await _pick_model(pilot)
        await _wait_for(required, pilot, "#setup-endpoint", "endpoint stage present")
        assert required.screen.query("#setup-endpoint")

    unsupported = _Host(_FakeCatalog(endpoint=EndpointRequirement.UNSUPPORTED))
    async with unsupported.run_test() as pilot:
        await _pick_model(pilot)
        await _wait_for(unsupported, pilot, "#setup-auth", "auth stage reached")
        assert list(unsupported.screen.query("#setup-endpoint")) == []


async def test_a_required_endpoint_blocks_completion_when_empty() -> None:
    app = _Host(_FakeCatalog(endpoint=EndpointRequirement.REQUIRED))
    async with app.run_test() as pilot:
        await _pick_model(pilot)
        await _submit_endpoint(pilot, "")
        await until(
            pilot,
            lambda: "endpoint" in _status_text(app).lower(),
            label="endpoint requirement message",
        )
        assert _setup_screen(app).query("#setup-endpoint")
        assert list(_setup_screen(app).query("#setup-auth")) == []


async def test_an_optional_endpoint_may_be_skipped_with_an_empty_answer() -> None:
    app = _Host(_FakeCatalog(endpoint=EndpointRequirement.OPTIONAL))
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="")
        assert _offered_method_ids(pilot) == ["environment"]


async def test_the_endpoint_stage_runs_before_the_auth_method_stage() -> None:
    """`none` is offered only when an endpoint is known, so the order of
    these two stages decides whether a local-endpoint operator is ever
    offered keyless auth at all."""
    seen: list[str | None] = []

    class _RecordingCatalog(_FakeCatalog):
        def auth_methods(
            self, reference: str, *, endpoint: str | None = None
        ) -> tuple[AuthMethodDescriptor, ...]:
            seen.append(endpoint)
            return super().auth_methods(reference, endpoint=endpoint)

    app = _Host(_RecordingCatalog(auth=(_ENVIRONMENT, _NONE_METHOD)))
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        assert seen == ["http://endpoint.example"]


async def test_changing_the_endpoint_recomputes_the_auth_methods() -> None:
    """Going back and clearing the endpoint must withdraw `none`."""
    app = _Host(_FakeCatalog(auth=(_ENVIRONMENT, _NONE_METHOD)))
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        assert "none" in _offered_method_ids(pilot)
        await _go_back_and_clear_the_endpoint(pilot)
        assert "none" not in _offered_method_ids(pilot)


# ---------------------------------------------------------------------------
# Discovery and device login
# ---------------------------------------------------------------------------


async def test_discovery_failure_falls_through_to_manual_entry() -> None:
    """`discover` returning () is the normal offline case, not an error."""
    app = _Host(_FakeCatalog(discovered=()))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: isinstance(app.screen, ModelSearchScreen),
            label="model search screen",
        )
        assert str(app.screen.query_one("#search-status", Static).render()) != ""
        assert list(app.screen.query("#error-dialog")) == []


async def test_a_raising_discovery_still_reaches_the_search_screen() -> None:
    app = _Host(_FakeCatalog(discover_error=RuntimeError("no route to host")))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: isinstance(app.screen, ModelSearchScreen),
            label="model search screen",
        )
        assert list(app.screen.query("#error-dialog")) == []


async def test_a_device_login_prompt_renders_the_uri_and_code() -> None:
    catalog = _FakeCatalog(
        auth=(AuthMethodDescriptor(id="device-login", display_name="Sign in"),),
        device_prompt=DeviceLoginPrompt(
            verification_uri="https://example.test/device",
            user_code="ABCD-1234",
            expires_in_seconds=600,
        ),
    )
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot)
        await _choose_auth_method(pilot, "device-login")
        await until(
            pilot,
            lambda: (
                "ABCD-1234"
                in str(_setup_screen(app).query_one("#setup-device-code", Static).render())
            ),
            label="device code rendered",
        )
        text = str(_setup_screen(app).query_one("#setup-device-code", Static).render())
        assert "https://example.test/device" in text


async def test_a_failed_device_login_keeps_the_wizard_open() -> None:
    catalog = _FakeCatalog(
        auth=(AuthMethodDescriptor(id="device-login", display_name="Sign in"),),
        device_prompt=DeviceLoginPrompt(
            verification_uri="https://example.test/device",
            user_code="ABCD-1234",
            expires_in_seconds=600,
        ),
        finish_error=RuntimeError("authorization expired"),
    )
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot)
        await _choose_auth_method(pilot, "device-login")
        await until(
            pilot,
            lambda: "authorization expired" in _status_text(app),
            label="login failure reported",
        )
        assert app.result == "unset"
        assert _setup_screen(app).is_running


# ---------------------------------------------------------------------------
# Checklist, cancel, retry (migrated)
# ---------------------------------------------------------------------------


async def test_the_checklist_records_each_completed_step() -> None:
    app = _Host(_FakeCatalog())
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await until(pilot, lambda: "✓" in _steps_text(app), label="checklist rendered")
        steps = _steps_text(app)
        assert _REFERENCE in steps
        assert "http://endpoint.example" in steps


async def test_going_back_does_not_duplicate_a_checklist_entry() -> None:
    app = _Host(_FakeCatalog(auth=(_ENVIRONMENT, _NONE_METHOD)))
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _go_back_and_clear_the_endpoint(pilot)
        assert _steps_text(app).count(_REFERENCE) == 1


async def test_escape_dismisses_none() -> None:
    catalog = _FakeCatalog()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.result is None, label="dismissed with None")
        assert catalog.tested == []


async def test_cancelling_the_model_search_cancels_the_wizard() -> None:
    app = _Host(_FakeCatalog())
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: isinstance(app.screen, ModelSearchScreen),
            label="model search screen",
        )
        await pilot.press("escape")
        await until(pilot, lambda: app.result is None, label="dismissed with None")


async def test_probe_failure_keeps_the_screen_open_and_shows_the_error() -> None:
    saved: list[SetupResult] = []

    async def _save(result: SetupResult) -> None:
        saved.append(result)

    app = _Host(_FakeCatalog(test_error=RuntimeError("connection refused")), save_result=_save)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(
            pilot,
            lambda: "connection refused" in _status_text(app),
            label="probe failure reported",
        )
        assert app.result == "unset"
        assert saved == []


async def test_ctrl_r_retries_a_failed_probe() -> None:
    """The retry binding re-runs the probe with what the wizard collected,
    and must work while a widget inside the screen holds focus."""
    catalog = _FakeCatalog(test_error=RuntimeError("boom"))
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(pilot, lambda: "boom" in _status_text(app), label="probe failure reported")
        catalog._test_error = None
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        assert len(catalog.tested) == 2


# ---------------------------------------------------------------------------
# Apply before persist (migrated)
# ---------------------------------------------------------------------------


async def test_applying_settings_precedes_persisting_them() -> None:
    """A refused swap must leave the wizard open and the config unwritten."""
    saved: list[SetupResult] = []

    async def _save(result: SetupResult) -> None:
        saved.append(result)

    app = _Host(_FakeCatalog(), apply_result=lambda _result: False, save_result=_save)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(pilot, lambda: "Apply failed" in _status_text(app), label="apply refused")
        assert saved == []
        assert _setup_screen(app).is_running
        assert app.result == "unset"


async def test_a_successful_apply_is_followed_by_a_save_and_a_dismiss() -> None:
    order: list[str] = []

    def _apply(result: SetupResult) -> bool:
        order.append("apply")
        return True

    async def _save(result: SetupResult) -> None:
        order.append("save")

    app = _Host(_FakeCatalog(), apply_result=_apply, save_result=_save)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        assert order == ["apply", "save"]


async def test_save_failure_shows_the_error_and_keeps_the_screen_open() -> None:
    async def _save(result: SetupResult) -> None:
        raise RuntimeError("disk full")

    app = _Host(_FakeCatalog(), save_result=_save)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(pilot, lambda: "disk full" in _status_text(app), label="save failure reported")
        assert app.result == "unset"


async def test_save_failure_after_apply_warns_about_a_restart_revert() -> None:
    applied: list[SetupResult] = []

    def _apply(result: SetupResult) -> bool:
        applied.append(result)
        return True

    async def _save(result: SetupResult) -> None:
        raise RuntimeError("disk full")

    app = _Host(_FakeCatalog(), apply_result=_apply, save_result=_save)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(pilot, lambda: "disk full" in _status_text(app), label="save failure reported")
        text = _status_text(app).lower()
        assert applied
        assert app.result == "unset"
        assert "applied" in text
        assert "revert" in text


async def test_the_wizard_dismisses_a_result_or_none() -> None:
    """The dismiss contract, restated for profiles: a completed wizard hands
    back a `SetupResult`; a cancelled one hands back `None`."""
    app = _Host(_FakeCatalog())
    async with app.run_test() as pilot:
        await _run_to_completion(pilot)
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        result = app.result
        assert isinstance(result, SetupResult)
        assert result.profile.model == _REFERENCE
        assert result.model_tier is None


# ---------------------------------------------------------------------------
# Tier stage (migrated)
# ---------------------------------------------------------------------------


async def test_the_tier_stage_offers_automatic_low_and_high() -> None:
    app = _Host(_FakeCatalog(auth=(_NONE_METHOD,)))
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _choose_auth_method(pilot, "none")
        await _wait_for(app, pilot, "#setup-tier", "tier stage")
        tiers = _setup_screen(app).query_one("#setup-tier", OptionList)
        labels = [str(tiers.get_option_at_index(i).prompt) for i in range(tiers.option_count)]
        assert labels == ["Automatic", "Low", "High"]
        assert tiers.highlighted == 0


@pytest.mark.parametrize("tier", ["low", "high"])
async def test_choosing_an_explicit_tier_reports_it(tier: str) -> None:
    app = _Host(_FakeCatalog())
    async with app.run_test() as pilot:
        await _run_to_completion(pilot, tier_id=tier)
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        assert isinstance(app.result, SetupResult)
        assert app.result.model_tier == tier


async def test_an_explicit_tier_survives_a_wizard_reopen() -> None:
    """An explicit `low` override is a deliberate choice — reopening the
    wizard must pre-highlight it rather than resetting to Automatic."""
    app = _Host(_FakeCatalog(auth=(_NONE_METHOD,)), current_tier="low")
    async with app.run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://endpoint.example")
        await _choose_auth_method(pilot, "none")
        await _wait_for(app, pilot, "#setup-tier", "tier stage")
        tiers = _setup_screen(app).query_one("#setup-tier", OptionList)
        assert tiers.highlighted == 1
        assert str(tiers.get_option_at_index(1).prompt) == "Low"


# ---------------------------------------------------------------------------
# Editing an existing profile
# ---------------------------------------------------------------------------


async def test_editing_seeds_the_search_the_endpoint_and_the_auth_method() -> None:
    from korvid.agent.model_profiles import ConnectionAuthConfig

    profile = ModelConnectionConfig(
        model=_REFERENCE,
        endpoint="http://kept.example",
        auth=ConnectionAuthConfig(method="environment", settings={"key": "KEPT_KEY"}),
    )
    app = _Host(_FakeCatalog(), profile=profile)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: isinstance(app.screen, ModelSearchScreen),
            label="model search screen",
        )
        assert app.screen.query_one("#model-query", Input).value == _REFERENCE
        await _pick_model(pilot)
        await _wait_for(app, pilot, "#setup-endpoint", "endpoint stage")
        assert _setup_screen(app).query_one("#setup-endpoint", Input).value == "http://kept.example"
        await pilot.press("enter")
        await _wait_for(app, pilot, "#setup-auth", "auth stage")
        methods = _setup_screen(app).query_one("#setup-auth", OptionList)
        assert methods.highlighted == _offered_method_ids(pilot).index("environment")
        await _choose_auth_method(pilot, "environment")
        await _wait_for(app, pilot, "#field-key", "auth field stage")
        assert _setup_screen(app).query_one("#field-key", Input).value == "KEPT_KEY"


async def test_an_editor_without_apply_or_save_just_hands_back_the_profile() -> None:
    """The profile manager opens this screen as an editor: it tests the
    connection, then returns the edited profile for the manager to place."""
    catalog = _FakeCatalog()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await _run_to_completion(pilot, endpoint="http://edited.example")
        await until(pilot, lambda: isinstance(app.result, SetupResult), label="wizard result")
        assert isinstance(app.result, SetupResult)
        assert app.result.profile.endpoint == "http://edited.example"
        assert len(catalog.tested) == 1


# ---------------------------------------------------------------------------
# Source-level invariant
# ---------------------------------------------------------------------------


def test_the_setup_screen_source_names_no_vendor() -> None:
    source = Path("src/korvid/ui/widgets/agent_setup_screen.py").read_text(encoding="utf-8")
    for vendor in (
        "openai",
        "anthropic",
        "azure",
        "bedrock",
        "gemini",
        "ollama",
        "copilot",
        "vllm",
        "mistral",
        "cohere",
    ):
        assert vendor not in source.lower()
