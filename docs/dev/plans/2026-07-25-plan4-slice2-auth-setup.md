# Plan 4 Slice 2 — Multi-Auth Credentials + TUI Setup Wizard

**Status: COMPLETE** — all 9 tasks implemented on branch `agent-auth-slice2`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users connect korvid's agent to any provider × auth-method combination (GitHub Copilot device-login, Entra/AI Foundry, static API key, none) and configure it entirely inside the TUI via a `:ai` setup wizard.

**Architecture:** A `CredentialSource` ABC in `korvid.agent` supplies per-request auth headers asynchronously; concrete sources live in `korvid.providers` (static key, GitHub Copilot device flow with short-lived token refresh, Entra via azure-identity). The TUI wizard talks to an `AgentConfigurator` ABC defined in `korvid.agent`; the concrete configurator and all wiring stay at the composition root (`__main__.py`), preserving tach layers (`ui` never imports `providers`).

**Tech Stack:** Python 3.11+, httpx (SSE + device flow), keyring (token storage), azure-identity (optional extra), Textual ModalScreen.

## Global Constraints

- tach layers (AGENTS.md 42-51): `korvid.providers` depends only on `korvid.agent`; `korvid.ui` depends on `korvid.core`, `korvid.agent`, `korvid.k8s`. Never add `ui → providers` or `providers → core`.
- Composition root is `src/korvid/__main__.py` — the only place concrete providers/configurators are constructed.
- ruff S101: no `assert` in `src/` (tests OK). mypy --strict. Run `make check` before every commit.
- pytest needs `-p no:tach`.
- Commit trailer on every commit: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- GitHub Copilot chat endpoint is **unofficial** (`api.github.com/copilot_internal/v2/token` → `api.githubcopilot.com`); README must state this risk verbatim: "GitHub Copilot support uses an unofficial internal API that may change or break without notice."
- Device flow client id: `Iv1.b507a08c87ecfe98`. Copilot chat base URL: `https://api.githubcopilot.com`. Entra scope: `https://cognitiveservices.azure.com/.default`.
- Credentials file fallback: `~/.config/korvid/credentials.json`, mode `0600`.
- Auth method names (config + code, exact strings): `api_key`, `device-login`, `entra`, `none`.

---

### Task 1: CredentialSource ABC + provider refactor

**Files:**
- Create: `src/korvid/agent/credentials.py`
- Create: `src/korvid/providers/static_creds.py`
- Modify: `src/korvid/providers/openai_compat.py` (replace `api_key`/`auth_header` params with `credentials`)
- Modify: `src/korvid/providers/registry.py` (build `StaticHeaderSource`)
- Test: `tests/agent/test_credentials.py`, update `tests/providers/test_openai_compat.py`, `tests/providers/test_registry.py`

**Interfaces:**
- Produces: `CredentialSource` ABC — `async def headers(self) -> dict[str, str]`, `async def aclose(self) -> None` (no-op default). `StaticHeaderSource(value, *, header="Authorization", prefix="Bearer ")`.
- Produces: `OpenAICompatProvider(base_url, model, credentials: CredentialSource | None = None, client=None)`; `_headers` becomes `async`; `aclose()` also closes credentials.

- [x] **Step 1: Write failing tests**

```python
# tests/agent/test_credentials.py
from korvid.providers.static_creds import StaticHeaderSource


async def test_static_bearer_header() -> None:
    src = StaticHeaderSource("sk-1")
    assert await src.headers() == {"Authorization": "Bearer sk-1"}


async def test_static_custom_header_no_prefix() -> None:
    src = StaticHeaderSource("k", header="api-key", prefix="")
    assert await src.headers() == {"api-key": "k"}
```

In `tests/providers/test_openai_compat.py`, replace `api_key=...` usages: the `_provider` helper builds `OpenAICompatProvider(base_url=..., model=..., credentials=StaticHeaderSource("k"), client=...)`, and the header assertion test checks the sent request carries `Authorization: Bearer k`. Add one test that `credentials=None` sends no Authorization header.

- [x] **Step 2: Run tests, verify FAIL** — `uv run pytest tests/agent/test_credentials.py -p no:tach -x` fails with ImportError.

- [x] **Step 3: Implement**

```python
# src/korvid/agent/credentials.py
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
```

