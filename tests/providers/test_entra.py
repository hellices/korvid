import builtins
import re
from types import SimpleNamespace

import pytest

from korvid import __version__
from korvid.providers.entra import ENTRA_SCOPE, EntraCredentialSource


class FakeCredential:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.expires_on = 9_999_999_999

    async def get_token(self, scope: str) -> SimpleNamespace:
        self.calls.append(scope)
        return SimpleNamespace(token=f"tok-{len(self.calls)}", expires_on=self.expires_on)

    async def close(self) -> None:
        pass


async def test_headers_and_scope() -> None:
    cred = FakeCredential()
    src = EntraCredentialSource(credential=cred, clock=lambda: 1000.0)
    assert (await src.headers())["Authorization"] == "Bearer tok-1"
    assert cred.calls == [ENTRA_SCOPE]


async def test_token_cached_until_expiry_window() -> None:
    cred = FakeCredential()
    now = {"t": 1000.0}
    src = EntraCredentialSource(credential=cred, clock=lambda: now["t"])
    await src.headers()
    await src.headers()
    assert len(cred.calls) == 1
    cred.expires_on = 9_999_999_999
    now["t"] = 9_999_999_999 - 100  # inside 300s refresh margin
    await src.headers()
    assert len(cred.calls) == 2


async def test_concurrent_headers_single_token_request() -> None:
    import asyncio

    class SlowCredential(FakeCredential):
        async def get_token(self, scope: str) -> SimpleNamespace:
            self.calls.append(scope)
            await asyncio.sleep(0.01)
            return SimpleNamespace(token="tok", expires_on=9_999_999_999)

    cred = SlowCredential()
    src = EntraCredentialSource(credential=cred, clock=lambda: 1000.0)
    await asyncio.gather(src.headers(), src.headers(), src.headers())
    assert len(cred.calls) == 1


async def test_lazy_import_failure_names_entra_extra_and_isolated_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "azure.identity.aio":
            raise ImportError("azure.identity.aio is unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    requirement = f"korvid[all,entra]=={__version__}"
    with pytest.raises(
        RuntimeError,
        match=(
            r"Entra auth requires isolated extras.*"
            r"including Entra.*"
            rf"uv tool install --force '{re.escape(requirement)}'.*"
            rf"pipx install --force '{re.escape(requirement)}'"
        ),
    ) as excinfo:
        await EntraCredentialSource(clock=lambda: 1000.0).headers()

    assert "pip install" not in str(excinfo.value)
