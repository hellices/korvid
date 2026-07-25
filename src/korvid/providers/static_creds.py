"""Static header credential source (API keys from environment)."""

from __future__ import annotations

from korvid.agent.credentials import CredentialSource


class StaticHeaderSource(CredentialSource):
    """A fixed secret rendered into one header, e.g. Authorization: Bearer <key>."""

    def __init__(
        self,
        value: str,
        *,
        header: str = "Authorization",
        prefix: str = "Bearer ",
    ) -> None:
        self._value = value
        self._header = header
        self._prefix = prefix

    async def headers(self) -> dict[str, str]:
        return {self._header: self._prefix + self._value}
