from __future__ import annotations

import ast
import hashlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / "AGENTS.md"
LEDGER = ROOT / "docs/dev/capabilities.md"
DEVELOPMENT_INDEX = ROOT / "docs/dev/README.md"
HISTORICAL_DESIGN = ROOT / "docs/dev/specs/2026-07-23-korvid-tui-design.md"
PYPROJECT = ROOT / "pyproject.toml"
TOOL_REGISTRY = ROOT / "src/korvid/tools/registry.py"

STATUSES = frozenset({"implemented", "partial", "validated", "deferred", "rejected"})
STATUS_MEANINGS = {
    "implemented": "Shipped behavior with maintained user documentation.",
    "partial": "Shipped with a deliberately bounded surface.",
    "validated": (
        "Implemented and qualified by explicit cross-platform, integration, or evaluation evidence."
    ),
    "deferred": "Plausible future work that is not committed to a release.",
    "rejected": "A direction intentionally excluded from korvid's product model.",
}
HISTORICAL_TITLE = "# korvid — AI-Native Kubernetes TUI Design Document"
HISTORICAL_BANNER = (
    "> **Historical baseline.** This document preserves the original 2026-07-23\n"
    "> intent; it is not the current product inventory. Read the\n"
    "> [current architecture](2026-08-12-korvid-architecture.md) and\n"
    "> [living capability ledger](../capabilities.md) for the system as built and\n"
    "> current product decisions.\n\n"
)
HISTORICAL_BODY_SHA256 = "614889af28739946260b9baac2121e1127dad58472e0bf93d64c53aa29959f6c"

_STATUS_ROW = re.compile(
    r"^\|\s*`(?P<status>[a-z]+)`\s*\|\s*(?P<meaning>[^|]+?)\s*\|$",
    re.MULTILINE,
)
_CAPABILITY_ROW = re.compile(
    r"^\|\s*(?P<capability>[^|]+?)\s*\|\s*`(?P<status>[a-z]+)`\s*"
    r"\|\s*(?P<scope>[^|]+?)\s*\|$",
    re.MULTILINE,
)


def _ledger() -> str:
    assert LEDGER.is_file(), "the living capability ledger has not been created"
    return LEDGER.read_text(encoding="utf-8")


def _capabilities() -> dict[str, tuple[str, str]]:
    return {
        match["capability"].strip(): (match["status"], match["scope"].strip())
        for match in _CAPABILITY_ROW.finditer(_ledger())
        if match["status"] in STATUSES
    }


def test_ledger_defines_exactly_the_approved_status_vocabulary() -> None:
    ledger = _ledger()

    meanings = {match["status"]: match["meaning"].strip() for match in _STATUS_ROW.finditer(ledger)}
    capability_statuses = frozenset(match["status"] for match in _CAPABILITY_ROW.finditer(ledger))
    assert meanings == STATUS_MEANINGS
    assert capability_statuses == STATUSES


def test_ledger_records_the_current_shipped_and_bounded_surfaces() -> None:
    capabilities = _capabilities()

    assert capabilities["Keyboard-first TUI"][0] == "implemented"
    assert capabilities["Pulse"][0] == "partial"
    assert capabilities["Action Palette"][0] == "implemented"
    assert capabilities["Approval and audit safety"][0] == "validated"
    assert capabilities["Embedded Agent"][0] == "implemented"
    assert capabilities["Local MCP adapter"][0] == "implemented"
    assert capabilities["Observability connectors"][0] == "implemented"
    assert capabilities["Evaluation harness"][0] == "implemented"

    pulse_scope = capabilities["Pulse"][1].lower()
    assert "pod" in pulse_scope
    assert "deployment" in pulse_scope
    assert "recent warning" in pulse_scope

    assert "in-tui" in capabilities["Embedded Agent"][1].lower()
    assert "loopback" in capabilities["Local MCP adapter"][1].lower()
    assert "validation" in capabilities["Observability connectors"][1].lower()
    assert "validation" in capabilities["Evaluation harness"][1].lower()


