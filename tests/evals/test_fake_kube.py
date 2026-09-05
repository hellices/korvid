"""RBAC denial enforcement in the scenario-seeded FakeKubeClient (issue #69).

A fixture that declares `forbidden` rules grades the model on *observing* a
denial (`journeys/rbac-evidence-gap.yaml`). If a read entry point serves the
withheld evidence anyway, the rule still loads, the journey still runs, and
the score describes a gap that was never there — so the denial table is
pinned both behaviourally and structurally.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.scenario import ContainerLogs, Scenario
from korvid.k8s.errors import ApiStatusError
from korvid.tools.executor import ToolExecutor
from tests.evals.fixtures import EVAL_INTERACTION


def _scenario(**overrides: Any) -> Scenario:
    fields: dict[str, Any] = {
        "id": "s1",
        "question": "q",
        "interaction": EVAL_INTERACTION,
        "root_cause": "oom_killed",
        "must_mention": (("oom",),),
        "objects": (
            {
                "kind": "Pod",
                "apiVersion": "v1",
                "metadata": {"name": "api-1", "namespace": "shop", "uid": "u1"},
                "spec": {"nodeName": "node-a", "containers": [{"name": "app"}]},
                "status": {"phase": "Running"},
            },
        ),
        "logs": {"shop/api-1/app": ContainerLogs(current=("line 1",), previous=("old crash",))},
    }
    fields.update(overrides)
    return Scenario(**fields)


async def test_a_forbidden_read_fails_with_403_like_a_denied_rbac_rule() -> None:
    """The fixture can withhold a read the way RBAC does, and a rule that
    withholds nothing measures nothing."""
    scenario = _scenario(forbidden=({"kind": "pods", "namespace": "shop", "subresource": "log"},))
    executor = ToolExecutor(FakeKubeClient(scenario), builtin_aliases())

    denied = await executor.execute("get_logs", {"pod": "api-1", "namespace": "shop"})
    assert denied.startswith("ERROR:")
    assert "forbidden" in denied.lower()

    allowed = await executor.execute(
        "get_resource", {"kind": "pods", "name": "api-1", "namespace": "shop"}
    )
    assert not allowed.startswith("ERROR:")
    assert "api-1" in allowed


def test_every_read_entry_point_consults_the_denial_table() -> None:
    """A new read that skips `_deny` is a hole with no symptom.

    The rule still loads, the journey still runs, and the model quietly gets
    the evidence the fixture meant to withhold - so the score describes a
    gap that was never there. Two such holes shipped in this file's first
    draft (events and Helm releases), which is why this is pinned rather
    than left to review.
    """
    reads = [
        name
        for name, member in inspect.getmembers(FakeKubeClient)
        if (name.startswith(("list_", "get_", "stream_")) and inspect.isfunction(member))
    ]
    assert reads, "no read entry points found - the naming convention changed"
    for name in reads:
        body = inspect.getsource(getattr(FakeKubeClient, name))
        assert "self._deny(" in body, (
            f"{name} reads the fixture without consulting the denial table; "
            "a forbidden rule would silently not apply to it"
        )


async def test_the_denial_table_refuses_at_the_client_not_only_at_the_tool() -> None:
    """`_deny` itself must raise, and an omitted subresource is a wildcard:
    an entry point that consults a table which never refuses passes the
    structural check above while still leaking evidence."""
    client = FakeKubeClient(_scenario(forbidden=({"kind": "pods", "namespace": "shop"},)))

    with pytest.raises(ApiStatusError, match=r"403|forbidden"):
        async for _ in client.stream_logs("shop", "api-1", "app"):
            pass