```python
# src/korvid/providers/static_creds.py
"""Static header credential source (API keys from environment)."""

from __future__ import annotations

from korvid.agent.credentials import CredentialSource


class StaticHeaderSource(CredentialSource):
    """A fixed secret rendered into one header, e.g. Authorization: Bearer <key>."""

    def __init__(self, value: str, *, header: str = "Authorization", prefix: str = "Bearer ") -> None:
        self._value = value
        self._header = header
        self._prefix = prefix

    async def headers(self) -> dict[str, str]:
        return {self._header: self._prefix + self._value}
```

In `openai_compat.py`: `__init__(self, base_url, model, credentials: CredentialSource | None = None, client=None)`; delete `_api_key`/`_auth_header`; `_headers` becomes `async def _headers(self) -> dict[str, str]: return await self._credentials.headers() if self._credentials else {}`; the request call site becomes `headers=await self._headers()`; `aclose()` additionally `await self._credentials.aclose()` when set (nested finally so both run). In `registry.py`, build `api_key and StaticHeaderSource(api_key, header="api-key", prefix="") if name == "azure" else StaticHeaderSource(api_key)` and pass `credentials=`.

- [x] **Step 4: `make check` green** (fix any fallout in existing tests).
- [x] **Step 5: Commit** — `feat: CredentialSource ABC; providers take credential sources`

---

### Task 2: TokenStore (keyring + 0600 file fallback)

**Files:**
- Create: `src/korvid/providers/token_store.py`
- Modify: `pyproject.toml` (add `keyring>=25` to dependencies)
- Test: `tests/providers/test_token_store.py`

**Interfaces:**
- Produces: `TokenStore(fallback_path: Path | None = None)` with sync `save(key, value)`, `load(key) -> str | None`, `delete(key)`. Keyring service name `"korvid"`. Fallback JSON file defaults to `~/.config/korvid/credentials.json`.

- [x] **Step 1: Failing tests**

```python
# tests/providers/test_token_store.py
import json
import stat
import sys
import types
from pathlib import Path

from korvid.providers.token_store import TokenStore


def _no_keyring(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # import keyring -> ImportError


def test_file_fallback_roundtrip(tmp_path: Path, monkeypatch) -> None:
    _no_keyring(monkeypatch)
    store = TokenStore(fallback_path=tmp_path / "creds.json")
    store.save("github-oauth", "gho_x")
    assert store.load("github-oauth") == "gho_x"
    store.delete("github-oauth")
    assert store.load("github-oauth") is None


def test_file_mode_0600(tmp_path: Path, monkeypatch) -> None:
    _no_keyring(monkeypatch)
    p = tmp_path / "creds.json"
    TokenStore(fallback_path=p).save("k", "v")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_keyring_preferred(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, str] = {}
    fake = types.SimpleNamespace(
        set_password=lambda svc, k, v: calls.__setitem__(f"{svc}/{k}", v),
        get_password=lambda svc, k: calls.get(f"{svc}/{k}"),
        delete_password=lambda svc, k: calls.pop(f"{svc}/{k}", None),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    p = tmp_path / "creds.json"
    store = TokenStore(fallback_path=p)
    store.save("k", "v")
    assert store.load("k") == "v"
    assert not p.exists()  # keyring used, no file written


def test_keyring_error_falls_back_to_file(tmp_path: Path, monkeypatch) -> None:
    def boom(*a: object) -> None:
        raise RuntimeError("no backend")

    fake = types.SimpleNamespace(set_password=boom, get_password=boom, delete_password=boom)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = TokenStore(fallback_path=tmp_path / "creds.json")
    store.save("k", "v")
    assert store.load("k") == "v"
```

- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement**

```python
# src/korvid/providers/token_store.py
"""OS-keyring-backed token storage with a 0600 JSON file fallback."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_SERVICE = "korvid"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "korvid" / "credentials.json"


class TokenStore:
    def __init__(self, fallback_path: Path | None = None) -> None:
        self._path = fallback_path or DEFAULT_CREDENTIALS_PATH

    def save(self, key: str, value: str) -> None:
        try:
            import keyring

            keyring.set_password(_SERVICE, key, value)
            return
        except Exception:
            logger.debug("keyring unavailable; using file fallback", exc_info=True)
        data = self._read_file()
        data[key] = value
        self._write_file(data)

    def load(self, key: str) -> str | None:
        try:
            import keyring

            value = keyring.get_password(_SERVICE, key)
            if value is not None:
                return value
        except Exception:
            logger.debug("keyring unavailable; using file fallback", exc_info=True)
        return self._read_file().get(key)

    def delete(self, key: str) -> None:
        try:
            import keyring

            keyring.delete_password(_SERVICE, key)
        except Exception:
            logger.debug("keyring delete failed or unavailable", exc_info=True)
        data = self._read_file()
        if key in data:
            del data[key]
            self._write_file(data)

    def _read_file(self) -> dict[str, str]:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

    def _write_file(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        os.chmod(self._path, 0o600)
        self._path.write_text(json.dumps(data))
```

