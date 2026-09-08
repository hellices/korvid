"""Device-login prefixes: claimed before routing, whatever is installed.

LiteLLM ships providers whose *routing* call is an interactive login. Given
a reference under one of their prefixes, `get_llm_provider` builds the
provider's config object, which constructs an `Authenticator`, which creates
a credential directory under `~/.config/litellm/` and — when no usable token
is cached — prints a user code and blocks polling GitHub or OpenAI for the
answer. The operator's `api_key` and `endpoint` are ignored: the config
class overwrites both with whatever the authenticator returns.

That is not a call korvid may make while merely *resolving* a reference, so
`DEVICE_LOGIN_PREFIXES` claims those prefixes ahead of routing and the claim
holds whether or not a flow is installed to serve them.

Two kinds of test live here:

- a behavioural regression that drives the real factory with the real SDK
  and records every dangerous thing that could happen — routing, the
  authenticator, a socket, a file under the token directory;
- a drift gate that rediscovers the device-code providers from the
  *installed* LiteLLM rather than from this file's memory of them, so a
  release that adds a third one fails here instead of in an operator's
  terminal.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import socket
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig
from korvid.providers import litellm_runtime
from korvid.providers.litellm_factory import create_provider_from_profile
from korvid.providers.litellm_settings import DEVICE_LOGIN_PREFIXES
from korvid.providers.special_flows import SpecialFlowRegistry, normalize_prefix

#: The LiteLLM release every measurement in this module was taken on, and
#: the one `pyproject.toml` pins. A bump has to re-measure: the discovery
#: below reads an upstream layout convention, and the claimed set is only
#: as good as the release it was checked against.
MEASURED_LITELLM_VERSION: Final = "1.98.0"

#: The device-code providers measured on that release, as LiteLLM spells
#: them. Discovery must keep finding *at least* these: a restructure that
#: makes it find nothing would otherwise turn this gate green and silent.
MEASURED_DEVICE_CODE_PROVIDERS: Final = frozenset({"chatgpt", "github_copilot"})

#: Environment variables each device-code authenticator reads for its
#: credential directory. Pointed at a temporary path so a write the SDK
#: makes is visible to the test instead of landing in the developer's real
#: `~/.config`, and so "nothing was written" is an assertion rather than a
#: hope.
TOKEN_DIR_VARIABLES: Final = ("CHATGPT_TOKEN_DIR", "GITHUB_COPILOT_TOKEN_DIR")

#: Every reference that must be refused before anything happens. Both
#: spellings of the Copilot prefix, because the underscore form is the one
#: LiteLLM's own tables publish, and a `chatgpt` reference as it appears in
#: `models_by_provider()`.
TRAP_REFERENCES: Final = (
    "chatgpt/gpt-5.2",
    "github-copilot/gpt-4o",
    "github_copilot/gpt-4o",
)


# ---------------------------------------------------------------------------
# Discovery: which installed providers authenticate by device code
# ---------------------------------------------------------------------------


def _llms_package() -> ModuleType:
    return importlib.import_module("litellm.llms")


def _provider_packages() -> list[str]:
    """Every provider package under `litellm.llms`.

    Listed from the package's own `__path__` rather than through
    `pkgutil.iter_modules`, which skips a directory with no `__init__.py`
    — and both device-code providers ship as namespace packages, so the
    convenient enumeration is exactly the one that cannot see them.
    """
    names = {
        child.name
        for entry in getattr(_llms_package(), "__path__", ())
        for child in Path(entry).iterdir()
        if child.is_dir() and child.name.isidentifier() and not child.name.startswith("_")
    }
    return sorted(names)


def _authenticator_modules(package: str) -> list[ModuleType]:
    """Import the auth-shaped submodules of one provider package.

    Only modules whose *name* mentions auth are imported, so discovery
    costs three imports rather than importing every provider LiteLLM
    ships — which would run hundreds of module bodies to answer a
    question about two of them.
    """
    try:
        parent = importlib.import_module(f"litellm.llms.{package}")
    except Exception:  # a provider module may need an extra korvid lacks
        return []
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(list(getattr(parent, "__path__", ()))):
        if "auth" not in info.name:
            continue
        try:
            modules.append(importlib.import_module(f"litellm.llms.{package}.{info.name}"))
        except Exception:  # an unimportable module cannot start a login
            continue
    return modules


def _is_device_code_module(module: ModuleType) -> bool:
    """Does this module hold an authenticator driving a device-code login?

    Read from the module's own symbols rather than from its text: a
    device-code flow needs an `Authenticator` and needs to name the
    device code somewhere — the poll URL, the timeout, the login method.
    Both installed offenders satisfy this; `gigachat`, which ships an
    `authenticator` module with neither, does not.
    """
    authenticator = getattr(module, "Authenticator", None)
    if not isinstance(authenticator, type):
        return False
    names = list(vars(module)) + list(vars(authenticator))
    return any("devicecode" in name.lower().replace("_", "") for name in names)


def device_code_providers() -> frozenset[str]:
    """Installed provider packages whose authentication is a device login."""
    return frozenset(
        package
        for package in _provider_packages()
        if any(_is_device_code_module(module) for module in _authenticator_modules(package))
    )


# ---------------------------------------------------------------------------
# Recorders: everything a claimed reference must not be able to reach
# ---------------------------------------------------------------------------


class _Recorder:
    """Stands in for the SDK's authenticator, and remembers being used."""

    calls: list[str] = []  # noqa: RUF012 — shared on purpose, reset per test

    def __init__(self) -> None:
        _Recorder.calls.append("construct")

    def get_access_token(self) -> str:
        _Recorder.calls.append("get_access_token")
        return "device-login-token"

    def get_api_key(self) -> str:
        _Recorder.calls.append("get_api_key")
        return "device-login-token"

    def get_api_base(self) -> str:
        _Recorder.calls.append("get_api_base")
        return "https://device-login.invalid"

    def get_account_id(self) -> str | None:
        _Recorder.calls.append("get_account_id")
        return None


