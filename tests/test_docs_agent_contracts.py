"""Current documentation may only claim what the agent layer really does.

`tests/test_agent_replacement_guard.py` proves the retired *names* are gone.
This module proves the surviving *claims* are true, for the four that a
reader acts on:

- the security perimeter an operator relies on — a doc naming a tool korvid
  does not ship (`run_kubectl`) describes a validation step nothing performs,
  and so does a doc claiming `ToolExecutor` validates every call against a
  JSON schema: the real controls are the policy arming only exact registry
  names, the registry validating dispatch targets against import-time
  metadata, and the executor rejecting an unknown tool and performing its
  own explicit, typed argument validation;
- the provider-plugin API version a third party writes against;
- the low-tier prompt-pack and tool-description constraints an eval campaign
  has to hold fixed to keep its numbers comparable — including that the low
  tier's wording is *not* identical to what the high tier and the MCP server
  still read from the registry.

Historical records (`docs/dev/specs/`, `docs/dev/plans/`, `docs/superpowers/`)
are out of scope by the same rule the replacement guard uses: they record what
korvid intended or retired, not what it ships today.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from korvid.agent.provider_plugin import PROVIDER_PLUGIN_API_VERSION
from korvid.tools.registry import TOOLS_BY_NAME

_REPO_ROOT = Path(__file__).parents[1]

_HISTORICAL_DOC_PREFIXES = (
    "docs/dev/specs/",
    "docs/dev/plans/",
    "docs/superpowers/",
)


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _current_markdown() -> list[Path]:
    docs = _REPO_ROOT / "docs"
    pages = sorted(
        path
        for path in docs.rglob("*.md")
        if not _relative(path).startswith(_HISTORICAL_DOC_PREFIXES)
    )
    return [*pages, _REPO_ROOT / "README.md"]


_MARKDOWN_FILES = _current_markdown()


def _text(name: str) -> str:
    return (_REPO_ROOT / name).read_text(encoding="utf-8")


def test_the_scan_really_covers_the_operator_facing_pages() -> None:
    """A guard over an empty file list passes for the wrong reason."""
    scanned = {_relative(path) for path in _MARKDOWN_FILES}
    assert {
        "README.md",
        "docs/agent.md",
        "docs/ops.md",
        "docs/evals/methodology.md",
        "docs/provider-plugins.md",
        "docs/release-notes/unreleased.md",
        "docs/dev/ui-controllers.md",
    } <= scanned
    assert not any(path.startswith(_HISTORICAL_DOC_PREFIXES) for path in scanned)


# ---------------------------------------------------------------------------
# 1. The documented security perimeter is the one that exists
# ---------------------------------------------------------------------------


def test_korvid_ships_no_shell_tool_to_the_agent() -> None:
    """The premise of the claim below: there is no `run_kubectl` to validate."""
    assert "run_kubectl" not in TOOLS_BY_NAME
    assert not [name for name in TOOLS_BY_NAME if "kubectl" in name or "shell" in name]


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_page_claims_a_shell_tool_validation(path: Path) -> None:
    """`run_kubectl` was never armed on this surface.

    Claiming korvid validates a (verb x resource x flags) triple tells an
    operator a control exists. Nothing performs it, so the sentence is a
    security claim with no code behind it.
    """
    assert "run_kubectl" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["docs/ops.md", "docs/release-notes/unreleased.md"])
def test_the_perimeter_pages_state_the_boundary_that_really_runs(page: str) -> None:
    """What replaces the fabricated claim has to be the real perimeter.

    `ToolExecutor` never runs JSON-schema validation — a tool's declared
    schema is model-facing wording, not the runtime check. The real
    controls: the resolved policy arms only the registry's own exact tool
    names; the registry validates every dispatch target against
    import-time metadata; and the executor rejects a name outside that
    registry as an unknown tool and performs its own explicit, typed
    argument validation before a write reaches the cluster.
    """
    text = _text(page)
    assert "structured" in text
    assert "ToolExecutor" in text
    assert "no shell" in text
    # The controls that actually run, stated precisely, on both pages.
    assert "approval dialog" in text
    assert "keystroke" in text
    assert "resourceVersion" in text
    assert "fail-closed" in text
    assert "masking" in text
    assert "exact" in text
    assert "registry" in text
    assert "import-time" in text
    assert "unknown tool" in text
    assert "typed argument validation" in text


_SCHEMA_VALIDATION_OVERCLAIM = re.compile(
    r"validat\w*\s+(?:the\s+)?arguments?\s+against\s+"
    r"(?:each\s+tool.s|its|the)\s+declared\s+schema",
    re.IGNORECASE,
)


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_page_claims_the_executor_validates_against_a_declared_schema(
    path: Path,
) -> None:
    """`ToolExecutor` does not run JSON-schema validation.

    It rejects a name outside the registry as an unknown tool and performs
    its own explicit, typed argument checks (`isinstance` on `kind`,
    `name`, `namespace`, `replicas`, `resources`); the declared OpenAI-style
    schema is model-facing wording the registry hands the provider, never
    the runtime control. Whitespace is normalized before matching so the
    claim cannot hide by wrapping across a line break.
    """
    normalized = " ".join(path.read_text(encoding="utf-8").split())
    assert not _SCHEMA_VALIDATION_OVERCLAIM.search(normalized), _relative(path)


_IDENTICAL_TOOL_WORDING_OVERCLAIM = re.compile(
    r"(?:describes?|describing)\s+(?:a|every)\s+tool\s+identically", re.IGNORECASE
)


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_page_claims_every_surface_describes_tools_identically(
    path: Path,
) -> None:
    """The low tier ships its own shipped, versioned tool wording.

    `LOW_TOOL_DESCRIPTIONS` replaces the registry's wording, by exact tool
    name, on the low route only. The high tier and the MCP server still
    describe every tool with the registry's own text, so "every surface
    describes a tool identically" was never true once the low map shipped.
    """
    normalized = " ".join(path.read_text(encoding="utf-8").split())
    assert not _IDENTICAL_TOOL_WORDING_OVERCLAIM.search(normalized), _relative(path)


@pytest.mark.parametrize("page", ["docs/agent.md", "docs/release-notes/unreleased.md"])
def test_the_tool_description_removal_note_names_which_arm_uses_which_wording(
    page: str,
) -> None:
    """The migration note for `agent.prompts.tool_descriptions` has to say
    what actually replaced it: per-deployment overrides are gone, the low
    tier ships its own versioned wording, and the high tier plus the MCP
    server still read the registry's.
    """
    text = _text(page)
    assert "removed" in text
    assert "low" in text.casefold()
    assert "registry" in text
    assert "MCP" in text


# ---------------------------------------------------------------------------
# 2. The provider-plugin API version a third party writes against
# ---------------------------------------------------------------------------

_API_V1_SPELLINGS = re.compile(r"\bAPI[-\s]v1\b", re.IGNORECASE)


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_page_sends_a_plugin_author_to_api_1(path: Path) -> None:
    """Only the API 1 → API 2 migration tables may name the retired version.

    A reader who follows "the API-v1 contract" writes a plugin against a
    contract `ValidatedPluginProvider` rejects.
    """
    text = path.read_text(encoding="utf-8")
    offenders = [
        line for line in text.splitlines() if _API_V1_SPELLINGS.search(line) and "API 2" not in line
    ]
    assert offenders == [], f"{_relative(path)} points at the retired plugin API: {offenders}"


@pytest.mark.parametrize("page", ["docs/agent.md", "README.md"])
def test_the_plugin_pointers_name_the_shipped_api_version(page: str) -> None:
    assert PROVIDER_PLUGIN_API_VERSION == 2
    assert f"API {PROVIDER_PLUGIN_API_VERSION}" in _text(page)


# ---------------------------------------------------------------------------
# 3. No stale capability-profile or runtime-profile prose
# ---------------------------------------------------------------------------

_PROFILE_PROSE = re.compile(r"(capability|runtime)[- ]profile", re.IGNORECASE)

#: A page may still *name* a profile while saying it is gone — the eval
#: pages have to describe the arm their published campaigns really ran on.
#: Checked over a small window rather than one line because the sentence
#: that retires a name often wraps past it.
_HISTORICAL_MARKERS = ("retired", "deleted", "predates", "pre-tier", "historical")
_MARKER_WINDOW = 2


def _historically_marked(lines: list[str], index: int) -> bool:
    window = lines[max(0, index - _MARKER_WINDOW) : index + _MARKER_WINDOW + 1]
    joined = " ".join(window).casefold()
    return any(marker in joined for marker in _HISTORICAL_MARKERS)


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_page_offers_a_capability_profile_as_a_feature(path: Path) -> None:
    """Profiles were replaced by `agent.model_tier`.

    A reader of a feature list acts on it: "capability profiles for small
    local models" describes a knob `KorvidConfig` now rejects at startup.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders = [
        line
        for index, line in enumerate(lines)
        if _PROFILE_PROSE.search(line) and not _historically_marked(lines, index)
    ]
    assert offenders == [], f"{_relative(path)} still offers a profile: {offenders}"


