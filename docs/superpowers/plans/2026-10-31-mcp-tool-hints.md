# MCP Tool Hints (#518) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a title and MCP tool annotations for every tool in `tools/list`, derived from each tool's declared `effect` (#518).

**Architecture:** A new pure module, `tools/hints.py`, maps each `Effect` to `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, refines the three proposal tools, and holds each tool's title. It validates every registered tool at import. `KorvidMCPServer.list_tools` turns those hints into `types.Tool(title=..., annotations=types.ToolAnnotations(...))`; the stdio adapter relays them unchanged.

**Tech Stack:** Python 3.11+, the MCP Python SDK 2.x (`mcp.types`), pytest, mypy `--strict`, ruff, tach.

## Global Constraints

- **Hints never gate anything.** korvid's approval does not read them: a proposal still waits for a keystroke in the TUI, and `--readonly` still removes writes.
- **Derived, never claimed:** a tool's hints come from its registered `effect`; no `cluster_write` or `write_proposal` tool is ever read-only.
- **Every effect and every tool is covered:** at import, `check_effects` rejects an `Effect` value without hints even if no tool uses it yet, and `validate_hints` rejects a registered tool without a title or a proposal tool without hints of its own.
- **Size limits:** `src/korvid/tools/registry.py` is at its limit (1420 lines), and `scripts/check_source_size.py` is not edited, so titles live in `tools/hints.py`, not on `ToolDef`.
- **Docs tests:** `tests/test_docs_readability.py` requires `list_resources`, `get_resource`, `get_logs` and `diagnose_workload` to each name exactly one table row in `docs/mcp.md`, so the hints table describes read families without naming them.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the issue

- **Proposals are refined per tool.** The three `write_proposal` tools share an effect but not a behaviour. `propose_write` proposes every write, deletes included, so it is destructive and not idempotent. `get_write_proposal` and `cancel_write_proposal` are neither destructive nor read-only (the issue's rule), and are idempotent.
- **`idempotentHint` only where it means something.** The MCP specification reads it only for tools that are not read-only, so read-only tools leave it unset.
- **`cluster_write` is mapped too** (not read-only, destructive), although MCP never lists those tools, so every `Effect` value has hints.
- **The title is set twice:** on `Tool.title` and on `ToolAnnotations.title`, which earlier protocol revisions read.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/tools/hints.py` | create | `ToolHints`, `TOOL_TITLES`, `validate_hints`, `tool_hints` |
| `src/korvid/mcp/server.py` | modify | `_tool`, and `list_tools` through it |
| `tests/tools/test_hints.py` | create | coverage, the effect table, the no-read-only-write rule |
| `tests/mcp/test_server.py`, `tests/mcp/test_stdio.py` | modify | published and relayed annotations |
| `docs/mcp.md`, `docs/release-notes/unreleased.md` | modify | what the hints mean |

---

### Task 1: The hints

**Files:** Create `src/korvid/tools/hints.py` and `tests/tools/test_hints.py`.

- [ ] **Step 1: Write the failing tests:**

```python
"""Every tool's host-facing hints, derived from its declared effect (#518)."""

from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest

from korvid.tools.hints import TOOL_TITLES, ToolHints, check_effects, tool_hints, validate_hints
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


def test_an_effect_no_tool_uses_yet_still_needs_hints_at_import() -> None:
    with pytest.raises(ValueError, match="no hints for effect 'write_proposal'"):
        check_effects({"cluster_read", "external_read", "ui_only", "cluster_write"})
```

- [ ] **Step 2:** Run `uv run --frozen pytest -p no:tach tests/tools/test_hints.py -q`. Expected: collection fails, no module.
- [ ] **Step 3: Implement it:**

