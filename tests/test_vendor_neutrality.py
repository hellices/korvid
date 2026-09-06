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

#: The routing surface. Not the whole tree - `evals/` runs local benchmark
#: harnesses whose CLI defaults legitimately name a vendor, `k8s/` names
#: cloud vendors to detect the *cluster's* CSP, and `mcp/`, `obs/`,
#: `tools/` and the top-level modules are wiring. Scanning those would
#: produce an allow-list long enough to say nothing.
#:
#: `test_the_scan_covers_exactly_the_declared_surface` pins both halves:
#: every root here must exist and contribute files, and every package
#: *not* here must be one of the deliberate exclusions below.
SCANNED_ROOTS: tuple[str, ...] = (
    "src/korvid/providers",
    "src/korvid/agent",
    "src/korvid/ui",
)
SCANNED_FILES: tuple[str, ...] = ("src/korvid/core/config.py",)

#: Packages under `src/korvid` deliberately outside the routing surface,
#: each with the reason it is out. A package that appears in neither this
#: tuple nor `SCANNED_ROOTS` fails the scan-surface test, so a new
#: routing package cannot arrive unscanned by accident.
UNSCANNED_PACKAGES: tuple[str, ...] = (
    # Only `core/config.py` is on the surface, and it is region-scoped.
    "src/korvid/core",
    # Local benchmark harnesses: their CLI defaults and base URLs name
    # `ollama` and `openai` because that is what they benchmark.
    "src/korvid/evals",
    # Cloud vendor names describing the *cluster's* CSP, never a model.
    "src/korvid/k8s",
    # Wiring and adapters that construct no provider.
    "src/korvid/mcp",
    "src/korvid/obs",
    "src/korvid/tools",
)

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
        # Reads LiteLLM's shipped tables; the vendor names are *data* this
        # module iterates and rewrites, never a branch it takes. The
        # github_copilot exclusion in litellm_catalog.py is exactly such a
        # rewrite, and it must be able to name the string it excludes.
        # `litellm_runtime.py` is deliberately absent: it is the single
        # import boundary for the SDK and spells no vendor at all.
        "src/korvid/providers/litellm_catalog.py",
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


