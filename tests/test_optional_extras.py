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
_AGENT_MODULES = ("httpx", "keyring")

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
        "korvid.agent.runtime",
        "korvid.agent.outbound",
    ],
)
def test_base_import_does_not_require_optional_extras(module: str) -> None:
    """The composition root, UI, tool layer, agent runtime, and outbound policy
    are all importable without the [mcp]/[agent] extras installed."""
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
        "    if name in {'httpx', 'keyring'}:\n"
        "        return None\n"
        "    return real_find_spec(name, *args, **kwargs)\n"
        "importlib.util.find_spec = fake_find_spec\n"
        "from korvid.__main__ import _build_agent_wiring\n"
        "from korvid.core.config import KorvidConfig\n"
        "runtime, configurator, rebuild, retarget, _disconnect, provider_box, _proxy = "
        "_build_agent_wiring(KorvidConfig(), object(), {})\n"
        "assert runtime is None\n"
        "assert configurator is None\n"
        "assert rebuild is None\n"
        "assert provider_box == [None]\n"
        "retarget(None, True, 'ctx')\n"
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
        "watched = ('korvid.agent.runtime', 'korvid.agent.provider', 'korvid.agent.profiles')\n"
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
        "watched = ('textual', 'httpx', 'keyring', 'mcp', 'anyio', 'starlette', 'uvicorn')\n"
        "leaked = [m for m in watched if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'agent outbound leaked optional extras into base import: {leaked}')\n"
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