```python
"""What each tool does to its environment, as hints for an MCP host (#518).

A host can treat tools differently by these hints, for example by allowing
read-only tools without asking. They are derived from each tool's declared
`effect`, so a tool cannot claim less than the registry says it does. They
are hints for the host's own prompting only: korvid's approval never relies
on them. A write proposal still waits for a keystroke in the TUI, and
`--readonly` still removes writes.

| `effect` | read-only | destructive | open world |
| --- | --- | --- | --- |
| `cluster_read` | yes | no | no |
| `external_read` | yes | no | yes |
| `ui_only` | yes | no | no |
| `cluster_write` | no | yes | no |
| `write_proposal` | no | per tool | no |

`propose_write` can propose a delete, so it is destructive; reading or
cancelling a proposal is not, and either can be repeated. Idempotence is
only stated where a tool is not read-only, as the MCP specification reads it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import get_args

from korvid.tools.registry import TOOL_DEFS, Effect, ToolDef


@dataclass(frozen=True, slots=True)
class ToolHints:
    """A tool's title, and what calling it may do: MCP `ToolAnnotations`."""

    title: str
    read_only: bool
    destructive: bool
    idempotent: bool | None
    open_world: bool


#: Effect -> (read-only, destructive, idempotent, open world).
_BY_EFFECT: Mapping[Effect, tuple[bool, bool, bool | None, bool]] = MappingProxyType(
    {
        "cluster_read": (True, False, None, False),
        "external_read": (True, False, None, True),
        "ui_only": (True, False, None, False),
        "cluster_write": (False, True, False, False),
        # Refined per tool below: proposals differ in what they ask for.
        "write_proposal": (False, True, False, False),
    }
)


def check_effects(mapped: Collection[str]) -> None:
    """Reject `mapped` effects that miss an `Effect` value, used by a tool yet or not.

    Raises:
        ValueError: naming the first effect without hints.
    """
    for effect in get_args(Effect):
        if effect not in mapped:
            raise ValueError(f"no hints for effect {effect!r}")


check_effects(_BY_EFFECT.keys())
#: Proposal tool -> (destructive, idempotent).
_PROPOSALS: Mapping[str, tuple[bool, bool]] = MappingProxyType(
    {
        "propose_write": (True, False),
        "get_write_proposal": (False, True),
        "cancel_write_proposal": (False, True),
    }
)
#: Each tool's human-readable title.
TOOL_TITLES: Mapping[str, str] = MappingProxyType(
    {
        "list_resources": "List resources",
        "get_resource": "Get a resource",
        "get_logs": "Get logs",
        "get_events": "Get events",
        "list_operators": "List operators",
        "helm_list_releases": "List Helm releases",
        "diagnose_pod": "Diagnose a Pod",
        "diagnose_workload": "Diagnose a workload",
        "diagnose_service": "Diagnose a Service",
        "diagnose_pvc": "Diagnose a PersistentVolumeClaim",
        "query_metrics": "Query metrics",
        "search_logs": "Search logs",
        "navigate": "Open a view",
        "set_filter": "Filter the table",
        "open_logs": "Open logs",
        "open_describe": "Open a description",
        "drill_down": "Drill down",
        "delete_resource": "Delete a resource",
        "scale_resource": "Scale a resource",
        "rollout_restart": "Restart a rollout",
        "resize_pod": "Resize a Pod",
        "propose_write": "Propose a write",
        "get_write_proposal": "Get a write proposal",
        "cancel_write_proposal": "Cancel a write proposal",
    }
)


def _hints(d: ToolDef, titles: Mapping[str, str]) -> ToolHints:
    title = titles.get(d.name)
    if not title:
        raise ValueError(f"tool {d.name!r} has no title")
    if d.effect not in _BY_EFFECT:
        raise ValueError(f"effect {d.effect!r} has no hints")
    read_only, destructive, idempotent, open_world = _BY_EFFECT[d.effect]
    if d.effect == "write_proposal":
        if d.name not in _PROPOSALS:
            raise ValueError(f"tool {d.name!r} proposes writes but has no hints of its own")
        destructive, idempotent = _PROPOSALS[d.name]
    return ToolHints(title, read_only, destructive, idempotent, open_world)


def validate_hints(defs: Sequence[ToolDef], *, titles: Mapping[str, str] = TOOL_TITLES) -> None:
    """Reject a tool without a title, or one whose effect has no hints.

    Raises:
        ValueError: naming the tool or effect that has none.
    """
    for d in defs:
        _hints(d, titles)


_HINTS: Mapping[str, ToolHints] = MappingProxyType(
    {d.name: _hints(d, TOOL_TITLES) for d in TOOL_DEFS}
)


def tool_hints(name: str) -> ToolHints:
    """The hints of registered tool `name`.

    Raises:
        KeyError: if no tool has that name.
    """
    return _HINTS[name]
```