def test_the_readme_feature_line_names_the_model_tier() -> None:
    readme = _text("README.md")
    assert "model tier" in readme or "model_tier" in readme


def test_the_controller_reference_describes_todays_seams() -> None:
    """`ui-controllers.md` documents an owner list, so it has to be current."""
    controllers = _text("docs/dev/ui-controllers.md")
    assert "note_context_switch" not in controllers
    assert "AgentSession" in controllers
    assert "InteractionContext" in controllers


# ---------------------------------------------------------------------------
# 4. The low-tier prompt-pack constraints an eval campaign depends on
# ---------------------------------------------------------------------------


def test_the_eval_methodology_states_the_low_pack_constraints() -> None:
    """A grind that changes these silently invalidates every published row."""
    methodology = _text("docs/evals/methodology.md")
    assert "LOW_TOOL_DESCRIPTIONS" in methodology
    assert "250" in methodology
    assert "exact tool name" in methodology
    # The high tier keeps the registry wording — the two arms are not the same.
    assert "high tier" in methodology
    # And a change is only landable with the retained cases re-run.
    assert "liveness-probe-failing" in methodology
    assert "oom-killed" in methodology


def test_the_agent_page_states_what_the_low_tier_changes_about_tool_text() -> None:
    agent = _text("docs/agent.md")
    assert "LOW_TOOL_DESCRIPTIONS" in agent
    assert "exact tool name" in agent


def test_the_low_pack_documentation_publishes_no_score() -> None:
    """`docs/evals/scoreboard.md` publishes numbers; prose sections must not.

    A percentage next to a prompt-pack rule reads as a measured result. The
    low-pack sections were written from the retained eval cases, not from a
    new campaign, so they state constraints and name cases — never a score.
    """
    methodology = _text("docs/evals/methodology.md")
    start = methodology.index("LOW_TOOL_DESCRIPTIONS")
    end = methodology.find("\n## ", start)
    section = methodology[start:] if end == -1 else methodology[start:end]
    assert not re.search(r"\d+(\.\d+)?\s?%", section), section
