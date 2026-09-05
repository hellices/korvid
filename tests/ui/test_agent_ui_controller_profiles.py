"""Profile ownership in `AgentUiController` (Task 11).

The controller opens the profile manager when profiles exist and the
setup wizard directly on a first run, activates a profile by rebuilding
*before* it persists, and keeps reading tier and follow from
`KorvidConfig` — profiles replace the transport scalars only.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
from korvid.core.config import KEEP_MODEL_TIER, KorvidConfig, ModelTierWrite
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
        #: What each call asked the writer to do with `agent.model_tier`.
        self.tiers: list[ModelTierWrite] = []
        self.error = error

    def __call__(
        self, profiles: ModelConnectionsConfig, *, model_tier: ModelTierWrite = KEEP_MODEL_TIER
    ) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(profiles)
        self.tiers.append(model_tier)


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
# The global capability tier, chosen from the manager
# ---------------------------------------------------------------------------


def _hand_back(env: Env, result: ProfileManagerResult) -> None:
    _screen, callback = env.ui.screens[-1]
    assert callback is not None
    callback(result)


async def test_the_manager_opens_on_the_persisted_tier(tmp_path: Path) -> None:
    """The screen renders the controller's tier, not a default of its own."""
    profiles = _profiles()
    env = _env(tmp_path, profiles=profiles, config=_config(profiles, agent_model_tier="high"))
    env.controller.handle_command([])
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ProfileManagerScreen)

    assert screen._current_tier == "high"


async def test_a_tier_choice_persists_the_tier_with_the_unchanged_profiles(
    tmp_path: Path,
) -> None:
    """One write, and the profiles in it are the set already on disk.

    The tier and the profiles share a writer precisely so they cannot
    disagree; sending a *different* profile set here would smuggle an
    unrelated profile edit into a tier decision.
    """
    saver = _Saver()
    profiles = _profiles()
    env = _env(tmp_path, profiles=profiles, saver=saver)
    env.controller.handle_command([])
    _hand_back(env, ProfileManagerResult(tier_changed=True, model_tier="low"))

    assert saver.tiers == ["low"]
    assert saver.calls[-1] == profiles
    assert env.controller.configured_model_tier == "low"
    assert env.controller.active_profile == "default"


async def test_choosing_automatic_clears_the_configured_tier(tmp_path: Path) -> None:
    """`None` here is Automatic, and `tier_changed` is what says so — the
    write must clear the override rather than leave the old one."""
    saver = _Saver()
    profiles = _profiles()
    env = _env(
        tmp_path,
        profiles=profiles,
        saver=saver,
        config=_config(profiles, agent_model_tier="high"),
    )
    env.controller.handle_command([])
    _hand_back(env, ProfileManagerResult(tier_changed=True, model_tier=None))

    assert saver.tiers == [None]
    assert env.controller.configured_model_tier is None


async def test_a_manager_result_without_a_tier_change_never_writes_one(
    tmp_path: Path,
) -> None:
    """Every other result must keep `KEEP_MODEL_TIER`: an edit or a switch
    that wrote a tier would overwrite the operator's override with the
    controller's copy of it."""
    saver = _Saver()
    env = _env(tmp_path, rebuild=_rebuilding(FakeSession()), saver=saver)
    env.controller.handle_command([])
    _hand_back(env, ProfileManagerResult(activated="staging"))
    _hand_back(env, ProfileManagerResult(edited=_profiles(active="default")))

    assert saver.tiers == [KEEP_MODEL_TIER, KEEP_MODEL_TIER]


async def test_a_failed_tier_save_leaves_the_configured_tier_alone(tmp_path: Path) -> None:
    """The tier is adopted from what reached the disk. Adopting it anyway
    would report a tier the next start does not have."""
    profiles = _profiles()
    env = _env(
        tmp_path,
        profiles=profiles,
        saver=_Saver(error=OSError("read-only")),
        config=_config(profiles, agent_model_tier="high"),
    )
    env.controller.handle_command([])
    _hand_back(env, ProfileManagerResult(tier_changed=True, model_tier="low"))

    assert env.controller.configured_model_tier == "high"
    assert "revert" in env.ui.notifications[-1][0]