- [ ] **Step 4:** Run the tests again. Expected: all pass. Then commit.

### Task 2: Publish them

**Files:** Modify `src/korvid/mcp/server.py`, `tests/mcp/test_server.py`, `tests/mcp/test_stdio.py`.

- [ ] **Step 1: Write the failing test** in `tests/mcp/test_server.py`:

```python
async def test_list_tools_publishes_each_tools_title_and_hints() -> None:
    from korvid.tools.hints import tool_hints
    from korvid.tools.registry import mcp_tool_schemas

    schemas = mcp_tool_schemas(write_proposals=True, observability_backends=frozenset({"metrics"}))
    server = KorvidMCPServer(RecordingExecutor(), schemas, capability_token=_TEST_CAPABILITY)

    tools = await server.list_tools()

    assert {t.name for t in tools} >= {"list_resources", "query_metrics", "propose_write"}
    for tool in tools:
        hints = tool_hints(tool.name)
        assert tool.title == hints.title
        assert tool.annotations == types.ToolAnnotations(
            title=hints.title,
            read_only_hint=hints.read_only,
            destructive_hint=hints.destructive,
            idempotent_hint=hints.idempotent,
            open_world_hint=hints.open_world,
        )
    proposal = next(t for t in tools if t.name == "propose_write")
    assert proposal.annotations is not None
    assert (proposal.annotations.read_only_hint, proposal.annotations.destructive_hint) == (
        False,
        True,
    )
```

In `tests/mcp/test_stdio.py`'s `test_stdio_forwards_tools_results_and_proposals_without_credentials`, assert that the relayed `list_resources` is read-only and that `propose_write` is not read-only and is destructive.

- [ ] **Step 2: Implement it.** Import `tool_hints` from `korvid.tools.hints`, add before `KorvidMCPServer`:

```python
def _tool(function: dict[str, Any]) -> types.Tool:
    hints = tool_hints(function["name"])
    return types.Tool(
        name=function["name"],
        title=hints.title,
        description=function["description"],
        input_schema=function["parameters"],
        annotations=types.ToolAnnotations(
            title=hints.title,
            read_only_hint=hints.read_only,
            destructive_hint=hints.destructive,
            idempotent_hint=hints.idempotent,
            open_world_hint=hints.open_world,
        ),
    )
```

and make `list_tools`:

```python
    async def list_tools(self) -> list[types.Tool]:
        """MCP ``tools/list``: mirror the agent tool definitions 1:1.

        Each tool carries its title and the hints its declared effect gives
        (`korvid.tools.hints`), for the host's own prompting. korvid's
        approval never relies on them.
        """
        return [_tool(t["function"]) for t in self._tools]
```

- [ ] **Step 3:** Run `uv run --frozen pytest -p no:tach tests/mcp tests/tools/test_hints.py -q`. Expected: all pass. Then commit.

### Task 3: Document it, then run the full gate

- [ ] `docs/mcp.md`: a "Tool hints" section with the table and the note that approval never relies on the hints; a release-notes entry.
- [ ] Run `make check`. Expected: every step passes.
