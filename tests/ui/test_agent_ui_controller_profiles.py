"""Profile ownership in `AgentUiController` (Task 11).

The controller opens the profile manager when profiles exist and the
setup wizard directly on a first run, activates a profile by rebuilding
*before* it persists, and keeps reading tier and follow from
`KorvidConfig` — profiles replace the transport scalars only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    ConnectionAuthConfig,
    EndpointRequirement,
    ModelCatalog,
    ModelConnectionConfig,
    ModelConnectionsConfig,
    ModelEntry,
    SetupField,
)
from korvid.agent.session import AgentSession
from korvid.agent.setup import AgentSettings
from korvid.core.config import KorvidConfig
from korvid.ui.agent_ui_controller import settings_from_profile
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen, SetupResult
from korvid.ui.widgets.profile_manager_screen import ProfileManagerResult, ProfileManagerScreen

from .agent_session_fakes import FakeSession
from .test_agent_ui_controller import Env


class _StubCatalog(ModelCatalog):
    """Enough catalog to open a screen; the screens' own tests drive it."""

    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        return ()

    def entry(self, reference: str) -> ModelEntry | None:
        return None

    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]:
        return ()

    def option_fields(self, reference: str) -> tuple[SetupField, ...]:
        return ()

    def endpoint_requirement(self, reference: str) -> EndpointRequirement:
        return EndpointRequirement.OPTIONAL

    async def discover(self, profile: ModelConnectionConfig) -> tuple[ModelEntry, ...]:
        return ()

    async def test(self, profile: ModelConnectionConfig) -> str:
        return "ok"

    async def begin_auth(self, profile: ModelConnectionConfig) -> None:
        return None

    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None:
        return None


def _profiles(active: str | None = "default", **extra: object) -> ModelConnectionsConfig:
    profiles: dict[str, ModelConnectionConfig] = {
        "default": ModelConnectionConfig(model="acme/model-x"),
        "staging": ModelConnectionConfig(
            model="acme/model-y",
            endpoint="http://staging.example",
            auth=ConnectionAuthConfig(method="environment", settings={"key": "STAGING_KEY"}),
        ),
    }
    return ModelConnectionsConfig(active=active, profiles=profiles, **extra)  # type: ignore[arg-type]  # unparsed only


def _config(profiles: ModelConnectionsConfig, **overrides: Any) -> KorvidConfig:
    return KorvidConfig(namespace="default", model_connections=profiles, **overrides)


