"""Tests for the diagnose_pod projection functions (issue #70).

Pure functions only: manifests and event lists in, report lines out. The
tool orchestration (fetching) is covered in test_tools.py.
"""

from __future__ import annotations

from typing import Any

from korvid.agent.diagnose import (
    condition_lines,
    container_state_lines,
    identity_lines,
    log_excerpt,
    node_condition_line,
    previous_log_containers,
    pvc_names,
    troubled_containers,
    warning_event_lines,
)


def _crashloop_pod() -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {
            "name": "api-1",
            "namespace": "default",
            "creationTimestamp": "2026-07-27T06:00:00Z",
        },
        "spec": {
            "nodeName": "node-a",
            "containers": [{"name": "app", "image": "api:v2"}],
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "data-claim"}},
                {"name": "tmp", "emptyDir": {}},
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "ContainersNotReady"},
                {"type": "PodScheduled", "status": "True"},
            ],
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 7,
                    "image": "api:v2",
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m0s restarting failed container",
                        }
                    },
                    "lastState": {
                        "terminated": {"exitCode": 1, "reason": "Error"},
                    },
                },
            ],
        },
    }


# --- identity ---------------------------------------------------------------


def test_identity_lines_show_phase_node_and_creation() -> None:
    lines = identity_lines(_crashloop_pod())
    joined = "\n".join(lines)
    assert "phase=Running" in joined
    assert "node=node-a" in joined
    assert "created=2026-07-27T06:00:00Z" in joined


def test_identity_lines_survive_an_empty_manifest() -> None:
    lines = identity_lines({})
    assert lines  # never empty: a pending pod still gets a phase line
    assert "phase=?" in "\n".join(lines)


# --- container states -------------------------------------------------------


def test_container_state_lines_show_waiting_reason_and_last_exit() -> None:
    lines = container_state_lines(_crashloop_pod())
    joined = "\n".join(lines)
    assert "app" in joined
    assert "waiting reason=CrashLoopBackOff" in joined
    assert "back-off 5m0s" in joined
    assert "restarts=7" in joined
    assert "last-exit=1 (Error)" in joined


def test_container_state_lines_include_init_containers() -> None:
    pod = _crashloop_pod()
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "migrate",
            "ready": False,
            "restartCount": 2,
            "state": {"terminated": {"exitCode": 3, "reason": "Error"}},
        }
    ]
    joined = "\n".join(container_state_lines(pod))
    assert "init migrate" in joined
    assert "terminated exit=3 (Error)" in joined


def test_container_state_lines_mark_a_healthy_container_running() -> None:
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 0,
            "state": {"running": {"startedAt": "2026-07-27T06:01:00Z"}},
        }
    ]
    joined = "\n".join(container_state_lines(pod))
    assert "running since 2026-07-27T06:01:00Z" in joined
    assert "ready" in joined


# --- troubled containers ----------------------------------------------------


def test_troubled_containers_pick_the_crashlooping_one() -> None:
    assert troubled_containers(_crashloop_pod()) == ["app"]


def test_troubled_containers_skip_healthy_ones() -> None:
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 0,
            "state": {"running": {"startedAt": "x"}},
        }
    ]
    assert troubled_containers(pod) == []


def test_troubled_containers_include_a_restarted_but_now_ready_container() -> None:
    """Restarts mean the evidence is in the logs even if it recovered."""
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 3,
            "state": {"running": {"startedAt": "x"}},
        }
    ]
    assert troubled_containers(pod) == ["app"]


def test_troubled_containers_include_failed_init_containers() -> None:
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = []
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "migrate",
            "ready": False,
            "restartCount": 1,
            "state": {"terminated": {"exitCode": 3}},
        }
    ]
    assert troubled_containers(pod) == ["migrate"]


def test_troubled_containers_skip_a_succeeded_init_container() -> None:
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = []
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "migrate",
            "ready": True,
            "restartCount": 0,
            "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
        }
    ]
    assert troubled_containers(pod) == []


def test_troubled_containers_skip_a_completed_init_container_despite_restarts() -> None:
    """Earlier failed attempts leave restartCount > 0 on a now-Completed init
    container; it succeeded, so its logs carry no diagnostic evidence."""
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = []
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "migrate",
            "ready": True,
            "restartCount": 2,
            "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
        }
    ]
    assert troubled_containers(pod) == []


