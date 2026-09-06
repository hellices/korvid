"""Vendor names must not reappear as routing decisions.

Scope: this checks *executable* regions of korvid's routing surface -
assignments, comparisons and dict literals - not docstrings or comments.
Prose may name a vendor; code may not branch on one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Anchored to the repository rather than the working directory: the
#: guard must mean the same thing however pytest was invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent

VENDOR_TOKENS = frozenset(
    {
        "openai",
        "anthropic",
        "claude",
        "azure",
        "bedrock",
        "gemini",
        "vertex",
        "cohere",
        "mistral",
        "groq",
        "together",
        "ollama",
        "copilot",
        "vllm",
        "deepseek",
        "xai",
    }
)

#: The routing surface. Not the whole tree - see the task notes for why
#: evals/, k8s/, obs/ and __main__.py are out of scope.
SCANNED_ROOTS: tuple[str, ...] = (
    "src/korvid/providers",
    "src/korvid/agent",
    "src/korvid/ui",
)
SCANNED_FILES: tuple[str, ...] = ("src/korvid/core/config.py",)

#: Modules that legitimately contain a vendor token in executable code,
#: each for a reason unrelated to model routing. Every entry needs a
#: reason; an entry without one is how a guard dies.
ALLOWED: frozenset[str] = frozenset(
    {
        # ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default" - the
        # literal OAuth scope string Entra requires. Not a branch: it is the
        # identifier of an external protocol, like a URL.
        "src/korvid/providers/entra.py",
        # Cluster CSP *display* names ("azure" -> "Azure") for the system
        # prompt. This describes the Kubernetes cluster, not the model.
        "src/korvid/agent/prompt_harness.py",
        # ModelDescriptor's capability catalog: context windows and tool
        # support keyed by model, which `ModelRouter` reads to pick a tier.
        # It is not an adapter table - it routes nothing and constructs no
        # transport - and nothing in this plan supersedes it.
        "src/korvid/agent/model_catalog.py",
        # The two flows LiteLLM structurally cannot own (Task 17).
        "src/korvid/providers/flow_copilot.py",
        "src/korvid/providers/flow_ollama_thinking.py",
        # Reads LiteLLM's shipped tables; the vendor names are *data* these
        # modules iterate and rewrite, never a branch they take. The
        # github_copilot exclusion in litellm_catalog.py is exactly such a
        # rewrite, and it must be able to name the string it excludes.
        "src/korvid/providers/litellm_catalog.py",
        "src/korvid/providers/litellm_runtime.py",
        # RETIRED_PROVIDER_ALIASES, DEVICE_LOGIN_PREFIXES and the reserved
        # names composed from them: the names a third-party plugin may not
        # register and the retired spellings that must stay unroutable.
        # `special_flows.from_entry_points` reads them on every start, so
        # removing the names removes the protection.
        "src/korvid/providers/litellm_settings.py",
    }
)

#: `core/config.py` is *not* whole-file allowed. Only the legacy-migration
#: region may name a vendor, and the region is computed from the module's
#: AST rather than by line or by a "legacy" substring - a migration
#: function's body and a migration-only alias table name providers on
#: lines that do not themselves say "legacy". Nested definitions count,
#: because `ast.walk` reaches them.
#:
#: Every name here must exist in the module **at the commit this guard
#: lands in**, which is what `test_every_migration_region_name_still_exists`
#: enforces in both directions.
_MIGRATION_MODULE = "src/korvid/core/config.py"
_MIGRATION_REGION_NAMES: frozenset[str] = frozenset(
    {
        "_migrate_legacy_agent",
        "_migrate_azure_endpoint",
        "_legacy_model_reference",
        "_legacy_auth",
        "_legacy_options",
        "_legacy_ollama_options",
        # One legacy knob validated the way its own pre-profile parser was.
        # Its warnings quote the `agent.ollama.<key>` line the operator has
        # to fix, so the key has to survive in the text.
        "_legacy_ollama_value",
        "_legacy_ollama_number",
        "_LEGACY_OPENAI_COMPAT_NAMES",
        # Migration-only: names whose credential handling changed, warned on
        # load. Measured offender at the pre-plan tree.
        "_LEGACY_REVIEW_NAMES",
        # The agent-level keys the legacy shape owned, which the first
        # successful save strips. `ollama` is one of them.
        "LEGACY_AGENT_KEYS",
    }
)


def _migration_line_span(tree: ast.Module) -> set[int]:
    """Every line belonging to a named migration function or assignment."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = targets[0] if targets else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name in _MIGRATION_REGION_NAMES and node.end_lineno is not None:
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


def _repo(name: str) -> Path:
    return _REPO_ROOT / name


def _scanned_paths() -> list[str]:
    """Repo-relative posix names of every scanned module."""
    paths = {p for root in SCANNED_ROOTS for p in _repo(root).rglob("*.py")}
    paths.update(_repo(name) for name in SCANNED_FILES)
    return sorted(p.relative_to(_REPO_ROOT).as_posix() for p in paths)