Note: `monkeypatch.setitem(sys.modules, "keyring", None)` makes `import keyring` raise ImportError — caught by the blanket `except Exception`.

- [x] **Step 4: `make check` green** (keyring dep added via `uv add keyring`).
- [x] **Step 5: Commit** — `feat: TokenStore with keyring and 0600 file fallback`

---

### Task 3: GitHub device flow + Copilot credential source

**Files:**
- Create: `src/korvid/providers/github_copilot.py`
- Test: `tests/providers/test_github_copilot.py`

**Interfaces:**
- Produces: `DeviceCodePrompt` frozen dataclass `(user_code, verification_uri, device_code, interval, expires_in)`; `DeviceLoginError(Exception)`; `GitHubDeviceFlow(client=None)` with `async start() -> DeviceCodePrompt` and `async poll(prompt) -> str` (OAuth token); `CopilotCredentialSource(oauth_token, client=None, clock=time.time)` implementing `CredentialSource`; constants `COPILOT_CHAT_BASE_URL = "https://api.githubcopilot.com"`, `GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"`.

- [x] **Step 1: Failing tests** (httpx.MockTransport; see full behaviors below)

```python
# tests/providers/test_github_copilot.py — key cases
import json
import httpx
import pytest

from korvid.providers.github_copilot import (
    CopilotCredentialSource,
    DeviceLoginError,
    GitHubDeviceFlow,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_start_returns_prompt() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "device_code": "d", "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5, "expires_in": 900,
        })

    prompt = await GitHubDeviceFlow(client=_client(handler)).start()
    assert prompt.user_code == "ABCD-1234"


async def test_poll_pending_then_token(monkeypatch) -> None:
    responses = iter([
        {"error": "authorization_pending"},
        {"access_token": "gho_tok", "token_type": "bearer"},
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    flow = GitHubDeviceFlow(client=_client(handler))
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("korvid.providers.github_copilot.asyncio.sleep", fake_sleep)
    from korvid.providers.github_copilot import DeviceCodePrompt
    prompt = DeviceCodePrompt("u", "https://x", "d", 1, 900)
    assert await flow.poll(prompt) == "gho_tok"
    assert sleeps == [1]


async def test_poll_denied_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "access_denied"})

    from korvid.providers.github_copilot import DeviceCodePrompt
    with pytest.raises(DeviceLoginError):
        await GitHubDeviceFlow(client=_client(handler)).poll(DeviceCodePrompt("u", "x", "d", 0, 900))


async def test_copilot_source_exchanges_and_caches() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": f"cop-{calls['n']}", "expires_at": 9_999_999_999})

    src = CopilotCredentialSource("gho_x", client=_client(handler), clock=lambda: 1000.0)
    h1 = await src.headers()
    h2 = await src.headers()
    assert h1["Authorization"] == "Bearer cop-1"
    assert h1["Copilot-Integration-Id"] == "vscode-chat"
    assert "Editor-Version" in h1
    assert calls["n"] == 1  # cached
    assert h2 == h1


async def test_copilot_source_refreshes_when_expired() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": f"cop-{calls['n']}", "expires_at": 1100})

    now = {"t": 1000.0}
    src = CopilotCredentialSource("gho_x", client=_client(handler), clock=lambda: now["t"])
    await src.headers()
    now["t"] = 1090.0  # inside 60s refresh window before 1100
    await src.headers()
    assert calls["n"] == 2


async def test_copilot_exchange_failure_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad token"})

    src = CopilotCredentialSource("gho_bad", client=_client(handler))
    with pytest.raises(DeviceLoginError):
        await src.headers()
```

- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement**

