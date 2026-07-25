"""Agent setup contracts — importable by ui without touching providers.

The TUI setup wizard talks to an AgentConfigurator; the concrete
ProviderConfigurator lives in korvid.providers and is injected at the
composition root (tach layer rules: ui must not import providers).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    provider: str
    auth_method: str  # api_key | device-login | entra | none
    base_url: str | None
    model: str
    api_key_env: str | None = None


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
    async def test(self, settings: AgentSettings) -> str: ...

    @abstractmethod
    async def save(self, settings: AgentSettings) -> None: ...