def _executable_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """`(lineno, value)` for string constants in executable positions.

    Docstrings are excluded by identity, so a module that *documents* a
    vendor name in prose is not an offender. Comments never reach the
    AST at all, which is why this is an AST walk and not a grep.
    """
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_module_branches_on_a_vendor_name() -> None:
    offenders: list[str] = []
    for posix in _scanned_paths():
        if posix in ALLOWED:
            continue
        tree = ast.parse(_repo(posix).read_text(encoding="utf-8"))
        exempt = _migration_line_span(tree) if posix == _MIGRATION_MODULE else set()
        for lineno, value in _executable_strings(tree):
            if lineno in exempt:
                continue
            lowered = value.lower()
            if any(token in lowered for token in VENDOR_TOKENS):
                offenders.append(f"{posix}:{lineno}: {value!r}")
    assert offenders == []


def test_load_config_itself_names_no_vendor() -> None:
    """The pre-plan tree names two vendors inline in `load_config`.

    It inferred `device-login` from `provider == "github-copilot"`, and it
    read the legacy `agent.ollama` sub-mapping by key. Both moved into
    `_legacy_auth` and `_legacy_options`, where the migration exemption
    covers them. Exempting `load_config` instead would exempt the
    module's largest function - which is not a region, it is a hole.
    """
    tree = ast.parse(_repo(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "load_config"
    )
    offenders = [
        f"{lineno}: {value!r}"
        for lineno, value in _executable_strings(target)
        if any(token in value.lower() for token in VENDOR_TOKENS)
    ]
    assert offenders == []


def test_the_migration_exemption_is_a_region_not_the_whole_file() -> None:
    """A vendor name added anywhere in core/config.py outside the named
    migration functions must still fail. Whole-file allowance would make
    the largest module in the change a permanent blind spot."""
    tree = ast.parse(_repo(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    exempt = _migration_line_span(tree)
    total = {lineno for lineno, _ in _executable_strings(tree)}
    assert exempt, "the migration region resolved to nothing - names drifted"
    assert not total <= exempt, "the exemption swallowed the whole module"


def test_every_migration_region_name_still_exists() -> None:
    """If a migration helper is renamed, the exemption must move with it
    rather than silently covering nothing. This also catches the reverse
    mistake: naming a helper that *this* task deletes, which would fail
    the guard on the commit that introduces it."""
    tree = ast.parse(_repo(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert defined >= _MIGRATION_REGION_NAMES


def test_every_allowance_names_a_file_that_exists() -> None:
    """A stale allowance silently widens the guard."""
    missing = [name for name in ALLOWED if not _repo(name).exists()]
    assert missing == []


def test_the_allowance_is_a_strict_subset_of_the_scan() -> None:
    """The allow-list must not be able to swallow the scan: if a path
    were listed twice, or a directory prefix crept in, the guard would
    quietly stop checking whole subtrees."""
    scanned = set(_scanned_paths())
    assert scanned > ALLOWED
    assert not any(name.endswith("/") or "*" in name for name in ALLOWED)


#: What each allowed module is allowed to say. An allowance is not a
#: blank cheque: a module may name the vendors its stated reason covers
#: and no others, so a static provider frozenset appearing inside an
#: allowed file fails here instead of hiding behind the allowance.
ALLOWED_TOKENS: dict[str, frozenset[str]] = {
    "src/korvid/providers/entra.py": frozenset({"azure"}),
    "src/korvid/providers/flow_copilot.py": frozenset({"copilot"}),
    "src/korvid/providers/flow_ollama_thinking.py": frozenset({"ollama"}),
    # The catalog's only *written* vendor tokens are the Copilot prefixes
    # it rewrites, because resolving them through litellm starts a device
    # login. Everything else it handles is data it iterates, not a name it
    # spells. If a provider frozenset ever lands here, this fails.
    "src/korvid/providers/litellm_catalog.py": frozenset({"copilot"}),
}


@pytest.mark.parametrize("module", sorted(ALLOWED_TOKENS))
def test_an_allowed_module_only_names_the_vendors_its_reason_covers(
    module: str,
) -> None:
    """Give the guard a chance to see inside its own exceptions.

    `litellm_catalog.py` is allowed because it must name the
    `github_copilot` prefix it rewrites. That reason does not extend to a
    hand-written table of every other provider, which is exactly the
    thing this plan removes and exactly the thing an unscoped allowance
    would hide.
    """
    permitted = ALLOWED_TOKENS[module]
    tree = ast.parse(_repo(module).read_text(encoding="utf-8"))
    offenders = [
        f"{lineno}: {value!r}"
        for lineno, value in _executable_strings(tree)
        for token in VENDOR_TOKENS
        if token in value.lower() and token not in permitted
    ]
    assert offenders == []


def test_every_scoped_allowance_is_actually_allowed() -> None:
    """A path in ALLOWED_TOKENS that is not in ALLOWED is a scoping rule
    that never runs, and a file in neither is unscanned by accident."""
    assert set(ALLOWED_TOKENS) <= ALLOWED


def test_the_hand_written_adapter_table_is_gone() -> None:
    for name in ("BUILTIN_ADAPTERS", "create_provider("):
        assert not any(
            name in p.read_text(encoding="utf-8") for p in _repo("src/korvid").rglob("*.py")
        ), name


def test_the_deleted_modules_are_actually_deleted() -> None:
    for name in ("registry.py", "configurator.py", "openai_compat.py"):
        assert not _repo(f"src/korvid/providers/{name}").exists()