```python
# src/korvid/providers/github_copilot.py
"""GitHub Copilot auth: device flow login + short-lived chat token refresh.

UNOFFICIAL API: token exchange uses api.github.com/copilot_internal/v2/token
and chat uses api.githubcopilot.com — both may change without notice.
Requires an active GitHub Copilot subscription.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from korvid.agent.credentials import CredentialSource

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot plugin OAuth app
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - URL, not a secret
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"  # noqa: S105
COPILOT_CHAT_BASE_URL = "https://api.githubcopilot.com"
_REFRESH_MARGIN_S = 60.0
_JSON_ACCEPT = {"Accept": "application/json"}


class DeviceLoginError(Exception):
    """Device login failed, expired, or the Copilot token exchange was rejected."""


@dataclass(frozen=True)
class DeviceCodePrompt:
    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int


class GitHubDeviceFlow:
    """OAuth 2.0 device flow against github.com."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def start(self) -> DeviceCodePrompt:
        resp = await self._client.post(
            DEVICE_CODE_URL,
            data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
            headers=_JSON_ACCEPT,
        )
        resp.raise_for_status()
        d = resp.json()
        return DeviceCodePrompt(
            user_code=str(d["user_code"]),
            verification_uri=str(d["verification_uri"]),
            device_code=str(d["device_code"]),
            interval=int(d.get("interval", 5)),
            expires_in=int(d.get("expires_in", 900)),
        )

    async def poll(self, prompt: DeviceCodePrompt) -> str:
        """Poll until the user approves; return the OAuth access token."""
        deadline = time.monotonic() + prompt.expires_in
        while time.monotonic() < deadline:
            resp = await self._client.post(
                ACCESS_TOKEN_URL,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": prompt.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers=_JSON_ACCEPT,
            )
            resp.raise_for_status()
            d = resp.json()
            if "access_token" in d:
                return str(d["access_token"])
            error = d.get("error")
            if error in ("authorization_pending", "slow_down"):
                await asyncio.sleep(prompt.interval + (5 if error == "slow_down" else 0))
                continue
            raise DeviceLoginError(f"device login failed: {error}")
        raise DeviceLoginError("device login timed out")

    async def aclose(self) -> None:
        await self._client.aclose()


class CopilotCredentialSource(CredentialSource):
    """Exchanges a GitHub OAuth token for short-lived Copilot chat tokens."""

    def __init__(
        self,
        oauth_token: str,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oauth_token = oauth_token
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    async def headers(self) -> dict[str, str]:
        if self._token is None or self._clock() >= self._expires_at - _REFRESH_MARGIN_S:
            await self._refresh()
        return {
            "Authorization": f"Bearer {self._token}",
            "Copilot-Integration-Id": "vscode-chat",
            "Editor-Version": "vscode/1.95.0",
        }

    async def _refresh(self) -> None:
        resp = await self._client.get(
            COPILOT_TOKEN_URL,
            headers={"Authorization": f"token {self._oauth_token}", **_JSON_ACCEPT},
        )
        if resp.status_code != 200:
            raise DeviceLoginError(
                f"Copilot token exchange failed (HTTP {resp.status_code}) — "
                "check your Copilot subscription or re-run :ai to log in again"
            )
        d = resp.json()
        self._token = str(d["token"])
        self._expires_at = float(d.get("expires_at", 0))

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [x] **Step 4: `make check` green.**
- [x] **Step 5: Commit** — `feat: GitHub Copilot device flow and credential source`

---

### Task 4: Entra credential source (optional extra)

**Files:**
- Create: `src/korvid/providers/entra.py`
- Modify: `pyproject.toml` (`[project.optional-dependencies] entra = ["azure-identity>=1.19"]`; add `azure-identity` to dev group so tests/mypy see it)
- Test: `tests/providers/test_entra.py`

**Interfaces:**
- Produces: `EntraCredentialSource(credential: Any | None = None, clock=time.time)`; `ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"`. When `credential is None`, lazily imports `azure.identity.aio.DefaultAzureCredential`; ImportError → raise `RuntimeError("Entra auth requires: pip install korvid[entra]")`.

- [x] **Step 1: Failing tests**

```python
# tests/providers/test_entra.py
from types import SimpleNamespace

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
```

- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement**

```python
# src/korvid/providers/entra.py
"""Microsoft Entra ID credential source (Azure OpenAI / AI Foundry)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from korvid.agent.credentials import CredentialSource

ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"
_REFRESH_MARGIN_S = 300.0


class EntraCredentialSource(CredentialSource):
    """Bearer tokens via azure-identity (DefaultAzureCredential by default)."""

    def __init__(
        self,
        credential: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._clock = clock
        self._token: str | None = None
        self._expires_on = 0.0

    def _get_credential(self) -> Any:
        if self._credential is None:
            try:
                from azure.identity.aio import DefaultAzureCredential
            except ImportError as exc:
                raise RuntimeError("Entra auth requires: pip install korvid[entra]") from exc
            self._credential = DefaultAzureCredential()
        return self._credential

    async def headers(self) -> dict[str, str]:
        if self._token is None or self._clock() >= self._expires_on - _REFRESH_MARGIN_S:
            access = await self._get_credential().get_token(ENTRA_SCOPE)
            self._token = str(access.token)
            self._expires_on = float(access.expires_on)
        return {"Authorization": f"Bearer {self._token}"}

    async def aclose(self) -> None:
        if self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()
```