#: Every authenticator method a routing call can reach. Patched on the
#: class object itself, so a module that did `from ..authenticator import
#: Authenticator` is covered by the same patch.
_RECORDED_METHODS: Final = (
    "__init__",
    "get_access_token",
    "get_api_key",
    "get_api_base",
    "get_account_id",
)


def _record_authenticator(monkeypatch: pytest.MonkeyPatch, package: str) -> None:
    """Replace one provider's authenticator methods with the recorder's."""
    module = importlib.import_module(f"litellm.llms.{package}.authenticator")
    for name in _RECORDED_METHODS:
        if hasattr(module.Authenticator, name):
            monkeypatch.setattr(module.Authenticator, name, getattr(_Recorder, name))


@pytest.fixture
def device_login_watch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[str], list[str], Path]:
    """Make every device-login side effect observable and harmless.

    Returns the routing log, the authenticator log, and the directory the
    SDK would write a credential into.
    """
    routed: list[str] = []
    _Recorder.calls = []

    token_dir = tmp_path / "litellm-tokens"
    for variable in TOKEN_DIR_VARIABLES:
        monkeypatch.setenv(variable, str(token_dir / variable.lower()))

    def _record(model: str, **kwargs: object) -> tuple[str, str, None, None]:
        routed.append(model)
        return (model, "recorded", None, None)

    monkeypatch.setattr(litellm_runtime, "get_llm_provider", _record)

    for package in sorted(MEASURED_DEVICE_CODE_PROVIDERS):
        _record_authenticator(monkeypatch, package)

    return routed, _Recorder.calls, token_dir


