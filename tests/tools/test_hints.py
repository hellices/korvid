"""Every tool's host-facing hints, derived from its declared effect (#518)."""

from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest

from korvid.tools.hints import TOOL_TITLES, ToolHints, tool_hints, validate_hints
from korvid.tools.registry import TOOL_DEFS, Effect, tool_def


def test_every_tool_has_hints_and_a_title_of_its_own() -> None:
    hints = [tool_hints(d.name) for d in TOOL_DEFS]

    assert all(h.title.strip() for h in hints)
    assert len({h.title for h in hints}) == len(hints)
    assert set(TOOL_TITLES) == {d.name for d in TOOL_DEFS}


def test_every_declared_effect_maps_to_hints() -> None:
    proposal = tool_def("propose_write")
    assert proposal is not None
    for effect in get_args(Effect):
        base = proposal if effect == "write_proposal" else TOOL_DEFS[0]

        validate_hints([replace(base, effect=effect)])


def test_nothing_that_writes_or_proposes_a_write_is_ever_read_only() -> None:
    writes = [d for d in TOOL_DEFS if d.effect in ("cluster_write", "write_proposal")]

    assert writes
    assert not any(tool_hints(d.name).read_only for d in writes)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("list_resources", ToolHints("List resources", True, False, None, False)),
        ("query_metrics", ToolHints("Query metrics", True, False, None, True)),
        ("navigate", ToolHints("Open a view", True, False, None, False)),
        ("delete_resource", ToolHints("Delete a resource", False, True, False, False)),
        ("propose_write", ToolHints("Propose a write", False, True, False, False)),
        ("get_write_proposal", ToolHints("Get a write proposal", False, False, True, False)),
        ("cancel_write_proposal", ToolHints("Cancel a write proposal", False, False, True, False)),
    ],
)
def test_each_effect_gives_the_hints_its_table_row_says(name: str, expected: ToolHints) -> None:
    assert tool_hints(name) == expected


def test_a_tool_without_a_title_is_rejected() -> None:
    nameless = replace(TOOL_DEFS[0], name="brand_new_tool")

    with pytest.raises(ValueError, match="'brand_new_tool' has no title"):
        validate_hints([nameless])


def test_a_new_proposal_tool_needs_its_own_hints() -> None:
    proposal = tool_def("propose_write")
    assert proposal is not None
    unknown = replace(proposal, name="amend_write_proposal")

    with pytest.raises(ValueError, match="'amend_write_proposal' proposes writes"):
        validate_hints([unknown], titles={"amend_write_proposal": "Amend"})


def test_an_unknown_effect_is_rejected() -> None:
    odd = replace(TOOL_DEFS[0], effect="cluster_teleport")  # type: ignore[arg-type]  # an effect the registry would also reject

    with pytest.raises(ValueError, match="'cluster_teleport' has no hints"):
        validate_hints([odd])