- [x] **Step 4: `make check` green** (`uv add --optional entra azure-identity`, `uv add --group dev azure-identity`).
- [x] **Step 5: Commit** — `feat: Entra credential source as optional extra`

---

### Task 5: Config schema (auth method) + save + registry/composition wiring

**Files:**
- Modify: `src/korvid/core/config.py` (add `agent_auth_method`; add `save_agent_config`)
- Modify: `src/korvid/providers/registry.py` (auth-method dispatch, github-copilot alias)
- Modify: `src/korvid/__main__.py` (pass auth method + TokenStore)
- Test: `tests/core/test_config.py`, `tests/providers/test_registry.py`

**Interfaces:**
- Produces: `KorvidConfig.agent_auth_method: str | None`; parsed from `agent.auth.method`. Back-compat default when absent: `"api_key"` if `api_key_env` set else `"none"`.
- Produces: `save_agent_config(path, *, provider, auth_method, base_url, model, api_key_env) -> None` — read-modify-write YAML preserving unrelated top-level keys.
- Produces: `create_provider(*, enabled, provider, auth_method, base_url, model, api_key_env, oauth_token=None)`; `"github-copilot"` provider alias defaults `base_url=COPILOT_CHAT_BASE_URL`, requires `oauth_token` (else warn + None). `auth_method` dispatch: `api_key`→StaticHeaderSource(env), `device-login`→CopilotCredentialSource(oauth_token), `entra`→EntraCredentialSource(), `none`→None credentials.

- [x] **Step 1: Failing tests**

```python
# tests/core/test_config.py additions
def test_auth_method_parsed(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: github-copilot\n  auth:\n    method: device-login\n")
    cfg = load_config(p)
    assert cfg.agent_auth_method == "device-login"


def test_auth_method_backcompat_api_key(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: openai-compat\n  api_key_env: K\n")
    assert load_config(p).agent_auth_method == "api_key"


def test_auth_method_backcompat_none(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n")
    assert load_config(p).agent_auth_method == "none"


def test_save_agent_config_preserves_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("namespace: prod\nlog_buffer_lines: 9000\n")
    save_agent_config(
        p, provider="github-copilot", auth_method="device-login",
        base_url="https://api.githubcopilot.com", model="gpt-4o", api_key_env=None,
    )
    cfg = load_config(p)
    assert cfg.namespace == "prod"
    assert cfg.log_buffer_lines == 9000
    assert cfg.agent_provider == "github-copilot"
    assert cfg.agent_auth_method == "device-login"


def test_save_agent_config_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "c.yaml"
    save_agent_config(p, provider="ollama", auth_method="none",
                      base_url="http://localhost:11434/v1", model="llama3", api_key_env=None)
    assert load_config(p).agent_provider == "ollama"
```

```python
# tests/providers/test_registry.py additions
def test_github_copilot_requires_oauth_token() -> None:
    assert create_provider(
        enabled=True, provider="github-copilot", auth_method="device-login",
        base_url=None, model="gpt-4o", api_key_env=None, oauth_token=None,
    ) is None


def test_github_copilot_defaults_base_url() -> None:
    p = create_provider(
        enabled=True, provider="github-copilot", auth_method="device-login",
        base_url=None, model="gpt-4o", api_key_env=None, oauth_token="gho_x",
    )
    assert isinstance(p, OpenAICompatProvider)


def test_entra_auth_builds_provider() -> None:
    p = create_provider(
        enabled=True, provider="azure", auth_method="entra",
        base_url="https://foo.openai.azure.com/v1", model="gpt-4o",
        api_key_env=None, oauth_token=None,
    )
    assert isinstance(p, OpenAICompatProvider)
```

Update all existing `create_provider` call sites/tests to pass `auth_method` (existing behavior = `"api_key"` when `api_key_env` given, else `"none"`).

- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement**

`config.py`: parse `auth_raw = agent_raw.get("auth") or {}`; `method = _opt_str(auth_raw.get("method"))`; default `"api_key" if api_key_env else "none"` when method is None and provider present.