def _named_line_span(tree: ast.Module, name: str) -> set[int]:
    """Every line belonging to the function or assignment called *name*."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        defined: str | None = None
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defined = node.name
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            defined = targets[0] if targets else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined = node.target.id
        if defined == name and node.end_lineno is not None:
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


def _migration_line_span(tree: ast.Module) -> set[int]:
    """Every line belonging to a named migration function or assignment."""
    lines: set[int] = set()
    for name in _MIGRATION_REGION_NAMES:
        lines |= _named_line_span(tree, name)
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


def test_every_migration_region_name_exempts_a_vendor_name() -> None:
    """The exemption runs in both directions: named, and load-bearing.

    A region that exempts no vendor token is not covering anything. It
    reads like a deliberate carve-out, so nobody removes it, and the day
    a function of that name is rewritten into something that is not a
    migration it silently becomes a hole in the middle of the largest
    module on the surface. The list has to shrink when the migration
    does.
    """
    tree = ast.parse(_repo(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    strings = _executable_strings(tree)
    inert: list[str] = []
    for name in sorted(_MIGRATION_REGION_NAMES):
        span = _named_line_span(tree, name)
        if not span:
            inert.append(f"{name}: covers no line")
        elif not any(
            lineno in span and any(token in value.lower() for token in VENDOR_TOKENS)
            for lineno, value in strings
        ):
            inert.append(f"{name}: exempts no vendor name")
    assert inert == []


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
#:
#: Every entry in `ALLOWED` must appear here — an unscoped allowance is
#: an allowance for all sixteen tokens, which is the blind spot this
#: table exists to close.
ALLOWED_TOKENS: dict[str, frozenset[str]] = {
    "src/korvid/providers/entra.py": frozenset({"azure"}),
    "src/korvid/providers/flow_copilot.py": frozenset({"copilot"}),
    "src/korvid/providers/flow_ollama_thinking.py": frozenset({"ollama"}),
    # The catalog's only *written* vendor tokens are the Copilot prefixes
    # it rewrites, because resolving them through litellm starts a device
    # login. Everything else it handles is data it iterates, not a name it
    # spells. If a provider frozenset ever lands here, this fails.
    "src/korvid/providers/litellm_catalog.py": frozenset({"copilot"}),
    # The reserved-name sets: the retired adapter spellings, the
    # device-login prefix, and the four names korvid routes or flows
    # itself. Nothing else - a fifth self-served name would be a provider
    # table growing back one row at a time.
    "src/korvid/providers/litellm_settings.py": frozenset(
        {"openai", "vllm", "claude", "copilot", "azure", "anthropic", "ollama"}
    ),
    # Cluster CSP display names for the system prompt. `azure` is the only
    # one that collides with a model vendor token; `aws` and `gcp` do not
    # appear in VENDOR_TOKENS at all.
    "src/korvid/agent/prompt_harness.py": frozenset({"azure"}),
    # `ModelDescriptor`'s fallback catalog keys capability facts by model.
    # It routes nothing and constructs no transport, and the one token it
    # spells is the local runtime whose tags carry no vendor prefix.
    "src/korvid/agent/model_catalog.py": frozenset({"ollama"}),
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
    """Every allowance is scoped, and every scoping rule runs.

    A path in `ALLOWED_TOKENS` that is not in `ALLOWED` is a rule that
    never runs; a path in `ALLOWED` that is not in `ALLOWED_TOKENS` is an
    allowance for all sixteen tokens, which is how the module most likely
    to grow a provider table becomes the one module the guard cannot see
    into. The two sets have to be equal.
    """
    assert set(ALLOWED_TOKENS) == set(ALLOWED)


def test_no_allowance_permits_a_token_the_module_does_not_use() -> None:
    """An allowance must be load-bearing in both directions.

    `test_an_allowed_module_only_names_the_vendors_its_reason_covers`
    catches a module that says *more* than its reason covers. This is the
    other half: a permitted token the file no longer spells is a widening
    left behind by a change that removed the code it was written for, and
    it silently re-admits that vendor later. `litellm_runtime.py` and
    `plugin_registry.py` were both in that state before this test existed.
    """
    stale: list[str] = []
    for module, permitted in sorted(ALLOWED_TOKENS.items()):
        tree = ast.parse(_repo(module).read_text(encoding="utf-8"))
        spelled = {
            token
            for _lineno, value in _executable_strings(tree)
            for token in VENDOR_TOKENS
            if token in value.lower()
        }
        stale.extend(f"{module}: {token!r}" for token in sorted(permitted - spelled))
    assert stale == []


def test_the_scan_covers_exactly_the_declared_surface() -> None:
    """The scan surface is pinned, not merely described.

    Three ways a scan quietly stops scanning, all of them silent because
    `Path.rglob` on a name that does not exist returns nothing:

    - a root is renamed or misspelled and matches no file;
    - a root nests inside another, so a subtree looks covered twice and a
      later edit to one of them means less than it reads;
    - a new package joins the routing surface and nobody adds it here.

    The third is the one that matters, so every package under
    `src/korvid` must be named either as scanned or as deliberately out.
    """
    for root in SCANNED_ROOTS:
        directory = _repo(root)
        assert directory.is_dir(), f"{root} is not a directory"
        assert any(directory.rglob("*.py")), f"{root} contributes no module"
    for name in SCANNED_FILES:
        assert _repo(name).is_file(), f"{name} is not a file"

    assert not any(
        other != root and other.startswith(f"{root}/")
        for root in SCANNED_ROOTS
        for other in SCANNED_ROOTS
    ), "a scanned root nests inside another"

    packages = {
        f"src/korvid/{path.name}"
        for path in _repo("src/korvid").iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert packages == set(SCANNED_ROOTS) | set(UNSCANNED_PACKAGES)


def test_the_hand_written_adapter_table_is_gone() -> None:
    for name in ("BUILTIN_ADAPTERS", "create_provider("):
        assert not any(
            name in p.read_text(encoding="utf-8") for p in _repo("src/korvid").rglob("*.py")
        ), name


def test_the_deleted_modules_are_actually_deleted() -> None:
    for name in ("registry.py", "configurator.py", "openai_compat.py"):
        assert not _repo(f"src/korvid/providers/{name}").exists()
