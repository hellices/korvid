"""Drain impact planning (issue #40): classify the pods on a node before a
single eviction is issued - DaemonSet and mirror pods are skipped, emptyDir
pods are flagged, and PDB-violating evictions are called out up front."""

from typing import Any

from korvid.k8s.drain import DrainPlan, build_drain_plan


def _pod(
    name: str,
    namespace: str = "default",
    *,
    uid: str = "",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    owner_kind: str | None = None,
    volumes: list[dict[str, Any]] | None = None,
    phase: str = "Running",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if uid:
        metadata["uid"] = uid
    if labels:
        metadata["labels"] = labels
    if annotations:
        metadata["annotations"] = annotations
    if owner_kind:
        metadata["ownerReferences"] = [
            {"kind": owner_kind, "name": "owner", "controller": True},
        ]
    spec: dict[str, Any] = {}
    if volumes:
        spec["volumes"] = volumes
    return {"metadata": metadata, "spec": spec, "status": {"phase": phase}}


def _pdb(
    name: str,
    namespace: str = "default",
    *,
    match_labels: dict[str, str] | None = None,
    match_expressions: list[dict[str, Any]] | None = None,
    disruptions_allowed: int = 0,
) -> dict[str, Any]:
    selector: dict[str, Any] = {}
    if match_labels:
        selector["matchLabels"] = match_labels
    if match_expressions:
        selector["matchExpressions"] = match_expressions
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": selector},
        "status": {"disruptionsAllowed": disruptions_allowed},
    }


def test_plain_pod_is_an_eviction_target() -> None:
    plan = build_drain_plan([_pod("web-1", uid="u1")], [])
    assert len(plan.targets) == 1
    target = plan.targets[0]
    assert (target.namespace, target.name, target.uid) == ("default", "web-1", "u1")
    assert not target.local_storage
    assert target.pdb_blocked is None


def test_daemonset_pods_are_skipped() -> None:
    plan = build_drain_plan([_pod("ds-1", owner_kind="DaemonSet")], [])
    assert plan.targets == ()
    assert plan.skipped_daemonset == ("default/ds-1",)


def test_replicaset_owned_pods_are_not_skipped() -> None:
    plan = build_drain_plan([_pod("web-1", owner_kind="ReplicaSet")], [])
    assert len(plan.targets) == 1
    assert plan.skipped_daemonset == ()


def test_mirror_pods_are_skipped() -> None:
    pod = _pod("kube-apiserver-node1", annotations={"kubernetes.io/config.mirror": "abc"})
    plan = build_drain_plan([pod], [])
    assert plan.targets == ()
    assert plan.skipped_mirror == ("default/kube-apiserver-node1",)


def test_emptydir_pods_are_flagged_but_still_evicted() -> None:
    pod = _pod("cache-1", volumes=[{"name": "scratch", "emptyDir": {}}])
    plan = build_drain_plan([pod], [])
    assert len(plan.targets) == 1
    assert plan.targets[0].local_storage


def test_non_emptydir_volumes_are_not_flagged() -> None:
    pod = _pod("web-1", volumes=[{"name": "cfg", "configMap": {"name": "cfg"}}])
    plan = build_drain_plan([pod], [])
    assert not plan.targets[0].local_storage


def test_pdb_with_zero_disruptions_blocks_matching_pod() -> None:
    pod = _pod("db-1", labels={"app": "db"})
    pdb = _pdb("db-pdb", match_labels={"app": "db"}, disruptions_allowed=0)
    plan = build_drain_plan([pod], [pdb])
    assert plan.targets[0].pdb_blocked == "db-pdb"