class _Saver:
    """Records what the controller asked to persist."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[ModelConnectionsConfig] = []
        self.error = error

    def __call__(self, profiles: ModelConnectionsConfig) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(profiles)


def _rebuilding(session: AgentSession | None) -> Any:
    built: list[AgentSettings] = []

    def _rebuild(settings: AgentSettings) -> AgentSession | None:
        built.append(settings)
        return session

    _rebuild.built = built  # type: ignore[attr-defined]  # test-only recorder
    return _rebuild


def _env(
    tmp_path: Path,
    *,
    profiles: ModelConnectionsConfig | None = None,
    session: AgentSession | None = None,
    rebuild: Any = None,
    saver: _Saver | None = None,
    catalog: Any = "stub",
    profile_settings: Any = None,
    config: KorvidConfig | None = None,
) -> Env:
    resolved = profiles if profiles is not None else _profiles()
    return Env(
        tmp_path=tmp_path,
        session=session,
        config=config if config is not None else _config(resolved),
        configurator=object(),
        rebuild=rebuild,
        catalog=_StubCatalog() if catalog == "stub" else catalog,
        save_profiles=saver if saver is not None else _Saver(),
        profile_settings=profile_settings,
    )


# ---------------------------------------------------------------------------
# Which screen `:ai` opens
# ---------------------------------------------------------------------------


async def test_the_controller_opens_the_profile_manager_when_profiles_exist(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.controller.handle_command([])
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ProfileManagerScreen)


async def test_the_controller_opens_setup_directly_when_no_profile_exists(tmp_path: Path) -> None:
    """A first run must not show an empty list with nothing to pick."""
    env = _env(tmp_path, profiles=ModelConnectionsConfig())
    env.controller.handle_command([])
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, AgentSetupScreen)


async def test_an_unparsed_only_profile_set_still_opens_the_manager(tmp_path: Path) -> None:
    """An entry korvid could not parse is still something to repair — the
    manager is where it is visible, so it must not be skipped."""
    env = _env(
        tmp_path,
        profiles=ModelConnectionsConfig(unparsed={"broken": {"no": "model"}}),
    )
    env.controller.handle_command([])
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ProfileManagerScreen)


def test_a_missing_agent_extra_reports_the_install_hint_and_does_not_crash(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, catalog=None)
    env.controller.handle_command([])
    assert env.ui.screens == []
    message, severity = env.ui.notifications[-1]
    assert "Agent setup unavailable" in message
    assert severity == "warning"
    assert env.ui.notification_markup[-1] is False


# ---------------------------------------------------------------------------
# Activation: apply before persist
# ---------------------------------------------------------------------------


def _activate(env: Env, name: str) -> None:
    _screen, callback = env.ui.screens[-1]
    assert callback is not None
    callback(ProfileManagerResult(activated=name))


async def test_activating_a_profile_rebuilds_and_persists_only_the_pointer(tmp_path: Path) -> None:
    saver = _Saver()
    rebuild = _rebuilding(FakeSession())
    env = _env(tmp_path, rebuild=rebuild, saver=saver)
    env.controller.handle_command([])
    _activate(env, "staging")

    assert rebuild.built  # the provider was built first
    written = saver.calls[-1]
    assert written.active == "staging"
    assert set(written.profiles) == {"default", "staging"}
    assert env.controller.active_profile == "staging"


async def test_activation_hands_the_factory_the_profile_it_named(tmp_path: Path) -> None:
    rebuild = _rebuilding(FakeSession())
    env = _env(tmp_path, rebuild=rebuild)
    env.controller.handle_command([])
    _activate(env, "staging")

    built = rebuild.built[-1]
    assert built.model == "model-y"
    assert built.base_url == "http://staging.example"
    assert built.api_key_env == "STAGING_KEY"


async def test_a_refused_rebuild_keeps_the_previous_session_and_pointer(tmp_path: Path) -> None:
    """Apply-before-persist: a profile that cannot build a provider must
    not become the persisted active one."""
    saver = _Saver()
    previous = FakeSession()
    env = _env(tmp_path, session=previous, rebuild=lambda _s: None, saver=saver)
    env.controller.handle_command([])
    _activate(env, "staging")

    assert env.controller.active_profile == "default"
    assert env.controller.session is previous
    assert saver.calls == []


async def test_a_profile_with_a_config_error_is_never_handed_to_the_factory(tmp_path: Path) -> None:
    handed: list[ModelConnectionConfig] = []

    def _factory(profile: ModelConnectionConfig, tier: str | None) -> AgentSettings | None:
        handed.append(profile)
        return settings_from_profile(profile, tier)

    broken = ModelConnectionConfig(model="acme/model-z", options={"bad": object()})
    assert broken.config_error is not None  # the fixture is the precondition
    profiles = ModelConnectionsConfig(
        active="default",
        profiles={"default": ModelConnectionConfig(model="acme/model-x"), "broken": broken},
    )
    saver = _Saver()
    env = _env(
        tmp_path,
        profiles=profiles,
        rebuild=_rebuilding(FakeSession()),
        saver=saver,
        profile_settings=_factory,
    )
    env.controller.handle_command([])
    _activate(env, "broken")

    assert handed == []
    assert saver.calls == []
    assert env.controller.active_profile == "default"
    assert "invalid" in env.ui.notifications[-1][0].lower()


async def test_a_profile_without_a_provider_prefix_is_refused_not_guessed(tmp_path: Path) -> None:
    profiles = ModelConnectionsConfig(
        active="default",
        profiles={
            "default": ModelConnectionConfig(model="acme/model-x"),
            "bare": ModelConnectionConfig(model="model-without-prefix"),
        },
    )
    saver = _Saver()
    env = _env(tmp_path, profiles=profiles, rebuild=_rebuilding(FakeSession()), saver=saver)
    env.controller.handle_command([])
    _activate(env, "bare")

    assert saver.calls == []
    assert env.controller.active_profile == "default"


async def test_a_failed_persist_reports_it_and_keeps_the_applied_session(tmp_path: Path) -> None:
    """The swap already happened: the operator must be told the pointer
    will revert on restart rather than left believing it was saved."""
    session = FakeSession()
    saver = _Saver(error=OSError("read-only file system"))
    env = _env(tmp_path, rebuild=_rebuilding(session), saver=saver)
    env.controller.handle_command([])
    _activate(env, "staging")

    assert env.controller.session is session
    assert "revert" in env.ui.notifications[-1][0].lower()


# ---------------------------------------------------------------------------
# Editing the profile set
# ---------------------------------------------------------------------------


async def test_an_edited_profile_set_is_persisted_verbatim(tmp_path: Path) -> None:
    saver = _Saver()
    env = _env(tmp_path, saver=saver)
    env.controller.handle_command([])
    _screen, callback = env.ui.screens[-1]
    assert callback is not None
    edited = ModelConnectionsConfig(
        active="default",
        profiles={"default": ModelConnectionConfig(model="acme/model-new")},
        unparsed={"broken": {"raw": "value"}},
    )
    callback(ProfileManagerResult(edited=edited))

    written = saver.calls[-1]
    assert written.profiles["default"].model == "acme/model-new"
    assert dict(written.unparsed) == {"broken": {"raw": "value"}}
    assert env.controller.profiles.profiles["default"].model == "acme/model-new"


async def test_an_unparsed_entry_survives_an_activation(tmp_path: Path) -> None:
    """The round-trip stays authoritative: switching profiles must not
    drop an entry the operator still has to repair."""
    profiles = _profiles(unparsed={"broken": {"raw": "value"}})
    saver = _Saver()
    env = _env(tmp_path, profiles=profiles, rebuild=_rebuilding(FakeSession()), saver=saver)
    env.controller.handle_command([])
    _activate(env, "staging")

    assert dict(saver.calls[-1].unparsed) == {"broken": {"raw": "value"}}


async def test_a_cancelled_manager_changes_nothing(tmp_path: Path) -> None:
    saver = _Saver()
    env = _env(tmp_path, saver=saver)
    env.controller.handle_command([])
    _screen, callback = env.ui.screens[-1]
    assert callback is not None
    callback(None)

    assert saver.calls == []
    assert env.controller.active_profile == "default"


# ---------------------------------------------------------------------------
# What profiles do *not* replace
# ---------------------------------------------------------------------------


async def test_the_configured_tier_survives_a_profile_switch(tmp_path: Path) -> None:
    """`_configured_tier`, not the wizard's draft: the controller's is the
    persisted choice and a switch must not reset it."""
    profiles = _profiles()
    env = _env(
        tmp_path,
        profiles=profiles,
        rebuild=_rebuilding(FakeSession()),
        config=_config(profiles, agent_model_tier="high"),
    )
    env.controller.handle_command([])
    _activate(env, "staging")

    assert env.controller.configured_model_tier == "high"


async def test_agent_model_tier_and_agent_follow_still_come_from_settings(tmp_path: Path) -> None:
    """Profiles replace the transport scalars only. The controller keeps
    reading KorvidConfig for tier and follow."""
    profiles = _profiles()
    env = _env(
        tmp_path,
        profiles=profiles,
        config=_config(profiles, agent_model_tier="low", agent_follow=False),
    )
    assert env.controller.configured_model_tier == "low"
    assert env.controller.follow_enabled is False


async def test_the_tier_travels_to_the_factory_with_the_profile(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def _factory(profile: ModelConnectionConfig, tier: str | None) -> AgentSettings | None:
        seen.append(tier)
        return settings_from_profile(profile, tier)

    profiles = _profiles()
    env = _env(
        tmp_path,
        profiles=profiles,
        rebuild=_rebuilding(FakeSession()),
        profile_settings=_factory,
        config=_config(profiles, agent_model_tier="high"),
    )
    env.controller.handle_command([])
    _activate(env, "staging")

    assert seen == ["high"]


# ---------------------------------------------------------------------------
# The wizard's own result
# ---------------------------------------------------------------------------


async def test_a_completed_wizard_persists_the_new_profile_as_the_active_one(
    tmp_path: Path,
) -> None:
    saver = _Saver()
    env = _env(tmp_path, profiles=ModelConnectionsConfig(), saver=saver)
    env.controller.handle_command([])
    screen, callback = env.ui.screens[-1]
    assert isinstance(screen, AgentSetupScreen)
    assert callback is not None
    callback(SetupResult(profile=ModelConnectionConfig(model="acme/model-x"), model_tier="low"))

    written = saver.calls[-1]
    assert written.active is not None
    assert written.profiles[written.active].model == "acme/model-x"


async def test_a_cancelled_wizard_persists_nothing(tmp_path: Path) -> None:
    saver = _Saver()
    env = _env(tmp_path, profiles=ModelConnectionsConfig(), saver=saver)
    env.controller.handle_command([])
    _screen, callback = env.ui.screens[-1]
    assert callback is not None
    callback(None)

    assert saver.calls == []


# ---------------------------------------------------------------------------
# The interim factory
# ---------------------------------------------------------------------------


def test_settings_from_profile_never_carries_a_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile stores a variable *name*; resolution belongs to the
    provider factory, not to anything that renders or persists."""
    monkeypatch.setenv("SOME_KEY", "sk-secret-value")
    profile = ModelConnectionConfig(
        model="acme/model-x",
        auth=ConnectionAuthConfig(method="environment", settings={"key": "SOME_KEY"}),
    )
    settings = settings_from_profile(profile, None)

    assert settings is not None
    assert settings.api_key_env == "SOME_KEY"
    assert "sk-secret-value" not in repr(settings)


def test_settings_from_profile_refuses_a_reference_without_a_provider() -> None:
    assert settings_from_profile(ModelConnectionConfig(model="bare"), None) is None
