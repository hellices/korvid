from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
LEDGER = ROOT / "docs/dev/capabilities.md"
DEVELOPMENT_INDEX = ROOT / "docs/dev/README.md"
HISTORICAL_DESIGN = ROOT / "docs/dev/specs/2026-07-23-korvid-tui-design.md"

STATUSES = frozenset({"implemented", "partial", "validated", "deferred", "rejected"})
_STATUS_ROW = re.compile(r"^\|\s*`(?P<status>[a-z]+)`\s*\|", re.MULTILINE)
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

    statuses = frozenset(match["status"] for match in _STATUS_ROW.finditer(ledger))
    capability_statuses = frozenset(match["status"] for match in _CAPABILITY_ROW.finditer(ledger))
    assert statuses == STATUSES
    assert capability_statuses <= STATUSES


def test_ledger_records_the_current_shipped_and_bounded_surfaces() -> None:
    capabilities = _capabilities()

    assert capabilities["Keyboard-first TUI"][0] in {"implemented", "validated"}
    assert capabilities["Pulse"][0] == "partial"
    assert capabilities["Action Palette"][0] in {"implemented", "validated"}
    assert capabilities["Approval and audit safety"][0] in {"implemented", "validated"}
    assert capabilities["Embedded Agent"][0] in {"implemented", "validated"}
    assert capabilities["Local MCP adapter"][0] in {"implemented", "validated"}
    assert capabilities["Observability connectors"][0] in {"implemented", "partial"}
    assert capabilities["Evaluation harness"][0] in {"implemented", "partial"}

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
    assert "`korvid.panel`" in scope
    assert "`korvid.tool`" in scope
    assert "only public external" in scope.lower()
    assert "not public contracts" in scope.lower()


def test_development_docs_point_to_current_truth_from_historical_design() -> None:
    development_index = DEVELOPMENT_INDEX.read_text(encoding="utf-8")
    historical_banner = "\n".join(HISTORICAL_DESIGN.read_text(encoding="utf-8").splitlines()[:15])

    assert "[`capabilities.md`](capabilities.md)" in development_index
    assert "historical" in historical_banner.lower()
    assert "2026-08-12-korvid-architecture.md" in historical_banner
    assert "../capabilities.md" in historical_banner