```python
def save_agent_config(
    path: Path,
    *,
    provider: str,
    auth_method: str,
    base_url: str | None,
    model: str,
    api_key_env: str | None,
) -> None:
    """Persist the agent section, preserving unrelated top-level keys."""
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text()) or {}
    agent: dict[str, Any] = {"provider": provider, "model": model, "auth": {"method": auth_method}}
    if base_url:
        agent["base_url"] = base_url
    if api_key_env:
        agent["api_key_env"] = api_key_env
    raw["agent"] = agent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
```

`registry.py` sketch:

```python
def create_provider(
    *,
    enabled: bool,
    provider: str | None,
    auth_method: str | None,
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
    oauth_token: str | None = None,
) -> LLMProvider | None:
    if not enabled:
        return None
    name = provider.lower() if isinstance(provider, str) else ""
    if name == "github-copilot":
        if not model:
            logger.warning("github-copilot missing model — agent disabled")
            return None
        if not oauth_token:
            logger.warning("github-copilot: not logged in — run :ai in the TUI")
            return None
        return OpenAICompatProvider(
            base_url=base_url or COPILOT_CHAT_BASE_URL,
            model=model,
            credentials=CopilotCredentialSource(oauth_token),
        )
    if name not in _OPENAI_COMPAT_ALIASES:
        logger.warning("unknown agent provider %r — agent disabled", provider)
        return None
    if not base_url or not model:
        logger.warning("agent provider %r missing base_url/model — agent disabled", name)
        return None
    credentials = _build_credentials(name, auth_method, api_key_env)
    return OpenAICompatProvider(base_url=base_url, model=model, credentials=credentials)


def _build_credentials(
    name: str, auth_method: str | None, api_key_env: str | None
) -> CredentialSource | None:
    method = auth_method or ("api_key" if api_key_env else "none")
    if method == "entra":
        return EntraCredentialSource()
    if method == "api_key":
        api_key = os.environ.get(api_key_env) if api_key_env else None
        if not api_key:
            return None
        if name == "azure":
            return StaticHeaderSource(api_key, header="api-key", prefix="")
        return StaticHeaderSource(api_key)
    return None  # "none" and unknown methods -> unauthenticated
```

`__main__.py`: `token_store = TokenStore()`; `oauth = token_store.load("github-oauth") if config.agent_provider == "github-copilot" else None`; pass `auth_method=config.agent_auth_method, oauth_token=oauth` to `create_provider`.

- [x] **Step 4: `make check` green.**
- [x] **Step 5: Commit** — `feat: auth method config, save_agent_config, provider auth dispatch`

---

### Task 6: AgentConfigurator ABC + composition-root implementation

**Files:**
- Create: `src/korvid/agent/setup.py`
- Create: `src/korvid/providers/configurator.py`
- Test: `tests/providers/test_configurator.py`

**Interfaces:**
- Produces (in `korvid.agent.setup`, importable by ui):

```python
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


class AgentConfigurator(ABC):  # abc.ABC per AGENTS.md boundary rule (updated at review)
    @abstractmethod
    async def begin_device_login(self) -> DeviceLoginPrompt: ...
    @abstractmethod
    async def finish_device_login(self) -> None: ...
    @abstractmethod
    async def test(self, settings: AgentSettings) -> str: ...
    @abstractmethod
    async def save(self, settings: AgentSettings) -> None: ...
```

- Produces: `ProviderConfigurator(token_store, persist: Callable[[AgentSettings], None], client: httpx.AsyncClient | None = None)` implementing the ABC. `finish_device_login` polls and stores OAuth token under key `"github-oauth"`. `test()` builds a provider via `create_provider` (oauth from store), sends `[{"role": "user", "content": "Reply with the single word: ok"}]` with `tools=[]`, returns concatenated text (must be non-empty, else raise `RuntimeError("provider returned no text")`); always `aclose()`s the provider (try/finally). `save()` calls `persist(settings)`.

- [x] **Step 1: Failing tests** — fake `GitHubDeviceFlow` injected via constructor param `flow_factory: Callable[[], GitHubDeviceFlow]`; fake provider path exercised by monkeypatching `korvid.providers.configurator.create_provider` to return a `ScriptedProvider`-style stub whose `complete` yields `{"type": "text_delta", "text": "ok"}` then `{"type": "done"}`. Cases: (a) device login stores token, (b) `test` returns "ok", (c) `test` raises on providerless settings (create_provider returns None → `RuntimeError`), (d) `save` invokes persist with settings.

- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement** `ProviderConfigurator`:

