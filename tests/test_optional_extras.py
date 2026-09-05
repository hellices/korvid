"""Import-graph guards for the optional-extras split (issue #73).

A base installation (no `[mcp]`, no `[agent]` extra) must be able to import
the composition root and the full UI without pulling in any of the optional
third-party stacks. These tests run the import in a subprocess so nothing
already cached in this test process's `sys.modules` can mask a regression.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.fixtures.provider_plugin.site_helpers import FIXTURES_DIR

#: Top-level third-party modules that only the optional extras may pull in.
_MCP_MODULES = ("mcp", "anyio", "starlette", "uvicorn")
_AGENT_MODULES = ("httpx", "keyring", "litellm")

_PROBE = """
import sys

import {module}  # noqa: F401

leaked = [m for m in {watched!r} if m in sys.modules]
if leaked:
    raise SystemExit(f"optional extras leaked into base import: {{leaked}}")
"""


def _assert_import_is_extra_free(module: str) -> None:
    watched = _MCP_MODULES + _AGENT_MODULES
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, watched=watched)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def _run_subprocess_probe(probe: str) -> None:
    pythonpath = os.pathsep.join(
        entry for entry in [str(FIXTURES_DIR), os.environ.get("PYTHONPATH")] if entry
    )
    env = {**os.environ, "PYTHONPATH": pythonpath}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "korvid.__main__",
        "korvid.ui.app",
        "korvid.tools.executor",
        "korvid.agent.session",
        "korvid.agent.outbound",
    ],
)
def test_base_import_does_not_require_optional_extras(module: str) -> None:
    """The composition root, UI, tool layer, agent session, and outbound
    policy are all importable without the [mcp]/[agent] extras installed."""
    _assert_import_is_extra_free(module)


def test_mcp_adapter_is_the_only_mcp_stack_consumer() -> None:
    """Importing korvid.mcp.server is what pulls in the MCP stack — nothing
    else does, so the extra boundary matches the module boundary."""
    probe = (
        "import sys\n"
        "import korvid.mcp.server  # noqa: F401\n"
        "missing = [m for m in ('mcp', 'anyio', 'starlette', 'uvicorn') if m not in sys.modules]\n"
        "if missing:\n"
        "    raise SystemExit(f'expected the MCP stack to load: {missing}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["korvid.__main__", "korvid.ui.app"])
def test_base_import_does_not_scan_provider_plugins(module: str) -> None:
    probe = (
        "import importlib.metadata as metadata\n"
        "import sys\n"
        "def boom(*args, **kwargs):\n"
        "    raise SystemExit('provider entry-point discovery must stay lazy')\n"
        "metadata.entry_points = boom\n"
        "metadata.distributions = boom\n"
        f"import {module}  # noqa: F401\n"
        "assert 'company_provider' not in sys.modules\n"
        "assert 'unselected_provider' not in sys.modules\n"
    )
    _run_subprocess_probe(probe)


def test_agentless_wiring_does_not_scan_provider_plugins() -> None:
    probe = (
        "import importlib.metadata as metadata\n"
        "import importlib.util\n"
        "import sys\n"
        "def boom(*args, **kwargs):\n"
        "    raise SystemExit('provider entry-point discovery must stay lazy')\n"
        "metadata.entry_points = boom\n"
        "metadata.distributions = boom\n"
        "real_find_spec = importlib.util.find_spec\n"
        "def fake_find_spec(name, *args, **kwargs):\n"
        "    if name in {'httpx', 'keyring', 'litellm'}:\n"
        "        return None\n"
        "    return real_find_spec(name, *args, **kwargs)\n"
        "importlib.util.find_spec = fake_find_spec\n"
        "from korvid.__main__ import _build_agent_wiring\n"
        "from korvid.core.config import KorvidConfig\n"
        "wiring = _build_agent_wiring(KorvidConfig(), object(), {})\n"
        "assert wiring.session is None\n"
        "assert wiring.configurator is None\n"
        "assert wiring.rebuild is None\n"
        "assert wiring.provider_box == [None]\n"
        "assert wiring.session_box == [None]\n"
        "wiring.retarget(None, True, None)\n"
        "assert 'company_provider' not in sys.modules\n"
        "assert 'unselected_provider' not in sys.modules\n"
    )
    _run_subprocess_probe(probe)


@pytest.mark.parametrize("module", ["korvid.__main__", "korvid.ui.app"])
def test_base_import_does_not_load_the_agent_loop(module: str) -> None:
    """MCP-only/base startups must not import the embedded-agent loop or the
    provider ABC (issue #73 acceptance criterion) — those load only when the
    agent wiring is actually composed."""
    probe = (
        "import sys\n"
        f"import {module}  # noqa: F401\n"
        "watched = (\n"
        "    'korvid.agent.provider',\n"
        "    'korvid.agent.session',\n"
        "    'korvid.agent.model_policy',\n"
        "    'korvid.agent.prompt_harness',\n"
        "    'korvid.agent.native_engine',\n"
        "    'korvid.agent.request_gateway',\n"
        "    'korvid.agent.tool_harness',\n"
        "    'korvid.agent.conversation',\n"
        ")\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'embedded-agent loop leaked into base import: {leaked}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


def test_agent_outbound_does_not_load_optional_extras() -> None:
    probe = (
        "import sys\n"
        "import korvid.agent.outbound  # noqa: F401\n"
        "watched = ('textual', 'httpx', 'keyring', 'litellm', 'mcp', 'anyio', 'starlette', 'uvicorn')\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'agent outbound leaked optional extras into base import: {leaked}')\n"
    )
    _run_subprocess_probe(probe)


def test_the_base_install_does_not_import_litellm() -> None:
    """The base install must not reach LiteLLM through the app entry points."""
    probe = (
        "import sys\n"
        "import korvid.__main__  # noqa: F401\n"
        "if 'litellm' in sys.modules:\n"
        "    raise SystemExit('litellm leaked into the base import graph')\n"
        "import korvid.ui.app  # noqa: F401\n"
        "if 'litellm' in sys.modules:\n"
        "    raise SystemExit('litellm leaked into the base import graph')\n"
    )
    _run_subprocess_probe(probe)


def test_a_missing_agent_extra_degrades_to_no_agent_rather_than_a_crash() -> None:
    """Without the agent extra, startup should return the disabled wiring."""
    probe = (
        _MISSING_AGENT_EXTRA + "from korvid.__main__ import _build_agent_wiring\n"
        "from korvid.core.config import KorvidConfig\n"
        "wiring = _build_agent_wiring(KorvidConfig(), object(), {})\n"
        "assert wiring.session is None\n"
        "assert wiring.configurator is None\n"
        "assert wiring.rebuild is None\n"
        "assert wiring.provider_box == [None]\n"
        "assert wiring.session_box == [None]\n"
    )
    _run_subprocess_probe(probe)


def test_requesting_the_agent_explicitly_without_the_extra_fails_with_a_hint() -> None:
    """An enabled agent without its extra must fail with install guidance."""
    probe = (
        "import korvid.__main__ as main\n"
        "from korvid.core.config import KorvidConfig\n"
        "main._missing_extra_packages = lambda roots: ['httpx', 'keyring', 'litellm']\n"
        "try:\n"
        "    main._build_agent_wiring(KorvidConfig(agent_enabled=True), object(), {})\n"
        "except SystemExit as exc:\n"
        "    message = str(exc)\n"
        "    if 'korvid[all,entra]' not in message:\n"
        "        raise SystemExit(message)\n"
        "    if 'uv tool install --force' not in message:\n"
        "        raise SystemExit(message)\n"
        "    if 'pipx install --force' not in message:\n"
        "        raise SystemExit(message)\n"
        "else:\n"
        "    raise SystemExit('expected agent wiring to fail without the extra')\n"
    )
    _run_subprocess_probe(probe)


def test_the_connector_boundary_imports_without_the_observability_extra() -> None:
    """`korvid.obs.connector` is the policy half and must stay stdlib-only.

    The tool registry imports it for the signal catalogue, so a base
    installation would break at startup if it reached for httpx.
    """
    _assert_import_is_extra_free("korvid.obs.connector")


def test_the_tool_registry_imports_without_any_extra() -> None:
    _assert_import_is_extra_free("korvid.tools.registry")


def test_the_http_connectors_are_not_reachable_from_the_boundary() -> None:
    """Importing the boundary must not drag the HTTP implementations in.

    They are what needs the extra; if the package `__init__` or the
    boundary imported them eagerly, the extra would stop being optional.
    """
    probe = """
import sys

import korvid.obs.connector  # noqa: F401

leaked = [m for m in ("korvid.obs.prometheus", "korvid.obs.loki") if m in sys.modules]
if leaked:
    raise SystemExit(f"HTTP connectors leaked into the boundary import: {leaked}")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


#: The embedded-agent turn machinery. None of it may be imported by a base
#: or MCP-only installation, and none of it may be imported when the agent
#: extra is missing — the TUI still has to start (issue #316 task 13).
_AGENT_SESSION_MODULES = (
    "korvid.agent.native_engine",
    "korvid.agent.session",
    "korvid.agent.request_gateway",
    "korvid.agent.tool_harness",
    "korvid.agent.prompt_harness",
    "korvid.agent.conversation",
)

_MISSING_AGENT_EXTRA = (
    "import importlib.metadata as metadata\n"
    "import importlib.util\n"
    "import sys\n"
    "real_find_spec = importlib.util.find_spec\n"
    "def fake_find_spec(name, *args, **kwargs):\n"
    "    if name in {'httpx', 'keyring', 'litellm'}:\n"
    "        return None\n"
    "    return real_find_spec(name, *args, **kwargs)\n"
    "importlib.util.find_spec = fake_find_spec\n"
)


def test_the_agent_session_graph_stays_out_of_a_base_import() -> None:
    """A base/MCP-only TUI never imports the engine, session, or gateway."""
    probe = (
        "import sys\n"
        "import korvid.__main__  # noqa: F401\n"
        "import korvid.ui.app  # noqa: F401\n"
        f"watched = {_AGENT_SESSION_MODULES!r}\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'agent session graph leaked into base import: {leaked}')\n"
    )
    _run_subprocess_probe(probe)


def test_a_missing_agent_extra_leaves_the_session_graph_unimported() -> None:
    """The wiring degrades to no session; it must not import the loop to find out."""
    probe = (
        _MISSING_AGENT_EXTRA + "from korvid.__main__ import _build_agent_wiring\n"
        "from korvid.core.config import KorvidConfig\n"
        "wiring = _build_agent_wiring(KorvidConfig(), object(), {})\n"
        "assert wiring.session is None\n"
        f"watched = {_AGENT_SESSION_MODULES!r}\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'agentless wiring imported the session graph: {leaked}')\n"
    )
    _run_subprocess_probe(probe)


def test_a_disabled_agent_leaves_the_session_graph_unimported() -> None:
    """The extra is installed, but the operator did not enable the agent."""
    probe = (
        "import sys\n"
        "from korvid.__main__ import _build_agent_wiring\n"
        "from korvid.core.config import KorvidConfig\n"
        "wiring = _build_agent_wiring(KorvidConfig(), object(), {})\n"
        "assert wiring.session is None\n"
        f"watched = {_AGENT_SESSION_MODULES!r}\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'a disabled agent imported the session graph: {leaked}')\n"
    )
    _run_subprocess_probe(probe)


def test_the_mcp_only_tui_still_starts_without_the_agent_extra() -> None:
    """MCP is a separate extra: its adapter must not need the agent's."""
    probe = (
        _MISSING_AGENT_EXTRA + "import korvid.mcp.server  # noqa: F401\n"
        "import korvid.ui.app  # noqa: F401\n"
        f"watched = {_AGENT_SESSION_MODULES!r}\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'the MCP adapter pulled in the agent session: {leaked}')\n"
    )
    _run_subprocess_probe(probe)


def test_the_observability_boundary_needs_no_agent_session() -> None:
    probe = (
        "import sys\n"
        "import korvid.obs.connector  # noqa: F401\n"
        "import korvid.tools.registry  # noqa: F401\n"
        f"watched = {_AGENT_SESSION_MODULES!r}\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'the observability boundary pulled in the agent: {leaked}')\n"
    )
    _run_subprocess_probe(probe)