def test_pdb_with_budget_left_does_not_block() -> None:
    pod = _pod("db-1", labels={"app": "db"})
    pdb = _pdb("db-pdb", match_labels={"app": "db"}, disruptions_allowed=1)
    plan = build_drain_plan([pod], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_pdb_in_other_namespace_does_not_block() -> None:
    pod = _pod("db-1", namespace="prod", labels={"app": "db"})
    pdb = _pdb("db-pdb", namespace="staging", match_labels={"app": "db"})
    plan = build_drain_plan([pod], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_pdb_selector_mismatch_does_not_block() -> None:
    pod = _pod("web-1", labels={"app": "web"})
    pdb = _pdb("db-pdb", match_labels={"app": "db"})
    plan = build_drain_plan([pod], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_pdb_empty_selector_matches_all_pods_in_namespace() -> None:
    # policy/v1 semantics: an empty selector selects every pod in the ns.
    pod = _pod("web-1", labels={"app": "web"})
    plan = build_drain_plan([pod], [_pdb("all-pdb")])
    assert plan.targets[0].pdb_blocked == "all-pdb"


def test_pdb_match_expressions() -> None:
    pod = _pod("web-1", labels={"tier": "backend"})
    blocked = _pdb(
        "expr-pdb",
        match_expressions=[{"key": "tier", "operator": "In", "values": ["backend"]}],
    )
    not_matching = _pdb(
        "other-pdb",
        match_expressions=[{"key": "tier", "operator": "NotIn", "values": ["backend"]}],
    )
    exists = _pdb(
        "exists-pdb",
        match_expressions=[{"key": "tier", "operator": "Exists"}],
    )
    absent = _pdb(
        "absent-pdb",
        match_expressions=[{"key": "missing", "operator": "DoesNotExist"}],
    )
    assert build_drain_plan([pod], [blocked]).targets[0].pdb_blocked == "expr-pdb"
    assert build_drain_plan([pod], [not_matching]).targets[0].pdb_blocked is None
    assert build_drain_plan([pod], [exists]).targets[0].pdb_blocked == "exists-pdb"
    assert build_drain_plan([pod], [absent]).targets[0].pdb_blocked == "absent-pdb"


def test_terminal_pods_are_evicted_without_pdb_check() -> None:
    # Succeeded/Failed pods do not count against a PDB; evicting them is free.
    pod = _pod("done-1", labels={"app": "db"}, phase="Succeeded")
    pdb = _pdb("db-pdb", match_labels={"app": "db"}, disruptions_allowed=0)
    plan = build_drain_plan([pod], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_preview_lines_cover_every_category() -> None:
    pods = [
        _pod("web-1", uid="u1"),
        _pod("cache-1", volumes=[{"name": "scratch", "emptyDir": {}}]),
        _pod("db-1", labels={"app": "db"}),
        _pod("ds-1", owner_kind="DaemonSet"),
        _pod("mirror-1", annotations={"kubernetes.io/config.mirror": "abc"}),
    ]
    pdbs = [_pdb("db-pdb", match_labels={"app": "db"}, disruptions_allowed=0)]
    plan = build_drain_plan(pods, pdbs)
    out = "\n".join(plan.preview_lines())
    assert "Pods to evict (2)" in out
    assert "default/web-1" in out
    assert "default/cache-1" in out
    assert "local storage: emptyDir" in out
    assert "Blocked by PodDisruptionBudget (1)" in out
    assert "default/db-1" in out
    assert "db-pdb" in out
    assert "DaemonSet pods skipped (1)" in out
    assert "default/ds-1" in out
    assert "Mirror (static) pods skipped (1)" in out
    assert "default/mirror-1" in out


def test_preview_lines_when_nothing_to_evict() -> None:
    plan = build_drain_plan([], [])
    out = "\n".join(plan.preview_lines())
    assert "No pods to evict" in out


def test_plan_is_a_drainplan_dataclass() -> None:
    plan = build_drain_plan([], [])
    assert isinstance(plan, DrainPlan)


def test_non_controller_daemonset_reference_is_still_evicted() -> None:
    """Only a *controlling* DaemonSet owner exempts a pod (kubectl drain
    semantics); a non-controller reference must not hide the pod from the
    plan."""
    pod = _pod("web-1", uid="u1")
    pod["metadata"]["ownerReferences"] = [
        {"kind": "DaemonSet", "name": "owner", "controller": False},
    ]
    plan = build_drain_plan([pod], [])
    assert plan.skipped_daemonset == ()
    assert [t.name for t in plan.targets] == ["web-1"]


def test_multiple_matching_pdbs_block_regardless_of_budget() -> None:
    """The Eviction API rejects a pod matched by more than one PDB with a
    500 even when every budget has room; the plan must warn up front."""
    pod = _pod("web-1", uid="u1", labels={"app": "web"})
    pdbs = [
        _pdb("pdb-a", match_labels={"app": "web"}, disruptions_allowed=5),
        _pdb("pdb-b", match_labels={"app": "web"}, disruptions_allowed=5),
    ]
    plan = build_drain_plan([pod], pdbs)
    blocked = plan.targets[0].pdb_blocked
    assert blocked is not None
    assert "multiple PDBs match" in blocked
    assert "pdb-a" in blocked
    assert "pdb-b" in blocked


def test_pdb_allowance_is_allocated_across_the_plan() -> None:
    """disruptionsAllowed is budget-wide: a budget of 1 covering two pods
    on the node lets only the first through; the second is blocked."""
    pods = [
        _pod("web-1", uid="u1", labels={"app": "web"}),
        _pod("web-2", uid="u2", labels={"app": "web"}),
    ]
    pdbs = [_pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=1)]
    plan = build_drain_plan(pods, pdbs)
    assert plan.targets[0].pdb_blocked is None
    assert plan.targets[1].pdb_blocked == "web-pdb"


def test_null_selector_matches_no_pods() -> None:
    """policy/v1: a null/missing selector matches no pods; only an
    explicitly empty {} selector matches the whole namespace."""
    pod = _pod("web-1", uid="u1", labels={"app": "web"})
    null_selector_pdb = {
        "metadata": {"name": "null-pdb", "namespace": "default"},
        "spec": {},
        "status": {"disruptionsAllowed": 0},
    }
    plan = build_drain_plan([pod], [null_selector_pdb])
    assert plan.targets[0].pdb_blocked is None


def _ready(pod: dict[str, Any]) -> dict[str, Any]:
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    return pod


def test_always_allow_policy_admits_non_ready_pod_despite_exhausted_budget() -> None:
    pdb = _pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=0)
    pdb["spec"]["unhealthyPodEvictionPolicy"] = "AlwaysAllow"
    # No Ready condition -> the pod is unhealthy; AlwaysAllow admits it.
    plan = build_drain_plan([_pod("web-1", labels={"app": "web"})], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_always_allow_policy_still_blocks_ready_pod() -> None:
    pdb = _pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=0)
    pdb["spec"]["unhealthyPodEvictionPolicy"] = "AlwaysAllow"
    plan = build_drain_plan([_ready(_pod("web-1", labels={"app": "web"}))], [pdb])
    assert plan.targets[0].pdb_blocked == "web-pdb"


def test_always_allow_unhealthy_eviction_does_not_consume_the_allowance() -> None:
    pdb = _pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=1)
    pdb["spec"]["unhealthyPodEvictionPolicy"] = "AlwaysAllow"
    pods = [
        _pod("sick-1", labels={"app": "web"}),  # unhealthy: free pass
        _ready(_pod("web-1", labels={"app": "web"})),  # consumes the 1 allowance
        _ready(_pod("web-2", labels={"app": "web"})),  # budget now exhausted
    ]
    plan = build_drain_plan(pods, [pdb])
    blocked = {t.name: t.pdb_blocked for t in plan.targets}
    assert blocked == {"sick-1": None, "web-1": None, "web-2": "web-pdb"}


def test_stale_pdb_status_blocks_fail_safe() -> None:
    """Eviction admission refuses disruptions while status.observedGeneration
    trails metadata.generation, whatever the stale allowance says."""
    pdb = _pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=5)
    pdb["metadata"]["generation"] = 3
    pdb["status"]["observedGeneration"] = 2
    plan = build_drain_plan([_pod("web-1", labels={"app": "web"})], [pdb])
    assert plan.targets[0].pdb_blocked == "web-pdb (status not up to date)"