```python
# src/korvid/providers/configurator.py (core logic)
class ProviderConfigurator(AgentConfigurator):
    def __init__(
        self,
        token_store: TokenStore,
        persist: Callable[[AgentSettings], None],
        flow_factory: Callable[[], GitHubDeviceFlow] = GitHubDeviceFlow,
    ) -> None:
        self._store = token_store
        self._persist = persist
        self._flow_factory = flow_factory
        self._flow: GitHubDeviceFlow | None = None
        self._prompt: DeviceCodePrompt | None = None

    async def begin_device_login(self) -> DeviceLoginPrompt:
        self._flow = self._flow_factory()
        self._prompt = await self._flow.start()
        return DeviceLoginPrompt(self._prompt.user_code, self._prompt.verification_uri)

    async def finish_device_login(self) -> None:
        if self._flow is None or self._prompt is None:
            raise RuntimeError("begin_device_login must be called first")
        try:
            token = await self._flow.poll(self._prompt)
        finally:
            await self._flow.aclose()
            self._flow = None
            self._prompt = None
        self._store.save("github-oauth", token)

    async def test(self, settings: AgentSettings) -> str:
        oauth = self._store.load("github-oauth")
        provider = create_provider(
            enabled=True, provider=settings.provider, auth_method=settings.auth_method,
            base_url=settings.base_url, model=settings.model,
            api_key_env=settings.api_key_env, oauth_token=oauth,
        )
        if provider is None:
            raise RuntimeError("configuration incomplete — provider could not be created")
        text = ""
        try:
            async for ev in provider.complete(
                [{"role": "user", "content": "Reply with the single word: ok"}], []
            ):
                if ev.get("type") == "text_delta":
                    text += str(ev.get("text", ""))
        finally:
            await provider.aclose()
        if not text.strip():
            raise RuntimeError("provider returned no text")
        return text.strip()

    async def save(self, settings: AgentSettings) -> None:
        self._persist(settings)
```

- [x] **Step 4: `make check` green.**
- [x] **Step 5: Commit** — `feat: AgentConfigurator protocol and provider implementation` (contract later converted to `abc.ABC` at review)

---

### Task 7: TUI setup wizard (`:ai`) + runtime rebuild

**Files:**
- Create: `src/korvid/ui/widgets/agent_setup_screen.py`
- Modify: `src/korvid/ui/app.py` (`:ai` command, `agent_configurator` + `rebuild_agent` params, apply result)
- Modify: `src/korvid/ui/widgets/agent_panel.py` (`_SETUP_HINT` → "Run :ai to configure the agent, or edit ~/.config/korvid/config.yaml")
- Modify: `src/korvid/__main__.py` (wire `ProviderConfigurator` + `rebuild_agent` closure)
- Test: `tests/ui/test_agent_setup_screen.py`, update `tests/ui/test_agent_wiring.py`

**Interfaces:**
- Consumes: `AgentConfigurator`, `AgentSettings`, `DeviceLoginPrompt` from `korvid.agent.setup`.
- Produces: `AgentSetupScreen(configurator)` — `ModalScreen[AgentSettings | None]`; app param `rebuild_agent: Callable[[AgentSettings], AgentRuntime | None] | None`.

**Wizard flow (single screen, staged widgets — superseded during review; as implemented):**
1. Provider `OptionList`: `github-copilot`, `openai-compat`, `azure`, `ollama` (id strings). Selecting sets defaults: github-copilot → auth `device-login`, base_url None, model `gpt-4o`; openai-compat → auth `api_key`, base_url `https://api.openai.com/v1`, model `gpt-4o-mini`; azure → second OptionList `api_key` / `entra`, base_url empty (required), model empty (required); ollama → auth `none`, base_url `http://localhost:11434/v1`, model `llama3`.
2. Auth/endpoint stage: github-copilot signs in first (device login via `begin_device_login()`/`finish_device_login()` in a worker, reusing an existing login when `list_models` already answers); other providers ask for `Input#setup-base-url` then `Input#setup-api-key-env` (only for `api_key`). Enter advances; completed steps stay visible as a checklist (`Static#setup-steps`).
3. Model stage: `list_models(settings)` in a worker; non-empty → type-to-filter `OptionList#setup-model-list`; empty → typed `Input#setup-model` fallback with provider default.
4. Test stage: `test(settings)` in a worker; success → apply to the running app first (`apply_settings` callback), and only when the swap succeeds `save(settings)` → `dismiss(settings)`; failure at any point → show error in `#setup-status`, stay open (Esc cancels, Ctrl+R retries with the currently visible input values). Persisting before a successful apply is forbidden — a rejected change must never activate after a restart.
- Esc anywhere → `dismiss(None)`.

