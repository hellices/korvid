"""Agent setup contracts — importable by ui without touching providers.

The TUI setup wizard talks to an AgentConfigurator; the concrete
ProviderConfigurator lives in korvid.providers and is injected at the
composition root (tach layer rules: ui must not import providers).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, cast

from korvid.core.config import ModelConnectionConfig, project_legacy_transport


def _freeze_option_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_option_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_option_value(item) for item in value)
    return value


def _freeze_options(options: Mapping[str, object]) -> Mapping[str, object]:
    return cast("Mapping[str, object]", _freeze_option_value(options))


#: The three routes korvid ships: an explicit tier, or automatic routing.
#: The same set `KorvidConfig` accepts for `agent.model_tier`, checked
#: again here because the wizard, a plugin and a `dataclasses.replace`
#: can all build settings without going through config parsing.
MODEL_TIERS: Final[tuple[str, ...]] = ("low", "high")


@dataclass(frozen=True)
class AgentSettings:
    provider: str
    auth_method: str  # api_key | device-login | entra | none
    base_url: str | None
    model: str
    api_key_env: str | None = None
    #: Explicit model-capability tier override: `low`, `high`, or `None`
    #: for automatic routing (replaces the old retired arm names).
    model_tier: str | None = None
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Checked before the options are frozen: everything downstream
        # treats the tier as already validated — `ModelRouter.resolve`
        # takes a non-None tier as the user's own decision and routes it
        # without falling back, and `save_agent_config` writes whatever
        # it is handed. An unroutable value would otherwise surface as a
        # policy nobody chose, reported in the header as the user's.
        if self.model_tier is not None and (
            type(self.model_tier) is not str or self.model_tier not in MODEL_TIERS
        ):
            detail = repr(self.model_tier) if type(self.model_tier) is str else "<invalid type>"
            raise ValueError(
                f"model_tier must be one of {', '.join(MODEL_TIERS)} or None for automatic "
                f"routing, got {detail}"
            )
        object.__setattr__(self, "options", _freeze_options(self.options))


@dataclass(frozen=True)
class DeviceLoginPrompt:
    user_code: str
    verification_uri: str


class AgentConfigurator(ABC):
    """Boundary contract between the ui and providers layers (AGENTS.md: ABC)."""

    @abstractmethod
    async def begin_device_login(self) -> DeviceLoginPrompt: ...

    @abstractmethod
    async def finish_device_login(self) -> None: ...

    @abstractmethod
    async def list_models(self, settings: AgentSettings) -> list[str]:
        """Models available for these settings, [] when unknown (caller falls back to input)."""

    @abstractmethod
    async def test(self, settings: AgentSettings) -> str: ...

    @abstractmethod
    async def save(self, settings: AgentSettings) -> None: ...


def settings_from_profile(profile: ModelConnectionConfig, tier: str | None) -> AgentSettings | None:
    """Project a profile onto the transport record the runtime still speaks.

    Interim: Task 15 replaces this with the profile-native provider
    factory. Every caller — the composition root's connection probe, a
    profile switch, the wizard's apply — goes through the same core
    projection startup derives its scalars from, so a profile the
    transport cannot serve is refused in exactly one place and an Azure
    deployment path is rebuilt in exactly one place.

    Args:
        profile: The connection to project.
        tier: The agent's persisted capability-tier override, or None for
            automatic routing. Not a profile field: it rides along.

    Returns:
        The settings, or None when the transport cannot serve the profile.
    """
    projection, _refusal = project_legacy_transport(profile)
    if projection is None:
        return None
    return AgentSettings(
        provider=projection.provider,
        auth_method=projection.auth_method,
        base_url=projection.base_url,
        model=projection.model,
        api_key_env=projection.api_key_env,
        model_tier=tier,
        options=projection.options,
    )


def profile_refusal(profile: ModelConnectionConfig) -> str | None:
    """Why the interim transport cannot serve *profile*, or None if it can.

    The message a refusal is reported with, so the operator is told which
    of the several reasons applies instead of always being told the model
    reference is missing a prefix.
    """
    return project_legacy_transport(profile)[1]
