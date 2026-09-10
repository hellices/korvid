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
from xml.etree import ElementTree

import pytest

from korvid import __version__
from korvid.agent.model_profiles import MetadataRefresh
from korvid.agent.tiers import high, low
from korvid.tools.registry import TOOLS_BY_NAME
from korvid.ui.widgets.model_search_screen import _REFRESH_MESSAGES
from tests.config_keys import names_key

_REPO_ROOT = Path(__file__).parents[1]
_CURRENT_RELEASE_NOTE = f"docs/release-notes/v{__version__}.md"

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
        _CURRENT_RELEASE_NOTE,
        "docs/release-notes/v0.4.0.md",
        "docs/dev/ui-controllers.md",
    } <= scanned
    assert not any(path.startswith(_HISTORICAL_DOC_PREFIXES) for path in scanned)


def test_public_guides_distinguish_validated_evidence_from_navigable_citations() -> None:
    """Compound diagnoses mint evidence even though no screen can show it all."""
    overview = " ".join(_text("docs/overview.md").lower().split())
    agent = " ".join(_text("docs/agent.md").lower().split())

    assert "navigable citation opens its actual view" in overview
    assert "navigable citations open their source view" in agent
    assert "compound diagnostics remain validated evidence" in agent


def test_agent_install_guide_matches_base_install_availability() -> None:
    """Without the optional extra, neither Ctrl-A nor :ai is registered."""
    agent = " ".join(_text("docs/agent.md").split())

    assert "`Ctrl-A` is unavailable" in agent
    assert "`:ai` is not registered" in agent
    assert "`Ctrl-A` simply shows a setup hint" not in agent


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


@pytest.mark.parametrize("page", ["docs/ops.md", _CURRENT_RELEASE_NOTE])
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

    The low tier replaces the registry's wording, by exact tool
    name, on the low route only. The high tier and the MCP server still
    describe every tool with the registry's own text, so "every surface
    describes a tool identically" was never true once the low map shipped.
    """
    normalized = " ".join(path.read_text(encoding="utf-8").split())
    assert not _IDENTICAL_TOOL_WORDING_OVERCLAIM.search(normalized), _relative(path)


@pytest.mark.parametrize("page", [_CURRENT_RELEASE_NOTE])
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
    today's settings is release history: the current release note owns it,
    the startup error itself names the replacement, and the guide describes
    what an operator configures today.
    """
    config = (_REPO_ROOT / "src" / "korvid" / "core" / "config.py").read_text(encoding="utf-8")
    removed_keys = re.findall(r"\"(agent\.\w+) was removed", config)
    assert removed_keys, "the startup migration error must still name the retired keys"

    agent = _text("docs/agent.md")
    assert "Upgrading from the profile-based agent" not in agent
    # Matched as whole keys: today's supported `agent.profiles` merely
    # *contains* the retired singular spelling, so a substring test would
    # read the replacement as the thing it replaced.
    assert [key for key in removed_keys if names_key(agent, key)] == []
    assert "model_tier" in agent, "the supported key still has to be on the page"
    assert re.search(
        rf"\[[^\]]*(?:migration|upgrade)[^\]]*\]\(release-notes/{re.escape(Path(_CURRENT_RELEASE_NOTE).name)}\)",
        agent,
        re.IGNORECASE,
    ), "the current guide must send upgrades to the release note that owns migration history"

    notes = _text(_CURRENT_RELEASE_NOTE)
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