async def test_a_tier_choice_does_not_rebuild_the_session(tmp_path: Path) -> None:
    """A tier is configuration, not a connection: changing it must not
    tear down a working provider, and must say when it takes effect."""
    rebuild = _rebuilding(FakeSession())
    env = _env(tmp_path, rebuild=rebuild, session=FakeSession())
    env.controller.handle_command([])
    _hand_back(env, ProfileManagerResult(tier_changed=True, model_tier="high"))

    assert rebuild.built == []
    message = env.ui.notifications[-1][0]
    assert "high" in message


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


def test_settings_from_profile_refuses_a_prefix_the_legacy_transport_cannot_serve() -> None:
    """Startup refuses these with a warning. A profile switch reaching the
    same prefix through a second, laxer projection would send a bearer
    token to a vendor that expects its own header."""
    profile = ModelConnectionConfig(model="anthropic/claude-sonnet-4-5")

    assert settings_from_profile(profile, None) is None


def test_settings_from_profile_refuses_a_profile_with_a_config_error() -> None:
    broken = ModelConnectionConfig(model="acme/model-x", options={"bad": object()})
    assert broken.config_error is not None  # the fixture is the precondition

    assert settings_from_profile(broken, None) is None


def test_settings_from_profile_reattaches_the_azure_deployment_path() -> None:
    """The projection has to be the one startup uses, deployment path and
    all: a `:ai` switch that drops it configures a 404."""
    from korvid.core.config import load_config

    profile = ModelConnectionConfig(
        model="azure/gpt-4o",
        endpoint="https://x.openai.azure.com",
        auth=ConnectionAuthConfig(method="environment", settings={"key": "AZURE_OPENAI_API_KEY"}),
        options={"azure_deployment": "my-dep"},
    )
    settings = settings_from_profile(profile, None)

    assert settings is not None
    assert settings.base_url == "https://x.openai.azure.com/openai/deployments/my-dep"
    assert load_config  # the startup path this must agree with


def test_settings_from_profile_matches_the_scalars_startup_derives(tmp_path: Path) -> None:
    from korvid.core.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n"
        "  active: main\n"
        "  profiles:\n"
        "    main:\n"
        "      model: azure/gpt-4o\n"
        "      endpoint: https://x.openai.azure.com\n"
        "      auth:\n"
        "        method: environment\n"
        "        key: AZURE_OPENAI_API_KEY\n"
        "      options:\n"
        "        azure_deployment: my-dep\n",
        encoding="utf-8",
    )
    startup = load_config(path)
    settings = settings_from_profile(startup.model_connections.profiles["main"], None)

    assert settings is not None
    assert settings.provider == startup.agent_provider
    assert settings.base_url == startup.agent_base_url
    assert settings.model == startup.agent_model
    assert settings.api_key_env == startup.agent_api_key_env
    assert settings.auth_method == startup.agent_auth_method


# ---------------------------------------------------------------------------
# The first-run wizard's own persistence hook
# ---------------------------------------------------------------------------


def _first_run(tmp_path: Path, **kwargs: Any) -> Env:
    return _env(tmp_path, profiles=ModelConnectionsConfig(), **kwargs)


def _pushed_setup(env: Env) -> AgentSetupScreen:
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, AgentSetupScreen)
    return screen


async def test_the_first_run_wizard_is_given_a_save_hook(tmp_path: Path) -> None:
    """Persistence has to run *inside* the wizard: a save that only
    happens after dismissal cannot keep the screen open when it fails."""
    saver = _Saver()
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    screen = _pushed_setup(env)

    save = screen._save_result
    assert save is not None
    await save(SetupResult(profile=ModelConnectionConfig(model="acme/model-x")))

    written = saver.calls[-1]
    assert written.active is not None
    assert written.profiles[written.active].model == "acme/model-x"


async def test_the_first_run_save_hook_adopts_what_it_wrote(tmp_path: Path) -> None:
    """The in-memory set is refreshed from the value handed to the writer,
    so the round-trip (`unparsed` included) stays authoritative."""
    saver = _Saver()
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    screen = _pushed_setup(env)

    save = screen._save_result
    assert save is not None
    await save(SetupResult(profile=ModelConnectionConfig(model="acme/model-x")))

    assert env.controller.profiles == saver.calls[-1]
    assert env.controller.active_profile == saver.calls[-1].active


