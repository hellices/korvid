"""CredentialSource ABC — pluggable auth boundary for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class CredentialSource(ABC):
    """Supplies per-request auth headers; may refresh tokens internally."""

    @abstractmethod
    async def headers(self) -> dict[str, str]:
        """Return headers to attach to the next provider request."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release owned resources (HTTP clients etc). Default: no-op."""