def test_the_ollama_row_names_the_six_keys_and_where_they_live() -> None:
    """The tuning knobs are read out of a profile's `options`, not the bare
    names.

    `_options_from` reads exactly six keys off `profile.options`. They used
    to be dedicated `agent_ollama_<key>` config fields under a retired
    per-vendor namespace; Task 18 deleted those, so the row has to name
    `options` instead — `num_ctx: 32768` at the top level of a profile is
    silently ignored. Both the six keys and the `options` mapping they nest
    under have to be on the page.
    """
    flow = _text("src/korvid/providers/flow_ollama_thinking.py")
    start = flow.index("def _options_from(")
    end = flow.index("def _credentials_for(", start)
    block = flow[start:end]
    keys = re.findall(r"profile_options\.get\(\"(\w+)\"\)", block)
    assert keys == ["num_ctx", "temperature", "seed", "think", "keep_alive", "num_predict"], (
        "the six ollama keys the flow actually reads must drive this test, not a hand-written list"
    )

    agent = _text("docs/agent.md")
    row = next(line for line in agent.splitlines() if line.strip().startswith("| Ollama"))
    for key in keys:
        assert key in row, f"the Ollama row must still name {key}"
    assert "options" in row, "the Ollama row must say the six keys nest under a profile's `options`"


# ---------------------------------------------------------------------------
# 2. The provider-plugin API version a third party writes against
# ---------------------------------------------------------------------------

_API_V1_SPELLINGS = re.compile(r"\bAPI[-\s]v1\b", re.IGNORECASE)


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_current_page_sends_a_plugin_author_to_api_1(path: Path) -> None:
    """Only the API 1 → API 2 migration tables may name the retired version.

    A reader must not be directed to the removed construction contract.
    """
    text = path.read_text(encoding="utf-8")
    offenders = [
        line for line in text.splitlines() if _API_V1_SPELLINGS.search(line) and "API 2" not in line
    ]
    assert offenders == [], f"{_relative(path)} points at the retired plugin API: {offenders}"


@pytest.mark.parametrize("page", ["docs/agent.md", "README.md"])
def test_the_plugin_pointers_name_the_current_extension_points(page: str) -> None:
    text = _text(page)
    assert "SpecialFlow" in text
    assert "korvid.credential" in text


def test_provider_docs_distinguish_adapter_limits_from_engine_enforcement() -> None:
    text = " ".join(_text("docs/provider-plugins.md").split())

    assert "non-bool" in text
    assert "1,000,000,000" in text
    assert f"{low.BEHAVIOR.max_history_chars:,}" in text
    assert f"{high.BEHAVIOR.max_history_chars:,}" in text
    assert "not a per-field UTF-8 byte limit" in text
    assert "Custom adapters may emit" in text
    assert "response headers" in text
    assert "does not independently verify network I/O" in text


def test_agent_api_removals_are_marked_as_breaking_in_release_notes() -> None:
    text = " ".join(_text("docs/release-notes/unreleased.md").split())

    assert "**Breaking:**" in text
    assert "SpecialFlow provider contract" in text


def test_architecture_svg_descriptions_name_the_components_they_explain() -> None:
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    overview = ElementTree.fromstring(_text("docs/assets/agent-architecture-overview.svg"))
    description = overview.findtext("svg:desc", namespaces=namespace)
    assert description is not None
    for name in ("PromptHarness", "ComposedPrompt", "ModelRouter", "Execution ports"):
        assert name in description
    policy = ElementTree.fromstring(_text("docs/assets/agent-architecture-policy.svg"))
    assert "SpecialFlow" in policy.findtext("svg:desc", default="", namespaces=namespace)
    assert any(
        node.text == "SpecialFlow" for node in policy.findall("svg:text", namespaces=namespace)
    )


def test_current_tier_svg_budgets_match_the_live_behavior_modules() -> None:
    svg = ElementTree.fromstring(_text("docs/assets/agent-architecture-tiers.svg"))
    text = " ".join(svg.itertext())

    for behavior in (low.BEHAVIOR, high.BEHAVIOR):
        assert (
            f"{behavior.max_iterations} model rounds / "
            f"{behavior.max_history_chars:,} history characters"
        ) in text
        assert f"{behavior.max_result_chars:,} result characters" in text


def test_historical_architecture_commit_links_are_fully_pinned() -> None:
    text = _text("docs/dev/specs/2026-09-09-agent-architecture.md")
    commits = re.findall(r"https://github\.com/hellices/korvid/commit/([a-f0-9]+)", text)

    assert commits
    assert all(len(commit) == 40 for commit in commits)