async def test_a_failed_first_run_save_reaches_the_wizard_as_an_error(tmp_path: Path) -> None:
    """The screen renders "Applied, but save failed … will revert" only if
    the hook raises. Swallowing the error dismisses on a lie."""
    saver = _Saver(error=OSError("read-only file system"))
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    screen = _pushed_setup(env)

    save = screen._save_result
    assert save is not None
    with pytest.raises(OSError, match="read-only file system"):
        await save(SetupResult(profile=ModelConnectionConfig(model="acme/model-x")))

    assert env.controller.profiles.profiles == {}


async def test_the_first_run_result_is_persisted_once(tmp_path: Path) -> None:
    """The wizard's hook already wrote it. The dismiss callback must not
    write a second, duplicate profile beside it."""
    saver = _Saver()
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    screen, callback = env.ui.screens[-1]
    assert isinstance(screen, AgentSetupScreen)
    assert callback is not None

    result = SetupResult(profile=ModelConnectionConfig(model="acme/model-x"))
    save = screen._save_result
    assert save is not None
    await save(result)
    callback(result)

    assert len(saver.calls) == 1
    assert set(saver.calls[-1].profiles) == {"model-x"}


async def test_a_first_run_result_that_never_reached_the_hook_is_still_persisted(
    tmp_path: Path,
) -> None:
    """The callback stays the safety net: a wizard wired without the hook
    (or dismissed by another path) must not silently lose the profile."""
    saver = _Saver()
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    _screen, callback = env.ui.screens[-1]
    assert callback is not None

    callback(SetupResult(profile=ModelConnectionConfig(model="acme/model-x")))

    assert len(saver.calls) == 1


async def test_the_first_run_wizard_applies_before_it_saves(tmp_path: Path) -> None:
    """Apply-before-persist, on the production path: the screen is given
    both hooks and nothing is written until the wizard runs them."""
    saver = _Saver()
    rebuild = _rebuilding(FakeSession())
    env = _first_run(tmp_path, rebuild=rebuild, saver=saver)
    env.controller.handle_command([])
    screen = _pushed_setup(env)

    apply_result = screen._apply_result
    save = screen._save_result
    assert apply_result is not None
    assert save is not None
    assert saver.calls == []

    result = SetupResult(profile=ModelConnectionConfig(model="acme/model-x"))
    assert apply_result(result) is True
    assert rebuild.built  # applied first
    assert saver.calls == []  # and nothing persisted yet
    await save(result)
    assert len(saver.calls) == 1


async def test_the_profile_editor_is_not_given_a_save_hook(tmp_path: Path) -> None:
    """An edited profile is placed by the manager, not persisted mid-edit."""
    env = _env(tmp_path)
    env.controller.handle_command([])
    manager, _callback = env.ui.screens[-1]
    assert isinstance(manager, ProfileManagerScreen)

    task = asyncio.ensure_future(
        env.controller._edit_profile(ModelConnectionConfig(model="acme/model-x"))
    )
    await asyncio.sleep(0)
    editor = _pushed_setup(env)
    assert editor._save_result is None
    assert editor._apply_result is None

    _screen, callback = env.ui.screens[-1]
    assert callback is not None
    callback(None)
    assert await task is None


# ---------------------------------------------------------------------------
# `:model` writes through the profile set, not around it
# ---------------------------------------------------------------------------


def _model_env(tmp_path: Path, **kwargs: Any) -> Env:
    return _env(tmp_path, rebuild=_rebuilding(FakeSession()), **kwargs)


async def test_model_updates_the_active_profile_in_place_and_saves_once(tmp_path: Path) -> None:
    """One writer. `:model` used to reach a second, uncoordinated one that
    reloaded config.yaml and rewrote a `default` profile beside the one the
    operator was actually on."""
    saver = _Saver()
    profiles = _profiles(unparsed={"broken": {"raw": "value"}})
    env = _model_env(tmp_path, profiles=profiles, saver=saver)
    env.controller.handle_model_command(["model-z"])

    assert len(saver.calls) == 1
    written = saver.calls[-1]
    assert written.active == "default"
    assert written.profiles["default"].model == "acme/model-z"
    assert env.controller.profiles.profiles["default"].model == "acme/model-z"