def test_troubled_containers_include_a_completed_regular_container_with_restarts() -> None:
    """The Completed exception is for init containers only — a regular
    container that exited 0 after prior restarts still crashed before,
    and that evidence lives in its previous logs."""
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = [
        {
            "name": "worker",
            "ready": False,
            "restartCount": 3,
            "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
        }
    ]
    assert troubled_containers(pod) == ["worker"]


def test_troubled_containers_skip_a_completed_regular_container_without_restarts() -> None:
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"] = [
        {
            "name": "worker",
            "ready": False,
            "restartCount": 0,
            "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
        }
    ]
    assert troubled_containers(pod) == []


def test_previous_log_containers_lists_waiting_restarted_names() -> None:
    pod = _crashloop_pod()
    pod["status"]["initContainerStatuses"] = [
        {"name": "migrate", "restartCount": 0, "state": {"terminated": {"exitCode": 0}}}
    ]
    assert previous_log_containers(pod) == {"app"}


def test_previous_log_containers_excludes_currently_failed_termination() -> None:
    """A container terminated non-zero *right now* logged its failure in the
    current instance — previous would be the penultimate crash."""
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"][0]["state"] = {
        "terminated": {"exitCode": 1, "reason": "Error"}
    }
    assert previous_log_containers(pod) == set()


def test_previous_log_containers_includes_completed_with_restarts() -> None:
    pod = _crashloop_pod()
    pod["status"]["containerStatuses"][0]["state"] = {
        "terminated": {"exitCode": 0, "reason": "Completed"}
    }
    assert previous_log_containers(pod) == {"app"}


def test_previous_log_containers_empty_status_yields_empty() -> None:
    assert previous_log_containers({}) == set()


# --- conditions -------------------------------------------------------------


def test_condition_lines_put_failing_conditions_first() -> None:
    lines = condition_lines(_crashloop_pod())
    assert lines[0].startswith("Ready=False")
    assert "ContainersNotReady" in lines[0]
    assert any(line.startswith("PodScheduled=True") for line in lines[1:])


def test_condition_lines_empty_status_yields_no_lines() -> None:
    assert condition_lines({}) == []


# --- warning events ---------------------------------------------------------


def _event(
    reason: str, message: str, *, etype: str = "Warning", ts: str = "", count: int = 1
) -> dict[str, Any]:
    return {
        "type": etype,
        "reason": reason,
        "message": message,
        "count": count,
        "lastTimestamp": ts,
    }


def test_warning_event_lines_filter_dedupe_and_sort_newest_first() -> None:
    events = [
        _event("BackOff", "restarting failed container", ts="2026-07-27T06:10:00Z", count=5),
        _event("Pulled", "image pulled", etype="Normal", ts="2026-07-27T06:00:00Z"),
        _event("BackOff", "restarting failed container", ts="2026-07-27T06:05:00Z", count=3),
        _event("Unhealthy", "liveness probe failed", ts="2026-07-27T06:12:00Z"),
    ]
    lines = warning_event_lines(events)
    assert len(lines) == 2  # Normal dropped, duplicate BackOff deduplicated
    assert lines[0].startswith("Unhealthy")  # newest first
    assert "BackOff (5x" in lines[1]  # the newer duplicate wins


def test_warning_event_lines_cap_the_list() -> None:
    events = [_event(f"R{i}", f"m{i}", ts=f"2026-07-27T06:{i:02d}:00Z") for i in range(30)]
    lines = warning_event_lines(events)
    assert len(lines) <= 11  # cap + a "more" marker at most
    assert any("more warning" in line for line in lines)


def test_warning_event_lines_no_warnings_yields_empty() -> None:
    assert warning_event_lines([_event("Pulled", "ok", etype="Normal")]) == []


def test_warning_event_lines_prefer_series_fields_for_repeating_events() -> None:
    """events.k8s.io series record recurrence in `series.count` and
    `series.lastObservedTime`; the top-level fallbacks describe only the
    initial observation of an actively repeating warning."""
    repeating = _event("BackOff", "restarting failed container", ts="2026-07-27T05:00:00Z")
    repeating["series"] = {"count": 40, "lastObservedTime": "2026-07-27T06:30:00Z"}
    fresh = _event("Unhealthy", "probe failed", ts="2026-07-27T06:00:00Z")
    lines = warning_event_lines([fresh, repeating])
    assert lines[0].startswith("BackOff (40x, last 2026-07-27T06:30:00Z")
    assert lines[1].startswith("Unhealthy")


