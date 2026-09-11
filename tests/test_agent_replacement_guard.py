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
- known backend selectors, transition flags and versioned engine/session
  aliases stay absent;
- exactly one `AgentEngine` and one production `AgentSession` exist;
- importing the agent namespace does not load the runtime.

Historical records are deliberately out of scope: `docs/dev/specs/`,
`docs/dev/plans/` and `docs/superpowers/` describe how korvid got here and
must keep naming what they retired.
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
        "diagnostics.py",
        "engine.py",
        "events.py",
        "evidence.py",
        "install_hint.py",
        "interaction.py",
        "model_catalog.py",
        "model_policy.py",
        "model_profiles.py",
        "native_engine.py",
        "navigation.py",
        "outbound.py",
        "prompt_harness.py",
        "prompt_packs.py",
        "provider.py",
        "request_gateway.py",
        "session.py",
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
# A lightweight namespace with explicit module contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eager_import", [False, True], ids=["namespace-only", "eager-control"])
def test_importing_the_agent_namespace_does_not_load_its_runtime(eager_import: bool) -> None:
    """The optimized child must reject an intentionally eager control too."""
    extra_import = "import korvid.agent.model_policy\n" if eager_import else ""
    probe = (
        "import sys\n"
        "import korvid.agent\n"
        f"{extra_import}"
        "loaded = [name for name in sys.modules if name.startswith('korvid.agent.')]\n"
        "if loaded:\n"
        "    raise SystemExit(f'eager agent imports: {loaded}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", probe], capture_output=True, text=True, timeout=120
    )
    if eager_import:
        assert result.returncode == 1
        assert result.stderr.startswith("eager agent imports: ")
        assert "korvid.agent.model_policy" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


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


# ---------------------------------------------------------------------------
# The retired vocabulary is gone from the comments too
# ---------------------------------------------------------------------------

#: Wording from the retired profile arms that survived the rename inside
#: comments and docstrings. `docs/test_docs_agent_contracts.py` already
#: refuses it in operator-facing prose; a comment is read by the next
#: person changing that budget, and one that calls it a "profile budget"
#: sends them looking for a knob korvid removed. The replacements are the
#: shipped words: `tier result budget` and `low-tier budget`.
_RETIRED_TIER_VOCABULARY = ("profile budget", "small-profile", "full-profile")


@pytest.mark.parametrize("path", [*_SRC_FILES, *_TEST_FILES], ids=_relative)
def test_no_module_describes_a_budget_in_retired_profile_words(path: Path) -> None:
    found = _found(path.read_text(encoding="utf-8"), _RETIRED_TIER_VOCABULARY)
    assert found == [], f"{_relative(path)} still calls a tier budget {found}"


def test_the_shipped_budget_words_are_the_ones_in_use() -> None:
    """The rename is only complete if the replacement wording exists.

    A guard that only forbids can be satisfied by deleting the sentence,
    which loses the explanation the comment carried.
    """
    evidence = (_SRC / "agent" / "evidence.py").read_text(encoding="utf-8")
    outbound = (_SRC / "agent" / "outbound.py").read_text(encoding="utf-8")

    assert "low-tier budget" in evidence
    assert "tier budget" in outbound