async def test_model_leaves_every_other_profile_and_unparsed_entry_alone(tmp_path: Path) -> None:
    saver = _Saver()
    profiles = _profiles(unparsed={"broken": {"raw": "value"}})
    env = _model_env(tmp_path, profiles=profiles, saver=saver)
    env.controller.handle_model_command(["model-z"])

    written = saver.calls[-1]
    assert set(written.profiles) == {"default", "staging"}
    assert written.profiles["staging"] == profiles.profiles["staging"]
    assert dict(written.unparsed) == {"broken": {"raw": "value"}}


async def test_model_keeps_the_provider_prefix_of_the_profile_it_edits(tmp_path: Path) -> None:
    """`:model gpt-4o` changes the model, not the provider: dropping the
    prefix would make the reference unserviceable, and guessing a new one
    would silently point the profile at a different vendor."""
    saver = _Saver()
    env = _model_env(tmp_path, saver=saver)
    env.controller.handle_model_command(["model-z"])

    assert saver.calls[-1].profiles["default"].model == "acme/model-z"


async def test_model_takes_a_fully_qualified_reference_verbatim(tmp_path: Path) -> None:
    saver = _Saver()
    env = _model_env(tmp_path, saver=saver)
    env.controller.handle_model_command(["openai/gpt-4o"])

    assert saver.calls[-1].profiles["default"].model == "openai/gpt-4o"


async def test_model_preserves_the_endpoint_auth_and_options_of_the_profile(
    tmp_path: Path,
) -> None:
    saver = _Saver()
    profiles = _profiles(active="staging")
    env = _model_env(tmp_path, profiles=profiles, saver=saver)
    env.controller.handle_model_command(["model-z"])

    edited = saver.calls[-1].profiles["staging"]
    assert edited.model == "acme/model-z"
    assert edited.endpoint == "http://staging.example"
    assert edited.auth.method == "environment"
    assert dict(edited.auth.settings) == {"key": "STAGING_KEY"}


async def test_a_refused_model_swap_persists_nothing(tmp_path: Path) -> None:
    """Apply before persist: a rebuild the app refuses must not become the
    configuration a restart activates."""
    saver = _Saver()
    previous = FakeSession()
    env = _env(tmp_path, session=previous, rebuild=lambda _s: None, saver=saver)
    env.controller.handle_model_command(["model-z"])

    assert saver.calls == []
    assert env.controller.session is previous
    assert env.controller.profiles.profiles["default"].model == "acme/model-x"


