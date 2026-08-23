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
from typing import cast


def _freeze_option_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_option_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_option_value(item) for item in value)
    return value


def _freeze_options(options: Mapping[str, object]) -> Mapping[str, object]:
    return cast("Mapping[str, object]", _freeze_option_value(options))


@dataclass(frozen=True)
class AgentSettings:
    provider: str
    auth_method: str  # api_key | device-login | entra | none
    base_url: str | None
    model: str
    api_key_env: str | None = None
    #: Model-capability profile (issue #71): `full` or `small`.
    profile: str = "full"
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
