"""Column sorting (issue #37): sort keys on the data model, not rendered
strings — '150m' CPU compares numerically, ages compare as timestamps,
missing values sort last deterministically."""

from __future__ import annotations

import pytest

from korvid.core.sorting import SortSpec, sort_rows, toggle_sort
from korvid.k8s.metrics import PodMetrics
from korvid.k8s.models import GenericSummary, PodSummary


def _pod(name: str, namespace: str = "default", created: str = "") -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node="n1",
        created=created,
    )


def _generic(name: str, created: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="ConfigMap", created=created)


def _metrics(cpu: float, mem: int) -> PodMetrics:
    return PodMetrics(name="", namespace="", cpu_cores=cpu, memory_bytes=mem)


# ---------------------------------------------------------------------------
# toggle_sort
# ---------------------------------------------------------------------------


def test_first_toggle_name_is_ascending() -> None:
    spec = toggle_sort(None, "name")
    assert spec == SortSpec("name", descending=False)


def test_first_toggle_metrics_columns_are_descending() -> None:
    # Triaging starts from "what's eating the most" — biggest first.
    assert toggle_sort(None, "cpu").descending
    assert toggle_sort(None, "mem").descending
    assert toggle_sort(None, "age").descending


def test_repeat_toggle_flips_direction() -> None:
    spec = toggle_sort(SortSpec("name", descending=False), "name")
    assert spec == SortSpec("name", descending=True)


def test_toggle_different_column_resets_direction() -> None:
    spec = toggle_sort(SortSpec("cpu", descending=True), "name")
    assert spec == SortSpec("name", descending=False)


# ---------------------------------------------------------------------------
# name / age
# ---------------------------------------------------------------------------


def test_sort_by_name_is_case_insensitive() -> None:
    rows = [_pod("Zeta"), _pod("alpha"), _pod("Beta")]
    ordered = sort_rows(rows, SortSpec("name", descending=False))
    assert [r.name for r in ordered] == ["alpha", "Beta", "Zeta"]


def test_sort_by_name_descending() -> None:
    rows = [_pod("a"), _pod("c"), _pod("b")]
    ordered = sort_rows(rows, SortSpec("name", descending=True))
    assert [r.name for r in ordered] == ["c", "b", "a"]


def test_sort_by_age_compares_timestamps_not_rendered_strings() -> None:
    # Rendered ages '3h' vs '25m' would compare lexically; timestamps must win.
    old = _generic("old", "2026-07-26T09:00:00Z")
    young = _generic("young", "2026-07-26T11:35:00Z")
    ordered = sort_rows([young, old], SortSpec("age", descending=True))
    assert [r.name for r in ordered] == ["young", "old"]
    ordered = sort_rows([young, old], SortSpec("age", descending=False))
    assert [r.name for r in ordered] == ["old", "young"]


def test_sort_by_age_parses_timezone_offsets() -> None:
    # 10:00+01:00 is 09:00Z — lexically later but chronologically *older*
    # than 09:30Z, so string comparison would order these wrong.
    offset = _generic("offset", "2026-07-26T10:00:00+01:00")
    zulu = _generic("zulu", "2026-07-26T09:30:00Z")
    ordered = sort_rows([offset, zulu], SortSpec("age", descending=False))
    assert [r.name for r in ordered] == ["offset", "zulu"]


def test_unparsable_created_is_treated_as_missing() -> None:
    good = _generic("good", "2026-07-26T09:00:00Z")
    bad = _generic("bad", "not-a-timestamp")
    for descending in (False, True):
        ordered = sort_rows([bad, good], SortSpec("age", descending=descending))
        assert [r.name for r in ordered] == ["good", "bad"]


def test_rows_without_created_sort_last_in_both_directions() -> None:
    dated = _generic("dated", "2026-07-26T09:00:00Z")
    undated = _generic("undated", "")
    for descending in (False, True):
        ordered = sort_rows([undated, dated], SortSpec("age", descending=descending))
        assert [r.name for r in ordered] == ["dated", "undated"]


# ---------------------------------------------------------------------------
# cpu / mem via metrics lookup
# ---------------------------------------------------------------------------


def test_sort_by_cpu_uses_metrics_numbers() -> None:
    pods = [_pod("small"), _pod("big"), _pod("mid")]
    usage = {"small": _metrics(0.15, 1), "big": _metrics(1.0, 1), "mid": _metrics(0.5, 1)}
    ordered = sort_rows(
        pods,
        SortSpec("cpu", descending=True),
        metrics=lambda ns, name: usage.get(name),
    )
    assert [r.name for r in ordered] == ["big", "mid", "small"]


def test_sort_by_mem_ascending() -> None:
    pods = [_pod("big"), _pod("small")]
    usage = {"small": _metrics(0.1, 100), "big": _metrics(0.1, 5000)}
    ordered = sort_rows(
        pods,
        SortSpec("mem", descending=False),
        metrics=lambda ns, name: usage.get(name),
    )
    assert [r.name for r in ordered] == ["small", "big"]


def test_pods_without_metrics_sort_last_deterministically() -> None:
    pods = [_pod("nometrics-b"), _pod("hot"), _pod("nometrics-a")]
    usage = {"hot": _metrics(2.0, 1)}
    for descending in (False, True):
        ordered = sort_rows(
            pods,
            SortSpec("cpu", descending=descending),
            metrics=lambda ns, name: usage.get(name),
        )
        assert ordered[0].name == "hot"
        # Missing metrics always last, ordered by (namespace, name).
        assert [r.name for r in ordered[1:]] == ["nometrics-a", "nometrics-b"]


def test_metrics_sort_without_lookup_falls_back_to_name_order() -> None:
    pods = [_pod("b"), _pod("a")]
    ordered = sort_rows(pods, SortSpec("cpu", descending=True), metrics=None)
    assert [r.name for r in ordered] == ["a", "b"]


def test_ties_break_by_namespace_then_name() -> None:
    rows = [
        _generic("same", "2026-07-26T09:00:00Z", namespace="zz"),
        _generic("same", "2026-07-26T09:00:00Z", namespace="aa"),
    ]
    ordered = sort_rows(rows, SortSpec("age", descending=True))
    assert [r.namespace for r in ordered] == ["aa", "zz"]


def test_sort_rows_rejects_unknown_column() -> None:
    # Without validation an unknown column would silently fall through to
    # the metrics branch and return a plausible but wrong order.
    with pytest.raises(ValueError, match="unsupported sort column"):
        sort_rows([_pod("a")], SortSpec("restarts", descending=True))