async def test_a_failed_model_save_keeps_the_session_and_warns_about_the_revert(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    saver = _Saver(error=OSError("disk full"))
    env = _env(tmp_path, rebuild=_rebuilding(session), saver=saver)
    env.controller.handle_model_command(["model-z"])

    assert env.controller.session is session
    message = env.ui.notifications[-1][0]
    assert "disk full" in message
    assert "revert" in message.lower()
    # The write failed, so the in-memory set must still be what is on disk.
    assert env.controller.profiles.profiles["default"].model == "acme/model-x"


async def test_model_refuses_a_reference_the_transport_cannot_serve(tmp_path: Path) -> None:
    saver = _Saver()
    session = FakeSession()
    env = _env(tmp_path, session=session, rebuild=_rebuilding(FakeSession()), saver=saver)
    env.controller.handle_model_command(["anthropic/claude-sonnet-4-5"])

    assert saver.calls == []
    assert env.controller.session is session
    assert env.controller.profiles.profiles["default"].model == "acme/model-x"


async def test_model_on_a_legacy_startup_creates_the_default_profile(tmp_path: Path) -> None:
    """A config.yaml still holding the pre-profile scalars has no profile
    to edit. `:model` has to be the whole recovery, so it files the
    configuration korvid already has as a profile instead of asking the
    operator to re-run the wizard."""
    saver = _Saver()
    legacy = KorvidConfig(
        namespace="default",
        agent_enabled=True,
        agent_provider="ollama",
        agent_auth_method="api_key",
        agent_api_key_env="OLLAMA_KEY",
        agent_base_url="http://localhost:11434/v1",
        agent_model="llama3",
    )
    env = _env(
        tmp_path,
        profiles=ModelConnectionsConfig(),
        rebuild=_rebuilding(FakeSession()),
        saver=saver,
        config=legacy,
    )
    env.controller.handle_model_command(["llama3.2"])

    written = saver.calls[-1]
    assert written.active == "default"
    created = written.profiles["default"]
    assert created.model == "ollama/llama3.2"
    assert created.endpoint == "http://localhost:11434/v1"
    # The profile vocabulary, not the transport's: a profile written with
    # `api_key` would be refused by the projection on the next start.
    assert created.auth.method == "environment"
    assert dict(created.auth.settings) == {"key": "OLLAMA_KEY"}


async def test_model_without_any_configuration_still_asks_for_the_wizard(tmp_path: Path) -> None:
    saver = _Saver()
    env = _env(tmp_path, profiles=ModelConnectionsConfig(), saver=saver)
    env.controller.handle_model_command(["model-z"])

    assert saver.calls == []
    assert "run :ai first" in env.ui.notifications[-1][0]


# ---------------------------------------------------------------------------
# The first-run tier is persisted with the profiles, in one write
# ---------------------------------------------------------------------------


async def test_the_first_run_save_writes_the_chosen_tier_with_the_profiles(
    tmp_path: Path,
) -> None:
    """The wizard's tier answer reaches disk in the same write as the
    profile it belongs to — a tier applied to the live session but never
    written is silently lost at the next start."""
    saver = _Saver()
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    screen = _pushed_setup(env)

    save = screen._save_result
    assert save is not None
    await save(SetupResult(profile=ModelConnectionConfig(model="acme/model-x"), model_tier="high"))

    assert saver.tiers == ["high"]
    assert len(saver.calls) == 1  # one write, not a profile save plus a tier save


async def test_the_first_run_save_clears_a_stale_tier_when_automatic_is_chosen(
    tmp_path: Path,
) -> None:
    """Automatic is `None`, and it has to actually clear the override on
    disk: leaving `high` behind resets the wizard's own tier step next
    time to a choice the operator just rejected."""
    saver = _Saver()
    env = _first_run(
        tmp_path, saver=saver, config=_config(ModelConnectionsConfig(), agent_model_tier="high")
    )
    env.controller.handle_command([])
    screen = _pushed_setup(env)

    save = screen._save_result
    assert save is not None
    await save(SetupResult(profile=ModelConnectionConfig(model="acme/model-x"), model_tier=None))

    assert saver.tiers == [None]
    assert len(saver.calls) == 1


async def test_a_first_run_result_that_never_reached_the_hook_still_carries_its_tier(
    tmp_path: Path,
) -> None:
    saver = _Saver()
    env = _first_run(tmp_path, saver=saver)
    env.controller.handle_command([])
    _screen, callback = env.ui.screens[-1]
    assert callback is not None

    callback(SetupResult(profile=ModelConnectionConfig(model="acme/model-x"), model_tier="low"))

    assert saver.tiers == ["low"]


async def test_a_profile_edit_save_leaves_the_persisted_tier_alone(tmp_path: Path) -> None:
    """The manager never asks about the tier, so its save must not be able
    to overwrite one — it hands the writer the "keep" sentinel."""
    saver = _Saver()
    env = _env(tmp_path, saver=saver, config=_config(_profiles(), agent_model_tier="high"))
    env.controller.handle_command([])
    _manager, callback = env.ui.screens[-1]
    assert callback is not None
    edited = dataclasses.replace(
        env.controller.profiles,
        profiles={
            **env.controller.profiles.profiles,
            "default": ModelConnectionConfig(model="acme/model-z"),
        },
    )
    callback(ProfileManagerResult(edited=edited))

    assert saver.tiers == [KEEP_MODEL_TIER]


async def test_activating_a_profile_leaves_the_persisted_tier_alone(tmp_path: Path) -> None:
    saver = _Saver()
    profiles = _profiles()
    env = _env(
        tmp_path,
        profiles=profiles,
        saver=saver,
        rebuild=_rebuilding(FakeSession()),
        config=_config(profiles, agent_model_tier="high"),
    )
    env.controller.handle_command([])
    _activate(env, "staging")

    assert saver.tiers == [KEEP_MODEL_TIER]


async def test_the_model_command_leaves_the_persisted_tier_alone(tmp_path: Path) -> None:
    """`:model` changes a model, not a routing tier."""
    saver = _Saver()
    profiles = _profiles()
    env = _model_env(
        tmp_path,
        profiles=profiles,
        saver=saver,
        config=_config(profiles, agent_model_tier="low"),
    )
    env.controller.handle_model_command(["model-z"])

    assert saver.tiers == [KEEP_MODEL_TIER]
