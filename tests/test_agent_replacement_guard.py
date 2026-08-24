"""The v1 agent implementation is gone, and stays gone (issue #316, task 14).

korvid shipped two agent programs during the interaction-harness migration:
the original `AgentRuntime` loop with its `AgentProfile`/`PromptOverrides`
configuration, and the native harness (`NativeAgentEngine` +
`DefaultAgentSession`) that replaced it. Task 14 deletes the first one.

A deletion is only finished when it cannot come back by accident, so this
module is the structural gate:

- the retired modules are absent from the tree and unimportable;
- no source file, current test, or current documentation page names a
  retired symbol;
- nothing reintroduces a backend selector, transition flag, or `v1`/`v2`
  suffix that would let two implementations coexist again;
- exactly one `AgentEngine` and one production `AgentSession` exist;
- the agent package publishes one coherent public surface.

Historical records are deliberately out of scope: `docs/dev/specs/`,
`docs/dev/plans/` and `docs/superpowers/` describe how korvid got here and
must keep naming what they retired. The removed configuration keys
(`agent.profile`, `agent.prompts`) are likewise allowed — but only on the
migration surfaces that tell an operator what to do about them.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from korvid.agent.engine import AgentEngine
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.session import AgentSession, DefaultAgentSession

_REPO_ROOT = Path(__file__).parents[1]
_SRC = _REPO_ROOT / "src" / "korvid"
_TESTS = _REPO_ROOT / "tests"
_AGENT_PACKAGE = _SRC / "agent"

#: The four modules task 14 deletes. Importable names and on-disk paths are
#: both listed because a resurrection can arrive either way.
_RETIRED_MODULES = (
    "korvid.agent.runtime",
    "korvid.agent.profiles",
    "korvid.agent.prompts",
    "korvid.agent.context",
)

_RETIRED_FILES = ("runtime.py", "profiles.py", "prompts.py", "context.py")

#: Every production name the v1 program owned. Substrings are intentional:
#: `SYSTEM_PROMPT` also catches `SMALL_SYSTEM_PROMPT`, and the dotted and
#: path spellings catch imports as well as prose that sends a reader to a
#: module that no longer exists.
_RETIRED_SYMBOLS = (
    "AgentRuntime",
    "AgentProfile",
    "build_profile",
    "PromptOverrides",
    "validate_prompt_overrides",
    "compose_system_prompt",
    "SYSTEM_PROMPT",
    "WRITE_PROMPT",
    "UI_DRIVE_PROMPT",
    "SMALL_UI_PROMPT",
    "SMALL_TOOL_DESCRIPTIONS",
    "PROFILE_NAMES",
    "PROMPT_BUDGET_SHARE",
    "agent_profile",
    "full_agent",
    "small_agent",
    "korvid.agent.runtime",
    "korvid.agent.profiles",
    "korvid.agent.prompts",
    "korvid.agent.context",
    "agent/runtime.py",
    "agent/profiles.py",
    "agent/prompts.py",
    "agent/context.py",
)

#: Names that would let two agent implementations coexist again: a runtime
#: selector, an environment switch, a transition flag, or a `v1`/`v2` suffix
#: on the engine or the session. korvid ships one implementation, so none of
#: these has anything to select between.
_FORBIDDEN_SELECTORS = (
    "agent_backend",
    "AGENT_BACKEND",
    "agent-backend",
    "runtime_v2",
    "RuntimeV2",
    "AgentRuntimeV2",
    "AgentEngineV2",
    "AgentSessionV2",
    "v1_adapter",
    "V1Adapter",
    "legacy_runtime",
    "LegacyRuntime",
    "legacy_engine",
    "use_native_engine",
    "USE_NATIVE_ENGINE",
    "native_backend_enabled",
)

#: The removed config keys. They are still spoken about, but only where an
#: operator is told what replaced them.
_REMOVED_CONFIG_KEYS = ("agent.profile", "agent.prompts")

#: Files allowed to name a removed config key: the startup migration error,
#: the tests that pin it, this guard, the operator-facing migration
#: documentation, and the decision record that explains the supersession.
_MIGRATION_SURFACES = frozenset(
    {
        "src/korvid/core/config.py",
        "tests/core/test_config.py",
        "tests/test_main_wiring.py",
        "tests/test_agent_replacement_guard.py",
        "docs/agent.md",
        "docs/dev/agent-decisions.md",
        "docs/release-notes/unreleased.md",
    }
)

#: Documentation that describes today's program. Historical specs, plans and
#: superpowers records are excluded by design — they are the audit trail of
#: the migration and must keep naming what it removed.
_HISTORICAL_DOC_PREFIXES = (
    "docs/dev/specs/",
    "docs/dev/plans/",
    "docs/superpowers/",
)


def _python_sources(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _current_docs() -> list[Path]:
    docs = _REPO_ROOT / "docs"
    return sorted(
        path
        for path in docs.rglob("*.md")
        if not _relative(path).startswith(_HISTORICAL_DOC_PREFIXES)
    )


def _found(text: str, needles: Iterable[str]) -> list[str]:
    return [needle for needle in needles if needle in text]


_SRC_FILES = _python_sources(_SRC)
_TEST_FILES = [path for path in _python_sources(_TESTS) if path.name != Path(__file__).name]
_DOC_FILES = _current_docs()
_MARKDOWN_FILES = [*_DOC_FILES, _REPO_ROOT / "README.md"]


# ---------------------------------------------------------------------------
# The modules are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _RETIRED_FILES)
def test_the_retired_agent_module_file_is_deleted(name: str) -> None:
    assert not (_AGENT_PACKAGE / name).exists(), f"src/korvid/agent/{name} is still on disk"


@pytest.mark.parametrize("module", _RETIRED_MODULES)
def test_the_retired_agent_module_cannot_be_imported(module: str) -> None:
    """Absent from the tree *and* from the import system.

    A stale `.pth`, a namespace package, or a re-export shim would make the
    file check pass while `import korvid.agent.runtime` still worked.
    """
    assert importlib.util.find_spec(module) is None
    with pytest.raises(ModuleNotFoundError, match=module.rsplit(".", 1)[-1]):
        importlib.import_module(module)


def test_the_agent_package_ships_exactly_the_harness_modules() -> None:
    """The final module list, so an added module is a reviewed decision."""
    present = {path.name for path in _AGENT_PACKAGE.glob("*.py")}
    assert present == {
        "__init__.py",
        "conversation.py",
        "credentials.py",
        "engine.py",
        "events.py",
        "evidence.py",
        "install_hint.py",
        "interaction.py",
        "model_catalog.py",
        "model_policy.py",
        "native_engine.py",
        "navigation.py",
        "outbound.py",
        "prompt_harness.py",
        "prompt_packs.py",
        "provider.py",
        "provider_plugin.py",
        "request_gateway.py",
        "session.py",
        "setup.py",
        "tool_harness.py",
    }


# ---------------------------------------------------------------------------
# No surviving mention of the retired surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _SRC_FILES, ids=_relative)
def test_no_source_file_names_a_retired_agent_symbol(path: Path) -> None:
    found = _found(path.read_text(encoding="utf-8"), _RETIRED_SYMBOLS)
    assert found == [], f"{_relative(path)} still names {found}"


@pytest.mark.parametrize("path", _TEST_FILES, ids=_relative)
def test_no_current_test_names_a_retired_agent_symbol(path: Path) -> None:
    found = _found(path.read_text(encoding="utf-8"), _RETIRED_SYMBOLS)
    assert found == [], f"{_relative(path)} still names {found}"


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_doc_names_a_retired_agent_symbol(path: Path) -> None:
    """Pages describing today's program must name today's classes.

    A reader who follows `AgentRuntime` out of the eval methodology finds a
    module that no longer exists instead of `DefaultAgentSession`, which is
    what actually persists across a journey.
    """
    found = _found(path.read_text(encoding="utf-8"), _RETIRED_SYMBOLS)
    assert found == [], f"{_relative(path)} still names {found}"


def test_removed_config_keys_appear_only_on_migration_surfaces() -> None:
    """`agent.profile`/`agent.prompts` survive only as migration advice.

    The startup error that names them is the whole point of keeping the
    strings: an operator with an old `config.yaml` must be told what to
    write instead. Anywhere else, the key reads as a supported option.
    """
    offenders = sorted(
        _relative(path)
        for path in (*_SRC_FILES, *_TEST_FILES, *_MARKDOWN_FILES)
        if _found(path.read_text(encoding="utf-8"), _REMOVED_CONFIG_KEYS)
    )
    assert set(offenders) <= _MIGRATION_SURFACES, (
        f"removed config keys named outside the migration surfaces: "
        f"{sorted(set(offenders) - _MIGRATION_SURFACES)}"
    )


def test_the_startup_migration_error_still_names_both_removed_keys() -> None:
    """The allowance above is only safe while the advice actually exists."""
    config = (_SRC / "core" / "config.py").read_text(encoding="utf-8")
    assert "agent.profile was removed" in config
    assert "agent.model_tier" in config
    assert "agent.prompts was removed" in config
    assert "agent.rules" in config


# ---------------------------------------------------------------------------
# One implementation, no selector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*_SRC_FILES, *_TEST_FILES], ids=_relative)
def test_no_module_declares_an_agent_backend_selector(path: Path) -> None:
    found = _found(path.read_text(encoding="utf-8"), _FORBIDDEN_SELECTORS)
    assert found == [], f"{_relative(path)} reintroduces a backend selector: {found}"


def _subclass_names(base: str) -> set[str]:
    """Every class in `src/korvid` whose declared bases include `base`."""
    found: set[str] = set()
    for path in _SRC_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for parent in node.bases:
                name = (
                    parent.attr if isinstance(parent, ast.Attribute) else getattr(parent, "id", "")
                )
                if name == base:
                    found.add(node.name)
    return found


def test_exactly_one_agent_engine_implementation_ships() -> None:
    assert _subclass_names("AgentEngine") == {"NativeAgentEngine"}
    assert issubclass(NativeAgentEngine, AgentEngine)


def test_exactly_one_production_agent_session_ships() -> None:
    assert _subclass_names("AgentSession") == {"DefaultAgentSession"}
    assert issubclass(DefaultAgentSession, AgentSession)


# ---------------------------------------------------------------------------
# One coherent public surface
# ---------------------------------------------------------------------------

#: The contracts the agent layer publishes to the composition root, the UI,
#: and provider plugins — grouped the way the layer is built.
_PUBLIC_SURFACE = {
    # interaction (task 1)
    "AgentUiBridge",
    "ClusterFacts",
    "DrillDown",
    "InteractionContext",
    "Navigate",
    "OpenDescribe",
    "OpenLogs",
    "PaneContext",
    "ResourceIdentity",
    "SetFilter",
    "UiAction",
    "UiActionResult",
    # model routing (task 5)
    "CapabilitySource",
    "ModelCapabilities",
    "ModelCatalogEntry",
    "ModelDescriptor",
    "ModelRouter",
    "ModelRoutingError",
    "ModelTier",
    "PolicyEnvironment",
    "ResolvedAgentPolicy",
    # prompt harness (task 6)
    "ComposedPrompt",
    "PromptCompositionError",
    "PromptHarness",
    "PromptInputs",
    "StaticPromptTooLargeError",
    "UnknownPromptOverlayError",
    "UnknownPromptPackError",
    "cluster_context_note",
    # request gateway (task 8)
    "OutboundPolicy",
    "OutboundSnapshot",
    "PreparedGatewayRequest",
    "RequestGateway",
    # tool harness (task 9)
    "ToolExecution",
    "ToolHarness",
    # engine (task 10)
    "AgentEngine",
    "AgentTurnRequest",
    "NativeAgentEngine",
    # session (task 11)
    "AgentSession",
    "DefaultAgentSession",
    "SessionRetargetError",
    # provider contract
    "REQUEST_SENT",
    "LLMProvider",
    # evidence (task 12)
    "Evidence",
    "EvidenceLedger",
    # events
    "AgentError",
    "AgentEvent",
    "TextDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnComplete",
    "TurnInterrupted",
    # setup
    "AgentConfigurator",
    "AgentSettings",
    "DeviceLoginPrompt",
}


def test_the_agent_package_publishes_the_final_public_surface() -> None:
    import korvid.agent as agent_package

    assert set(agent_package.__all__) == _PUBLIC_SURFACE
    assert len(agent_package.__all__) == len(set(agent_package.__all__))
    assert list(agent_package.__all__) == sorted(agent_package.__all__)


@pytest.mark.parametrize("name", sorted(_PUBLIC_SURFACE))
def test_every_published_agent_name_resolves(name: str) -> None:
    import korvid.agent as agent_package

    assert getattr(agent_package, name) is not None


def test_an_unpublished_agent_name_raises_attribute_error() -> None:
    import korvid.agent as agent_package

    with pytest.raises(AttributeError, match="AgentRuntim"):
        getattr(agent_package, "AgentRuntim" + "e")


def test_no_published_name_carries_a_version_suffix() -> None:
    """`v1`/`v2` existed only to tell two implementations apart."""
    import korvid.agent as agent_package

    suffixed = [name for name in agent_package.__all__ if name.lower().endswith(("v1", "v2"))]
    assert suffixed == []


# ---------------------------------------------------------------------------
# The guard is not scanning an empty world
# ---------------------------------------------------------------------------


def test_the_guard_scans_the_files_it_claims_to() -> None:
    src = {_relative(path) for path in _SRC_FILES}
    tests = {_relative(path) for path in _TEST_FILES}
    docs = {_relative(path) for path in _MARKDOWN_FILES}

    assert "src/korvid/agent/session.py" in src
    assert "src/korvid/agent/native_engine.py" in src
    assert "src/korvid/__main__.py" in src
    assert "tests/agent/test_session.py" in tests
    assert "tests/evals/test_journeys_cli.py" in tests
    assert {"docs/agent.md", "docs/evals/methodology.md", "docs/threat-model.md"} <= docs
    assert not any(path.startswith("docs/dev/specs/") for path in docs)
    assert not any(path.startswith("docs/superpowers/") for path in docs)


# ---------------------------------------------------------------------------
# The package docstring describes the surface it actually publishes
# ---------------------------------------------------------------------------

#: Contracts a third party implements that are deliberately *not* in
#: `__all__`: importing them eagerly would drag the plugin validator (and
#: its `ModelCapabilities` import graph) into every start, which is the
#: boundary `tests/test_optional_extras.py` pins. They stay reachable at
#: their own submodule, and the docstring has to say so rather than imply
#: `korvid.agent` publishes every contract a plugin needs.
_SUBMODULE_ONLY_CONTRACTS = {
    "korvid.agent.provider_plugin": (
        "ProviderPlugin",
        "ProviderPluginConfig",
        "ProviderPluginMetadata",
        "PROVIDER_PLUGIN_API_VERSION",
    ),
    "korvid.agent.credentials": ("CredentialSource",),
}


def test_the_provider_plugin_contracts_are_not_published_by_the_package() -> None:
    """The premise of the docstring fix: these really are submodule-only."""
    import korvid.agent as agent_package

    published = set(agent_package.__all__)
    for names in _SUBMODULE_ONLY_CONTRACTS.values():
        assert published.isdisjoint(names), sorted(published & set(names))


@pytest.mark.parametrize("module", sorted(_SUBMODULE_ONLY_CONTRACTS))
def test_each_submodule_only_contract_resolves_where_the_docstring_sends_readers(
    module: str,
) -> None:
    imported = importlib.import_module(module)
    for name in _SUBMODULE_ONLY_CONTRACTS[module]:
        assert getattr(imported, name) is not None


def test_the_package_docstring_names_the_submodules_that_own_them() -> None:
    """A plugin author reading `korvid.agent` must be sent somewhere real.

    The docstring used to read as though `__all__` were every contract the
    layer offers, which sends a plugin author looking for `ProviderPlugin`
    in a surface that does not carry it.
    """
    import korvid.agent as agent_package

    doc = agent_package.__doc__ or ""
    assert "provider_plugin" in doc
    assert "credentials" in doc
    for names in _SUBMODULE_ONLY_CONTRACTS.values():
        for name in names:
            assert name in doc or name.lower() in doc.lower()


def test_naming_the_submodules_does_not_make_the_package_import_them() -> None:
    """Accuracy in prose, not an eager import: the docstring costs nothing."""
    probe = (
        "import sys\n"
        "import korvid.agent  # noqa: F401\n"
        "leaked = [m for m in "
        "('korvid.agent.provider_plugin', 'korvid.agent.credentials', "
        "'korvid.agent.provider') if m in sys.modules]\n"
        "if leaked:\n"
        "    raise SystemExit(f'eager import: {leaked}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


def test_the_package_docstring_counts_the_submodules_not_the_contracts() -> None:
    """The docstring said "two contracts" over a list of five names.

    A plugin author counting them finds `ProviderPlugin`,
    `ProviderPluginMetadata`, `ProviderPluginConfig`,
    `PROVIDER_PLUGIN_API_VERSION` and `CredentialSource` — five contracts
    in two submodules. A prose count that disagrees with its own list
    makes the reader wonder which three were left out.
    """
    import korvid.agent as agent_package

    doc = agent_package.__doc__ or ""
    named = sum(len(names) for names in _SUBMODULE_ONLY_CONTRACTS.values())

    assert len(_SUBMODULE_ONLY_CONTRACTS) == 2
    assert named == 5
    assert "Two public contracts" not in doc
    assert "two submodules" in doc


# ---------------------------------------------------------------------------
# The typed UI action surface korvid actually ships
# ---------------------------------------------------------------------------

#: The action classes an armed registry tool can produce, and therefore the
#: whole `UiAction` union. Named here (not derived) so a member added
#: without a tool behind it fails this file, which is where the rule lives.
_SHIPPED_UI_ACTIONS = ("Navigate", "SetFilter", "OpenLogs", "OpenDescribe", "DrillDown")

#: Action classes that shipped in the union with no tool able to produce
#: them: three dataclasses, three eval-bridge branches and three live-bridge
#: branches implementing an action the model could never call.
_UNREACHABLE_UI_ACTIONS = ("SelectResource", "FocusPane", "OpenEvidence")

#: The design and plan pages for the harness. Excluded from the historical
#: allowance above precisely because they describe the surface as *shipped*:
#: a reader takes the action list in them for what exists today.
_HARNESS_DESIGN_PAGES = (
    "docs/superpowers/specs/2026-08-23-agent-interaction-harness-design.md",
    "docs/superpowers/plans/2026-08-23-agent-interaction-harness.md",
)


def test_the_union_is_exactly_the_actions_a_registry_tool_can_produce() -> None:
    from korvid.agent import interaction

    union = {member.__name__ for member in interaction.UiAction.__args__}

    assert union == set(_SHIPPED_UI_ACTIONS)
    for name in _UNREACHABLE_UI_ACTIONS:
        assert not hasattr(interaction, name), f"{name} is back in the interaction module"


@pytest.mark.parametrize("name", _UNREACHABLE_UI_ACTIONS)
def test_no_unreachable_action_survives_anywhere_in_the_tree(name: str) -> None:
    """Union, exports, both bridges, and the eval recorder — one sweep.

    The three implementations of each removed action were spread across
    `agent/interaction.py`, `agent/__init__.py`, `ui/agent_workspace_bridge.py`
    and `evals/interaction.py`; a partial deletion leaves a branch that
    cannot run but must still be maintained.
    """
    offenders = [
        _relative(path)
        for path in (*_SRC_FILES, *_TEST_FILES)
        if name in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"{name} still appears in {offenders}"


@pytest.mark.parametrize("page", _HARNESS_DESIGN_PAGES)
def test_the_harness_design_pages_describe_the_shipped_action_surface(page: str) -> None:
    """The pages a reader plans the next action from must name what ships.

    They may keep describing the migration, but the list of typed actions
    has to be the five korvid arms, plus the rule that made three of the
    original eight unreachable: a new action starts with a registry schema
    and eval evidence, not with a dataclass.
    """
    text = (_REPO_ROOT / page).read_text(encoding="utf-8")

    for action in ("navigate", "filter", "logs", "describe", "drill"):
        assert action in text.lower(), f"{page} does not name the shipped {action} action"
    assert "registry" in text.lower()
    assert "eval" in text.lower()
    for name in _UNREACHABLE_UI_ACTIONS:
        offenders = [
            line
            for line in text.splitlines()
            if name in line and "never shipped" not in line.lower()
        ]
        assert offenders == [], f"{page} still offers {name}: {offenders}"