**App wiring:** command `:ai` (alias `:agent`) pushes the screen when `self._agent_configurator` is not None, passing `apply_settings=self._apply_agent_settings`; the wizard calls it before saving (transactional order above). `_apply_agent_settings` calls `runtime = self._rebuild_agent(settings)`; on success sets `_agent_runtime`, `_agent_model_name = settings.model`, `_refresh_status()`, and if the panel is open re-enables input + header; on failure returns False and keeps the old runtime. `__main__` closure:

```python
def rebuild_agent(settings: AgentSettings) -> AgentRuntime | None:
    new_provider = create_provider(
        enabled=True, provider=settings.provider, auth_method=settings.auth_method,
        base_url=settings.base_url, model=settings.model,
        api_key_env=settings.api_key_env, oauth_token=token_store.load("github-oauth"),
    )
    ...
```

Track the live provider in a mutable holder at composition root: `provider_box: list[LLMProvider | None] = [provider]`. Build the replacement first; only after it exists swap `provider_box[0]` and close the old provider via `asyncio.create_task(old.aclose())` — closing the old provider before the replacement is known to exist would break the transactional failure behavior (a failed rebuild must leave the current runtime untouched). `_shutdown` closes `provider_box[0]`.

- [x] **Step 1: Failing UI tests** — drive with a `FakeConfigurator` recording calls: (a) ollama path: pick provider, accept defaults, test called, save called, dismissed with settings; (b) github-copilot path: begin/finish device login called, device code text visible; (c) test failure keeps screen open and shows error; (d) Esc dismisses None; (e) app-level: `:ai` applies settings → status bar shows "AI on" and panel input enabled.
- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement screen + app wiring + `__main__` closure.**
- [x] **Step 4: `make check` green.**
- [x] **Step 5: Commit** — `feat: in-TUI agent setup wizard (:ai) with device login`

---

### Task 8: `:model` command

**Files:**
- Modify: `src/korvid/ui/app.py` (+ command routing in `src/korvid/ui/command.py` if commands are parsed there)
- Test: `tests/ui/test_agent_wiring.py`

**Interfaces:**
- Consumes: `rebuild_agent`, current `AgentSettings` snapshot kept on app (`self._agent_settings: AgentSettings | None`, set at construction from config and updated by wizard/`:model`).

**Behavior:**
- `:model` (no arg) → status-bar flash/notify current model or "agent not configured".
- `:model <name>` → requires existing `_agent_settings`; `dataclasses.replace(settings, model=name)` → `rebuild_agent` + apply first → only after a successful swap, `save` via configurator (transactional: persisting first would activate a rejected change after restart). Save failure warns that the live change reverts to the last saved model on restart. Rebuild/apply errors notify, keep old runtime, and persist nothing.

- [x] **Step 1: Failing tests** — (a) `:model gpt-4o` swaps `_agent_model_name` and calls rebuild+save; (b) `:model` without config notifies and does not crash.
- [x] **Step 2: Verify FAIL.**
- [x] **Step 3: Implement.**
- [x] **Step 4: `make check` green.**
- [x] **Step 5: Commit** — `feat: :model command to switch agent model`

---

### Task 9: Docs + README + PR

**Files:**
- Modify: `README.md` (config examples per provider×auth, unofficial-API warning, install/run instructions incl. `uv run korvid` for dev and `pipx install korvid` style for users)
- Modify: `docs/dev/plans/2026-07-25-plan4-slice2-auth-setup.md` (mark complete)

**README must include these exact config examples:**

```yaml
# GitHub Copilot (log in via :ai inside korvid — no PAT needed)
agent:
  provider: github-copilot
  model: gpt-4o
  auth: {method: device-login}

# Azure OpenAI / AI Foundry with Entra ID (az login or managed identity)
agent:
  provider: azure
  base_url: https://YOUR-RESOURCE.openai.azure.com/openai/v1
  model: gpt-4o
  auth: {method: entra}

# Any OpenAI-compatible endpoint with an API key from the environment
agent:
  provider: openai-compat
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  api_key_env: OPENAI_API_KEY
  auth: {method: api_key}

# Local Ollama (no auth)
agent:
  provider: ollama
  base_url: http://localhost:11434/v1
  model: llama3
  auth: {method: none}
```

Plus the verbatim warning from Global Constraints and a note that Entra needs `pip install korvid[entra]`.

- [x] **Step 1: Write docs.**
- [x] **Step 2: `make check` green (docs don't break lint).**
- [x] **Step 3: Commit, push branch `agent-auth-slice2`, open PR, request Copilot review.**