@pytest.fixture
def refuse_sockets(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record any attempt to open a connection, and refuse it."""
    attempts: list[Any] = []

    def _connect(self: socket.socket, address: Any) -> None:
        attempts.append(address)
        raise AssertionError(f"a claimed reference opened a connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _connect)
    return attempts


def _profile(reference: str) -> ModelConnectionConfig:
    return ModelConnectionConfig(
        model=reference,
        endpoint=None,
        auth=ConnectionAuthConfig(method="provider-default"),
        options={},
    )


# ---------------------------------------------------------------------------
# The hazard is real: routing *is* the login
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reference", ["chatgpt/gpt-5.2", "github_copilot/gpt-4o"])
def test_routing_a_device_login_reference_authenticates_and_ignores_the_key(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """The measurement this whole file exists for, run against the real SDK.

    `get_llm_provider` is asked only to *resolve* a reference. For these
    prefixes it builds the provider config, which constructs the
    authenticator and asks it for a credential — and then returns that
    credential, discarding the `api_key` it was given. So an operator's
    own key does not make the reference safe, and there is no argument
    korvid could pass that would.
    """
    _Recorder.calls = []
    _record_authenticator(monkeypatch, reference.split("/", 1)[0])

    _model, _provider, dynamic_key, _api_base = litellm_runtime.get_llm_provider(
        model=reference,
        api_key="the-operators-own-key",
        api_base="https://the-operators-own-endpoint.invalid",
    )

    assert "construct" in _Recorder.calls
    assert dynamic_key == "device-login-token"


def test_routing_a_chatgpt_reference_also_discards_the_operators_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chatgpt` overwrites the host as well as the credential.

    Copilot's config at least honours an `api_base` it is handed; this
    one takes the authenticator's. Recorded because it removes the last
    argument korvid could have used to keep the reference pointed
    somewhere the operator chose.
    """
    _Recorder.calls = []
    _record_authenticator(monkeypatch, "chatgpt")

    _model, _provider, _key, api_base = litellm_runtime.get_llm_provider(
        model="chatgpt/gpt-5.2",
        api_key="the-operators-own-key",
        api_base="https://the-operators-own-endpoint.invalid",
    )

    assert api_base == "https://device-login.invalid"


def test_the_real_authenticator_writes_a_credential_directory_on_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Constructing one is already a filesystem write, before any prompt.

    Recorded here because the regression below asserts the *absence* of
    that directory, and an absence only means something once the presence
    has been demonstrated.
    """
    module = importlib.import_module("litellm.llms.chatgpt.authenticator")
    token_dir = tmp_path / "chatgpt"
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_dir))

    module.Authenticator()

    assert token_dir.is_dir()


# ---------------------------------------------------------------------------
# The regression: korvid refuses first, and reaches none of it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reference", TRAP_REFERENCES)
@pytest.mark.parametrize("flows", [None, "empty"])
def test_a_device_login_reference_never_reaches_routing_or_a_login(
    device_login_watch: tuple[list[str], list[str], Path],
    refuse_sockets: list[Any],
    reference: str,
    flows: str | None,
) -> None:
    """The whole claim, in one assertion set per reference.

    Run with no registry at all and with an empty one, because both are
    shapes a real installation has: `flows=None` is every caller that
    predates the plugin wiring, and an empty registry is korvid with the
    plugin uninstalled.
    """
    routed, authenticated, token_dir = device_login_watch
    registry = SpecialFlowRegistry() if flows == "empty" else None

    assert create_provider_from_profile(_profile(reference), flows=registry) is None

    assert routed == []
    assert authenticated == []
    assert refuse_sockets == []
    assert not token_dir.exists()


def test_every_device_login_prefix_is_claimed_by_an_empty_registry() -> None:
    """Uninstalling the flow must not turn the prefix back into a trap."""
    claimed = SpecialFlowRegistry().claimed_prefixes
    assert {normalize_prefix(prefix) for prefix in DEVICE_LOGIN_PREFIXES} <= claimed


# ---------------------------------------------------------------------------
# The drift gate: a new device-code provider fails here
# ---------------------------------------------------------------------------


def test_the_measured_release_is_the_installed_one() -> None:
    """Every measurement here was taken on one release; say which."""
    assert version("litellm") == MEASURED_LITELLM_VERSION


def test_discovery_still_finds_the_providers_it_was_written_against() -> None:
    """A discovery that finds nothing would make the gate below vacuous."""
    assert device_code_providers() >= MEASURED_DEVICE_CODE_PROVIDERS


def test_a_provider_with_an_authenticator_but_no_device_code_is_not_claimed() -> None:
    """The rule is device-code logins, not authentication in general.

    `gigachat` ships an `authenticator` module and authenticates with
    ordinary credentials. Claiming it would make an ordinary provider
    unroutable, so the discovery has to be able to tell them apart.
    """
    with_authenticator = {
        package for package in _provider_packages() if _authenticator_modules(package)
    }
    assert with_authenticator - device_code_providers()


def test_every_installed_device_code_provider_is_claimed_before_routing() -> None:
    """The gate: discovered on the installed release, claimed by korvid.

    Written as a subset check rather than an equality so that claiming a
    newly discovered provider is enough to make it pass — the fix is to
    add the prefix, never to edit the measurement.
    """
    claimed = {normalize_prefix(prefix) for prefix in DEVICE_LOGIN_PREFIXES}
    discovered = {normalize_prefix(name) for name in device_code_providers()}
    assert discovered <= claimed, (
        f"litellm {version('litellm')} ships device-code providers korvid does not claim: "
        f"{sorted(discovered - claimed)}. Add them to DEVICE_LOGIN_PREFIXES."
    )


def test_the_claim_survives_an_environment_that_points_the_login_elsewhere(
    device_login_watch: tuple[list[str], list[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached credential must not make the reference routable either.

    The refusal is about the prefix, not about whether a login would
    succeed today: an installation that already holds a token would
    otherwise route straight through to a vendor korvid never chose.
    """
    routed, authenticated, token_dir = device_login_watch
    monkeypatch.setenv("CHATGPT_API_BASE", "https://already-signed-in.invalid")

    assert create_provider_from_profile(_profile("chatgpt/gpt-5.2")) is None
    assert routed == []
    assert authenticated == []
    assert not os.path.exists(token_dir)
