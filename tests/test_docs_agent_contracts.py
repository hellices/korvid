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
    assert "UID" in text
    assert "fail-closed" in text
    assert "masking" in text
    assert "exact" in text
    assert "registry" in text
    assert "import-time" in text
    assert "unknown tool" in text
    assert "typed argument validation" in text


_SCHEMA_VALIDATION_OVERCLAIM = re.compile(
    r"validate\w*\s+(?:the\s+)?arguments?\s+against\s+"
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


@pytest.mark.parametrize("page", ["docs/release-notes/unreleased.md"])
def test_the_tool_description_removal_note_names_which_arm_uses_which_wording(
    page: str,
) -> None:
    """The migration note for the retired tool-description override has to
    say what actually replaced it: per-deployment overrides are gone, the
    low tier ships its own versioned wording, and the high tier plus the
    MCP server still read the registry's.

    The note is release history, so it lives on the release note. The Agent
    guide describes the product a reader operates today and links there;
    `test_the_agent_page_links_the_migration_note_instead_of_restating_it`
    pins that boundary.
    """
    text = _text(page)
    assert "removed" in text
    assert "low" in text.casefold()
    assert "registry" in text
    assert "MCP" in text


def test_the_agent_page_links_the_migration_note_instead_of_restating_it() -> None:
    """A product guide is not a migration manual.

    The keys the startup error retires (read out of `core/config.py` rather
    than spelled here, so this test cannot name a key as if it were
    supported) were replaced a release ago. The table mapping them onto
    today's settings is release history: `docs/release-notes/unreleased.md`
    owns it, the startup error itself names the replacement, and the guide
    describes what an operator configures today.
    """
    config = (_REPO_ROOT / "src" / "korvid" / "core" / "config.py").read_text(encoding="utf-8")
    removed_keys = re.findall(r"\"(agent\.\w+) was removed", config)
    assert removed_keys, "the startup migration error must still name the retired keys"

    agent = _text("docs/agent.md")
    assert "Upgrading from the profile-based agent" not in agent
    assert [key for key in removed_keys if key in agent] == []
    assert "model_tier" in agent, "the supported key still has to be on the page"

    notes = _text("docs/release-notes/unreleased.md")
    assert [key for key in removed_keys if key in notes] == removed_keys, (
        "the release note is where a reader with an old config.yaml is sent"
    )


def test_the_agent_page_states_the_eval_harness_packaging_boundary() -> None:
    """The methodology link needs its prerequisite next to it, not a click away.

    `pyproject.toml` genuinely excludes `korvid.evals` from wheels and
    source distributions (`[tool.hatch.build] exclude`). A reader who
    `pip install`s korvid and then follows the methodology link has no way
    to know the harness is not there until it fails to import — the guide
    has to say so, and give the exact recovery command, right beside the
    link rather than only on the page it points to.
    """
    pyproject = _text("pyproject.toml")
    assert "src/korvid/evals" in pyproject, "packaging must still exclude the harness"

    agent = _text("docs/agent.md")
    assert "evals/methodology.md" in agent
    window = agent[agent.index("evals/methodology.md") - 400 :][:800]
    assert "development-only" in window
    assert "wheel" in window
    assert "sdist" in window or "source distribution" in window
    assert "uv sync --frozen --dev --all-extras" in window


def test_the_agent_page_states_cloud_provider_detection_truthfully() -> None:
    """The cluster-detection fact belongs on the page it was cut from.

    `korvid.k8s.csp.detect_provider` recognizes exactly the AKS/EKS/GKE
    managed-distribution node labels and falls back to `UNKNOWN_PROVIDER`
    for everything else, including an RBAC-limited, bare-metal, or local
    cluster. Task 2 dropped the paragraph describing this without folding
    it into a surviving section; this pins a concise replacement instead
    of a restored multi-sentence feature walkthrough.
    """
    from korvid.k8s.csp import _MANAGED_LABELS, UNKNOWN_PROVIDER

    distributions = {dist.upper() for dist, _ in _MANAGED_LABELS.values()}
    assert distributions == {"AKS", "EKS", "GKE"}
    assert UNKNOWN_PROVIDER == "unknown"

    agent = _text("docs/agent.md")
    for name in sorted(distributions):
        assert name in agent
    assert "node metadata" in agent
    window = agent[agent.index("node metadata") - 300 :][:600]
    assert "best-effort" in window
    assert "RBAC" in window
    assert "bare-metal" in window
    assert "unknown" in window.casefold()


def test_the_ollama_row_names_the_namespace_its_six_keys_actually_live_under() -> None:
    """The tuning knobs are read out of `agent.ollama.*`, not the bare names.

    `Config` groups exactly six `agent_ollama_<key>` fields directly under
    the "Native Ollama tuning (issue #72): `agent.ollama.*` in config.yaml"
    comment, ending at the unrelated `keybindings` field. The provider
    table's Ollama row lists the six key names but, before this test, never
    said which namespace an operator has to nest them under in
    `config.yaml` — `num_ctx: 32768` at the top level of the agent block is
    silently ignored. Both the six keys and the `agent.ollama` namespace
    they require have to be on the page.
    """
    config = _text("src/korvid/core/config.py")
    start = config.index("Native Ollama tuning (issue #72)")
    end = config.index("keybindings", start)
    block = config[start:end]
    keys = re.findall(r"agent_ollama_(\w+):", block)
    assert keys == ["num_ctx", "temperature", "seed", "think", "keep_alive", "num_predict"], (
        "the six ollama keys config.py actually defines must drive this test, not a hand-written list"
    )

    agent = _text("docs/agent.md")
    row = next(line for line in agent.splitlines() if line.strip().startswith("| Ollama"))
    for key in keys:
        assert key in row, f"the Ollama row must still name {key}"
    assert "agent.ollama" in row, (
        "the Ollama row must say the six keys nest under the `agent.ollama` namespace"
    )


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


def test_the_agent_page_sends_low_tier_wording_questions_to_the_methodology() -> None:
    """The low tier's shipped wording is an eval contract, not product copy.

    `LOW_TOOL_DESCRIPTIONS`, its 250-character bound and its exact-tool-name
    application decide whether two campaigns are comparable — a question the
    eval methodology owns and
    `test_the_eval_methodology_states_the_low_pack_constraints` pins. The
    Agent guide tells an operator which tier is routed and what changes with
    it, then links to that page rather than shipping a second, driftable copy
    of the constraints.
    """
    agent = _text("docs/agent.md")

    assert "evals/methodology.md" in agent
    assert "LOW_TOOL_DESCRIPTIONS" not in agent
    assert "prompt_packs.py" not in agent
    # The product-visible half of the tier stays: which tier, and the budgets.
    assert "model_tier" in agent


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


# ---------------------------------------------------------------------------
# 5. No retired arm name offered as a feature, in prose or in a docstring
# ---------------------------------------------------------------------------

#: The two arm names the retired profile key took. They were replaced by
#: `agent.model_tier` (`low`/`high`/absent), so a page still offering one
#: is describing a knob `KorvidConfig` rejects at startup.
_RETIRED_ARM_PROSE = re.compile(r"`?(small|full)`?[- ]profile", re.IGNORECASE)

#: A published release note records what *that* release shipped and is not
#: rewritten; only `unreleased.md` describes the program being built.
_CURRENT_PAGES = [
    path
    for path in _MARKDOWN_FILES
    if not _relative(path).startswith("docs/release-notes/")
    or _relative(path) == "docs/release-notes/unreleased.md"
]


@pytest.mark.parametrize("path", _CURRENT_PAGES, ids=_relative)
def test_no_current_page_offers_a_small_or_full_profile(path: Path) -> None:
    """The README's feature list is the first thing a new user reads.

    "including a `small` profile tuned for 3B-14B local models" names an
    arm korvid no longer has; the equivalent today is the low model tier,
    which is also what an operator has to write in config.yaml.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders = [
        line
        for index, line in enumerate(lines)
        if _RETIRED_ARM_PROSE.search(line) and not _historically_marked(lines, index)
    ]

    assert offenders == [], f"{_relative(path)} still offers a retired arm: {offenders}"


def test_the_release_note_scan_still_covers_the_pages_it_should() -> None:
    """The exclusion above must not quietly empty the parametrisation."""
    scanned = {_relative(path) for path in _CURRENT_PAGES}

    assert "README.md" in scanned
    assert "docs/overview.md" in scanned
    assert "docs/release-notes/unreleased.md" in scanned
    assert not any(
        page.startswith("docs/release-notes/") and page != "docs/release-notes/unreleased.md"
        for page in scanned
    )


def test_the_agent_ui_controller_docstring_names_what_it_owns_today() -> None:
    """A module docstring is read like documentation, so it is held to it.

    `AgentUiController` holds `_configured_tier` — the explicit
    `agent.model_tier` the wizard seeds from — and has held no capability
    profile since the tier replaced it.
    """
    import korvid.ui.agent_ui_controller as controller_module

    doc = controller_module.__doc__ or ""

    assert "capability profile" not in doc
    assert "model tier" in doc


def test_the_ui_controller_reference_describes_the_state_it_really_owns() -> None:
    """The same claim in `docs/dev/ui-controllers.md`'s owner list."""
    controllers = _text("docs/dev/ui-controllers.md")

    assert "settings / profile" not in controllers
    assert "model tier" in controllers


# ---------------------------------------------------------------------------
# 6. What the model reads, and what the operator reads about it
# ---------------------------------------------------------------------------


def test_the_release_notes_record_the_truncation_marker_the_model_reads() -> None:
    """A marker change is model-visible, so it is a release note.

    `_MIDDLE_TRUNCATION_MARKER` is inserted into a tool result the model
    consumes: its wording is part of the prompt every over-long read
    produces, and an eval campaign comparing runs across this change is
    comparing two slightly different inputs. The note is what tells a
    reader (and a future campaign) which side of it a number came from.
    """
    from korvid.tools.executor import _MIDDLE_TRUNCATION_MARKER

    notes = _text("docs/release-notes/unreleased.md")
    marker = _MIDDLE_TRUNCATION_MARKER.strip()

    assert marker in notes, f"the unreleased notes do not record {marker!r}"
    assert "tier result budget" in marker


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """Every line outside a fenced block and outside a markdown table."""
    lines: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith("|"):
            continue
        lines.append((number, line))
    return lines


def test_the_overview_prose_stays_hand_wrapped() -> None:
    """The landing page is edited by hand and read as a diff.

    Every paragraph on it is wrapped at roughly 80 columns; a line that
    escapes the wrap is the signature of an in-place word swap, and it
    turns the next edit to that paragraph into a whole-paragraph diff
    nobody can review line by line.
    """
    overview = _text("docs/overview.md")
    long_lines = [(number, len(line)) for number, line in _prose_lines(overview) if len(line) > 100]

    assert long_lines == [], f"docs/overview.md has unwrapped prose lines: {long_lines}"


def test_the_wrap_scan_reads_the_paragraphs_and_skips_the_diagram() -> None:
    """The teeth of the scan above: it must not be an empty selection."""
    overview = _text("docs/overview.md")
    numbered = _prose_lines(overview)

    assert len(numbered) > 100
    assert not any("flowchart LR" in line for _, line in numbered)
    assert any("korvid" in line for _, line in numbered)