def test_stale_pdb_status_does_not_override_always_allow_unhealthy() -> None:
    pdb = _pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=0)
    pdb["spec"]["unhealthyPodEvictionPolicy"] = "AlwaysAllow"
    pdb["metadata"]["generation"] = 3
    pdb["status"]["observedGeneration"] = 2
    plan = build_drain_plan([_pod("web-1", labels={"app": "web"})], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_up_to_date_pdb_status_is_trusted() -> None:
    pdb = _pdb("web-pdb", match_labels={"app": "web"}, disruptions_allowed=1)
    pdb["metadata"]["generation"] = 3
    pdb["status"]["observedGeneration"] = 3
    plan = build_drain_plan([_pod("web-1", labels={"app": "web"})], [pdb])
    assert plan.targets[0].pdb_blocked is None


def test_notin_expression_matches_pod_missing_the_key() -> None:
    """apimachinery labels.Requirement semantics: NotIn matches objects
    that do not carry the key at all (kubectl behaves the same way)."""
    pdb = _pdb(
        "no-frontend-pdb",
        match_expressions=[{"key": "tier", "operator": "NotIn", "values": ["frontend"]}],
    )
    plan = build_drain_plan([_pod("web-1", labels={"app": "web"})], [pdb])
    assert plan.targets[0].pdb_blocked == "no-frontend-pdb"
