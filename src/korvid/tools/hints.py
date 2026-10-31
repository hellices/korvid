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