def test_the_readme_explains_the_current_agent_and_mcp_starting_points() -> None:
    readme = " ".join(_text("README.md").split())
    assert "named profiles" in readme.lower()
    assert "model catalog" in readme.lower()
    assert "`:ai`" in readme
    assert "`:model" in readme
    assert "korvid --mcp" in readme
    assert '"command": "korvid"' in readme
    assert '"args": ["mcp", "stdio"]' in readme
    assert "running TUI" in readme
    assert "same user" in readme
    assert "OAuth" in readme
    assert "headless" in readme


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
    assert "agent/tiers/low.py: TOOL_DESCRIPTIONS" in methodology
    assert "250" in methodology
    assert "exact tool name" in methodology
    # The high tier keeps the registry wording — the two arms are not the same.
    assert "high tier" in methodology
    # And a change is only landable with the retained cases re-run.
    assert "liveness-probe-failing" in methodology
    assert "oom-killed" in methodology


def test_the_agent_page_sends_low_tier_wording_questions_to_the_methodology() -> None:
    """The low tier's shipped wording is an eval contract, not product copy.

    The tier's tool wording, its 250-character bound and its exact-tool-name
    application decide whether two campaigns are comparable — a question the
    eval methodology owns and
    `test_the_eval_methodology_states_the_low_pack_constraints` pins. The
    Agent guide tells an operator which tier is routed and what changes with
    it, then links to that page rather than shipping a second, driftable copy
    of the constraints.
    """
    agent = _text("docs/agent.md")

    assert "evals/methodology.md" in agent
    assert "TOOL_DESCRIPTIONS" not in agent
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
    start = methodology.index("agent/tiers/low.py: TOOL_DESCRIPTIONS")
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
#: rewritten; include the current release alongside the development notes.
_CURRENT_PAGES = [
    path
    for path in _MARKDOWN_FILES
    if not _relative(path).startswith("docs/release-notes/")
    or _relative(path) in {"docs/release-notes/unreleased.md", _CURRENT_RELEASE_NOTE}
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
    assert _CURRENT_RELEASE_NOTE in scanned
    assert not any(
        page.startswith("docs/release-notes/")
        and page not in {"docs/release-notes/unreleased.md", _CURRENT_RELEASE_NOTE}
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

    notes = _text(_CURRENT_RELEASE_NOTE)
    marker = _MIDDLE_TRUNCATION_MARKER.strip()

    assert marker in notes, f"the release notes do not record {marker!r}"
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


def _collapsed(text: str) -> str:
    """Markdown wraps sentences across lines; a reader does not see the wrap."""
    return " ".join(text.split())


def test_the_airgap_guide_quotes_the_answer_the_screen_really_gives() -> None:
    """A doc that quotes a message an operator will see must quote it exactly.

    The air-gap guide is read by someone who cannot check korvid against the
    internet, and it tells them what `Ctrl-R` answers once models.dev is
    disabled. A paraphrase drifting from `_REFRESH_MESSAGES` leaves them
    matching a sentence korvid never prints against a screen that says
    something else, with no way to tell which of the two is wrong.
    """
    guide = _collapsed(_text("docs/airgap.md"))
    disabled = _REFRESH_MESSAGES[MetadataRefresh.DISABLED]

    assert _collapsed(disabled) in guide


def test_the_agent_guide_quotes_every_refresh_answer_it_shows() -> None:
    """`docs/agent.md` quotes three of the four refresh outcomes verbatim.

    They are the sentences a reader matches against their own screen after
    pressing `Ctrl-R`. `_REFRESH_MESSAGES` is the source, so a reword there
    must fail here rather than leave the guide quoting a sentence korvid no
    longer prints. The fourth (`DISABLED`) belongs to the air-gap guide and
    is pinned by the test above.
    """
    guide = _collapsed(_text("docs/agent.md"))

    for outcome in (
        MetadataRefresh.UPDATED,
        MetadataRefresh.UNCHANGED,
        MetadataRefresh.UNAVAILABLE,
    ):
        assert _collapsed(_REFRESH_MESSAGES[outcome]) in guide, outcome


def test_the_threat_model_lists_every_litellm_lockdown_flag() -> None:
    """A lockdown table that omits a flag understates what korvid closes.

    The table is the whole security claim of that section: each row is a
    channel that would otherwise carry prompts, tool arguments or usage
    records off the machine. Read out of `LOCKDOWN_FLAGS` so adding a ninth
    flag without documenting it fails.
    """
    from korvid.providers.litellm_runtime import LOCKDOWN_FLAGS

    threat_model = _text("docs/threat-model.md")
    rows = {
        line.split("|")[1].strip().strip("`")
        for line in threat_model.splitlines()
        if line.startswith("| `") and line.count("|") == 3
    }

    assert {name for name, _ in LOCKDOWN_FLAGS} <= rows

    # The prose counts them in words, so the count has to be spelled the way
    # the page spells it — a ninth flag then fails here as well as in the row
    # comparison above.
    words = {6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    spelled = words.get(len(LOCKDOWN_FLAGS), str(len(LOCKDOWN_FLAGS)))
    assert f"{spelled} attributes" in threat_model


def test_the_migration_docs_name_the_profile_the_migration_really_creates() -> None:
    """The legacy profile name is read out of `core/config.py`, not guessed.

    An operator whose `config.yaml` still has the flat scalars looks for the
    profile korvid wrote. A doc naming a different one sends them to a key
    that is not in the file.
    """
    from korvid.core.config import LEGACY_PROFILE_NAME

    assert f"`{LEGACY_PROFILE_NAME}`" in _text("docs/agent.md")
    # The release note shows the file korvid writes back, so the name has to
    # appear as the key it really writes, not only in prose around it.
    notes = _text(_CURRENT_RELEASE_NOTE)
    assert f"active: {LEGACY_PROFILE_NAME}" in notes
    assert f"\n    {LEGACY_PROFILE_NAME}:\n" in notes


def test_the_release_notes_describe_the_current_plugin_entry_point() -> None:
    notes_path = _REPO_ROOT / _CURRENT_RELEASE_NOTE
    assert notes_path.is_file(), "the plugin migration must ship in versioned release notes"
    notes = notes_path.read_text(encoding="utf-8")
    assert "`SpecialFlow`" in notes
    assert "`korvid.provider`" in notes
    assert "`korvid.credential`" in notes
    assert "descriptor.provider` must equal" not in notes
    assert "no longer loads" in notes


# ---------------------------------------------------------------------------
# 7. models.dev: no auto-revalidation claim; production caller always forced
# ---------------------------------------------------------------------------

_AUTO_REVALID_PATTERNS = re.compile(
    r"revalidat\w*\s+on\s+(?:its|korvid'?s?\s+own|its\s+own)",
    re.IGNORECASE,
)


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=_relative)
def test_no_doc_claims_models_dev_auto_revalidates(path: Path) -> None:
    """The CACHE_TTL_SECONDS guard exists for hypothetical future callers.

    Production has exactly one refresh call-site — the setup UI's Ctrl-R
    action — and it always passes `force=True`, bypassing the TTL. Claiming
    korvid "revalidates on its own" describes a behaviour nothing performs
    today. Normalizing whitespace catches the claim even if it wraps across
    a line break.
    """
    normalized = " ".join(path.read_text(encoding="utf-8").split())
    assert not _AUTO_REVALID_PATTERNS.search(normalized), (
        f"{_relative(path)} claims models.dev auto-revalidates"
    )


def test_models_dev_production_caller_always_uses_force() -> None:
    """The UI always passes force=True, making CACHE_TTL_SECONDS inactive in production.

    `ModelsDevSource.refresh(force=False)` would return CACHED inside the TTL
    window, but no production call site passes False.  This test reads the
    source of every call to the catalog's `refresh_metadata` in
    model_search_screen.py rather than hard-coding the boolean, so the
    relationship drifts visibly.
    """
    source = _text("src/korvid/ui/widgets/model_search_screen.py")
    # Find actual awaited catalog calls (not the method definition itself).
    calls = re.findall(r"await\s+self\._catalog\.refresh_metadata\([^)]*\)", source)
    assert calls, "model_search_screen.py must still await self._catalog.refresh_metadata"
    for call in calls:
        assert "force=True" in call, (
            f"model_search_screen.py calls refresh_metadata without force=True: {call!r}"
        )


# ---------------------------------------------------------------------------
# 8. Provider-plugin reserved-prefix semantics match the live constants
# ---------------------------------------------------------------------------


def test_provider_plugin_guide_names_all_reserved_prefix_sets() -> None:
    """provider-plugins.md must describe all three reserved sets from live constants.

    The three sets are distinct in `litellm_settings.py`:
    - `RETIRED_PROVIDER_ALIASES`: never routable aliases
    - `DEVICE_LOGIN_PREFIXES`: device-login traps
    - `_SELF_SERVED_PROVIDER_NAMES`: korvid's own routes (routable but
      unregistrable by third parties)

    The LiteLLM dynamic catalog (`models_by_provider()`) is the fourth fence.
    Reading all names from the live constants means adding a new alias forces
    a doc update rather than silently leaving the guide stale.
    """
    from korvid.providers.litellm_settings import (
        _SELF_SERVED_PROVIDER_NAMES,
        DEVICE_LOGIN_PREFIXES,
        RETIRED_PROVIDER_ALIASES,
    )

    guide = _text("docs/provider-plugins.md")

    for name in RETIRED_PROVIDER_ALIASES:
        assert name in guide, f"retired alias {name!r} missing from provider-plugins.md"
    for name in DEVICE_LOGIN_PREFIXES:
        assert name in guide, f"device-login prefix {name!r} missing from provider-plugins.md"
    for name in _SELF_SERVED_PROVIDER_NAMES:
        assert name in guide, f"self-served prefix {name!r} missing from provider-plugins.md"


def test_provider_plugin_guide_names_litellm_dynamic_catalog() -> None:
    """provider-plugins.md must explain that LiteLLM's dynamic prefix table is also reserved.

    `SpecialFlowRegistry.from_entry_points()` receives `models_by_provider()`
    as `reserved_prefixes`, so a third party cannot shadow any prefix the SDK
    ships natively. The guide must name `models_by_provider` — reading the
    live function name means a rename fails this test before it silently
    leaves the guide lying.
    """
    from korvid.providers.litellm_runtime import models_by_provider as _fn  # noqa: F401

    guide = _text("docs/provider-plugins.md")
    assert "models_by_provider" in guide, (
        "provider-plugins.md must mention models_by_provider (LiteLLM's dynamic catalog)"
    )


def test_provider_plugin_guide_does_not_say_two_lists() -> None:
    """The guide must describe three sets, not two.

    Claiming 'two lists' omits the LiteLLM dynamic catalog, which is a
    third enforced fence.  Whitespace is collapsed so wrapping does not hide
    a stale count.
    """
    guide = " ".join(_text("docs/provider-plugins.md").split())
    assert "Two lists are enforced" not in guide, (
        "provider-plugins.md still says 'Two lists' — update to three sets"
    )


# ---------------------------------------------------------------------------
# 9. Keyring auth documents the profile.model fallback
# ---------------------------------------------------------------------------


def test_keyring_auth_documents_profile_model_fallback() -> None:
    """agent.md must say keyring falls back to profile.model when auth.key is absent.

    `_from_keyring` uses `_named_setting(profile.auth.settings) or profile.model`
    as the entry name.  An operator who stores a key under the model reference
    instead of a named `auth.key` must be able to discover this from the docs
    rather than from source code.
    """
    from korvid.providers.litellm_factory import _from_keyring  # noqa: F401

    agent = _text("docs/agent.md")
    # The keyring row must name the fallback.
    keyring_row = next(
        (line for line in agent.splitlines() if "keyring" in line and "|" in line), None
    )
    assert keyring_row is not None, "agent.md must have a keyring row in the auth table"
    assert "profile.model" in keyring_row, (
        "agent.md keyring row must document the profile.model fallback"
    )