def test_warning_event_lines_sort_by_parsed_instants_not_strings() -> None:
    """RFC 3339 strings do not sort chronologically once offsets differ."""
    # 10:00+09:00 is 01:00Z — older than 05:00Z despite the larger string.
    older = _event("A", "offset ts", ts="2026-07-27T10:00:00+09:00")
    newer = _event("B", "utc ts", ts="2026-07-27T05:00:00Z")
    lines = warning_event_lines([older, newer])
    assert lines[0].startswith("B")
    assert lines[1].startswith("A")


def test_warning_event_lines_fall_back_to_creation_timestamp() -> None:
    """Some events carry only `metadata.creationTimestamp` — without the
    fallback they all sort as epoch and render without a time."""
    older = _event("A", "older", ts="")
    older["metadata"] = {"creationTimestamp": "2026-07-27T05:00:00Z"}
    newer = _event("B", "newer", ts="")
    newer["metadata"] = {"creationTimestamp": "2026-07-27T06:00:00Z"}
    lines = warning_event_lines([older, newer])
    assert lines[0].startswith("B")
    assert "last 2026-07-27T06:00:00Z" in lines[0]
    assert lines[1].startswith("A")


# --- log excerpt ------------------------------------------------------------


def test_log_excerpt_centers_on_the_last_error_and_keeps_the_tail() -> None:
    lines = [f"info {i}" for i in range(50)]
    lines[20] = "ERROR: connection refused"
    out = log_excerpt(lines, context=2, final=3)
    assert "ERROR: connection refused" in out
    assert "info 18" in out  # context before the match
    assert "info 22" in out  # context after the match
    assert "info 49" in out  # final lines always kept
    assert "info 5" not in out  # untargeted middle dropped
    assert "…" in out  # elision between the excerpt and the tail is visible


def test_log_excerpt_without_error_match_returns_only_the_tail() -> None:
    lines = [f"line {i}" for i in range(30)]
    out = log_excerpt(lines, context=2, final=5)
    assert "line 29" in out
    assert "line 25" in out
    assert "line 0" not in out


def test_log_excerpt_short_log_is_returned_whole() -> None:
    lines = ["a", "ERROR: b", "c"]
    assert log_excerpt(lines, context=2, final=5) == "a\nERROR: b\nc"


def test_log_excerpt_matches_panic_and_fatal_case_insensitively() -> None:
    lines = [f"x {i}" for i in range(40)]
    lines[10] = "panic: runtime error"
    out = log_excerpt(lines, context=1, final=2)
    assert "panic: runtime error" in out


# --- related ----------------------------------------------------------------


def test_pvc_names_lists_only_claim_volumes() -> None:
    assert pvc_names(_crashloop_pod()) == ["data-claim"]


def test_pvc_names_empty_spec_yields_empty() -> None:
    assert pvc_names({}) == []


def test_pvc_names_dedupes_repeated_claims_preserving_spec_order() -> None:
    pod = {
        "spec": {
            "volumes": [
                {"name": "v1", "persistentVolumeClaim": {"claimName": "shared"}},
                {"name": "v2", "persistentVolumeClaim": {"claimName": "logs"}},
                {"name": "v3", "persistentVolumeClaim": {"claimName": "shared"}},
            ]
        }
    }
    assert pvc_names(pod) == ["shared", "logs"]


def test_node_condition_line_summarizes_pressure_conditions() -> None:
    node = {
        "metadata": {"name": "node-a"},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "MemoryPressure", "status": "True"},
                {"type": "DiskPressure", "status": "False"},
            ]
        },
    }
    line = node_condition_line(node)
    assert "node-a" in line
    assert "Ready=True" in line
    assert "MemoryPressure=True" in line
    assert "DiskPressure" not in line  # only Ready and abnormal conditions shown


def test_node_condition_line_treats_unknown_pressure_as_abnormal() -> None:
    """Unknown means the kubelet cannot report — that is not healthy."""
    node = {
        "metadata": {"name": "node-a"},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "Unknown"},
                {"type": "MemoryPressure", "status": "Unknown"},
                {"type": "DiskPressure", "status": "False"},
            ]
        },
    }
    line = node_condition_line(node)
    assert "Ready=Unknown" in line
    assert "MemoryPressure=Unknown" in line
    assert "DiskPressure" not in line


def test_node_condition_line_handles_missing_conditions() -> None:
    assert "no conditions reported" in node_condition_line({"metadata": {"name": "n"}})