def test_ledger_records_deferred_and_rejected_product_directions() -> None:
    capabilities = _capabilities()

    assert capabilities["Fleet / simultaneous multi-cluster"][0] == "deferred"
    assert capabilities["Incident persistence and replay"][0] == "deferred"
    assert capabilities["Autonomous writes / approval bypass"][0] == "rejected"
    assert "not committed" in capabilities["Fleet / simultaneous multi-cluster"][1].lower()
    assert "not committed" in capabilities["Incident persistence and replay"][1].lower()


def test_ledger_names_only_the_public_external_extension_contracts() -> None:
    status, scope = _capabilities()["External extension contracts"]

    assert status == "implemented"
    assert "`korvid.provider`" in scope
    assert "`korvid.credential`" in scope
    assert "only public external" in scope.lower()
    assert "`korvid.panel` is unimplemented and is not a public contract" in scope
    assert "`korvid.tool` is unimplemented and is not a public contract" in scope

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert set(project["entry-points"]) == {"korvid.provider", "korvid.credential"}


def test_contributor_registration_guidance_matches_the_extension_ledger() -> None:
    ledger_scope = _capabilities()["External extension contracts"][1]
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    public_groups = set(project["entry-points"])
    unavailable_groups = set(
        re.findall(r"`(korvid\.\w+)` is unimplemented and is not a public contract", ledger_scope)
    )
    agents = AGENTS.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^- Plugins/providers register .*?(?=^- |\n## |\Z)", agents)
    assert match is not None
    guidance = " ".join(match.group().split())
    public_guidance, separator, unavailable_guidance = guidance.partition(
        "Panel and tool extension groups"
    )

    assert public_groups == {"korvid.provider", "korvid.credential"}
    assert unavailable_groups == {"korvid.panel", "korvid.tool"}
    assert "only public external entry-point groups" in public_guidance
    assert set(re.findall(r"`(korvid\.\w+)`", public_guidance)) == public_groups
    assert separator
    for group in unavailable_groups:
        assert f"`{group}`" in unavailable_guidance
    assert "not implemented" in unavailable_guidance
    assert "not public contracts" in unavailable_guidance


def test_tool_registry_docstring_keeps_unimplemented_extension_loading_private() -> None:
    ledger_scope = _capabilities()["External extension contracts"][1]
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    registry_source = TOOL_REGISTRY.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(registry_source), clean=False)

    assert "`korvid.tool` is unimplemented and is not a public contract" in ledger_scope
    assert "korvid.tool" not in project["entry-points"]
    assert docstring is not None
    assert (
        "External tool loading and a public tool-extension contract are not implemented"
        in docstring
    )
    assert "`korvid.tool` is not a public entry-point group" in docstring
    assert "documented `korvid.tool`" not in docstring


def test_development_docs_link_the_capability_ledger() -> None:
    development_index = DEVELOPMENT_INDEX.read_text(encoding="utf-8")

    assert "[`capabilities.md`](capabilities.md)" in development_index


def test_historical_design_has_exact_current_truth_banner() -> None:
    historical = HISTORICAL_DESIGN.read_text(encoding="utf-8")
    prefix = f"{HISTORICAL_TITLE}\n\n"
    banner = historical[len(prefix) : len(prefix) + len(HISTORICAL_BANNER)]

    assert historical.startswith(prefix)
    assert banner == HISTORICAL_BANNER
    assert "[current architecture](2026-08-12-korvid-architecture.md)" in banner
    assert "[living capability ledger](../capabilities.md)" in banner


def test_historical_design_body_is_byte_for_byte_preserved() -> None:
    historical = HISTORICAL_DESIGN.read_text(encoding="utf-8")

    assert historical.count(HISTORICAL_BANNER) == 1
    historical_body = historical.replace(HISTORICAL_BANNER, "", 1)
    assert hashlib.sha256(historical_body.encode()).hexdigest() == HISTORICAL_BODY_SHA256
