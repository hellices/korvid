"""Structural manifest diff for dry-run previews (issue #19)."""

from typing import Any

from korvid.k8s.dryrun import diff_manifests


class TestDiffManifests:
    def test_changed_scalar(self) -> None:
        cur = {"spec": {"replicas": 3}}
        new = {"spec": {"replicas": 5}}
        assert diff_manifests(cur, new) == ["~ spec.replicas: 3 -> 5"]

    def test_added_nested_key_lists_leaves(self) -> None:
        cur: dict[str, Any] = {"spec": {"template": {"metadata": {}}}}
        new = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": "2026-01-01T00:00:00"}
                    }
                }
            }
        }
        assert diff_manifests(cur, new) == [
            "+ spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt:"
            ' "2026-01-01T00:00:00"'
        ]

    def test_removed_key(self) -> None:
        cur = {"spec": {"paused": True}}
        new: dict[str, Any] = {"spec": {}}
        assert diff_manifests(cur, new) == ["- spec.paused: true"]

    def test_ignores_status_and_metadata_noise(self) -> None:
        cur = {
            "metadata": {"resourceVersion": "1", "generation": 1, "managedFields": [{"a": 1}]},
            "status": {"replicas": 3},
            "spec": {"replicas": 3},
        }
        new = {
            "metadata": {"resourceVersion": "2", "generation": 2, "managedFields": []},
            "status": {"replicas": 5},
            "spec": {"replicas": 3},
        }
        assert diff_manifests(cur, new) == []

    def test_equal_manifests_empty(self) -> None:
        m = {"spec": {"replicas": 3}, "metadata": {"name": "web"}}
        assert diff_manifests(m, m) == []

    def test_truncates_to_max_lines(self) -> None:
        cur = {"spec": {f"k{i}": i for i in range(20)}}
        new = {"spec": {f"k{i}": i + 1 for i in range(20)}}
        lines = diff_manifests(cur, new, max_lines=5)
        assert len(lines) == 6
        assert lines[-1] == "... (+15 more changes)"

    def test_list_change_is_single_line(self) -> None:
        cur = {"spec": {"args": ["a", "b"]}}
        new = {"spec": {"args": ["a", "c"]}}
        lines = diff_manifests(cur, new)
        assert len(lines) == 1
        assert lines[0].startswith("~ spec.args: ")

    def test_long_values_truncated(self) -> None:
        cur = {"spec": {"cmd": "x"}}
        new = {"spec": {"cmd": "y" * 200}}
        (line,) = diff_manifests(cur, new)
        assert len(line) < 160
        assert "..." in line

    def test_metadata_name_change_still_visible(self) -> None:
        # only noisy metadata keys are ignored, not the whole subtree
        cur = {"metadata": {"labels": {"app": "web"}}}
        new = {"metadata": {"labels": {"app": "web2"}}}
        assert diff_manifests(cur, new) == ['~ metadata.labels.app: "web" -> "web2"']


def test_bool_vs_int_is_a_change() -> None:
    """JSON scalar types are distinct: True is not 1 (Python equality
    conflates them). An admission change from boolean to integer must show."""
    assert diff_manifests({"spec": {"flag": True}}, {"spec": {"flag": 1}}) == [
        "~ spec.flag: true -> 1"
    ]
    assert diff_manifests({"spec": {"n": 0}}, {"spec": {"n": False}}) == ["~ spec.n: 0 -> false"]


def test_bool_vs_int_inside_list_is_a_change() -> None:
    """Lists compare atomically, but with JSON type semantics."""
    assert diff_manifests({"spec": {"xs": [True, 2]}}, {"spec": {"xs": [1, 2]}}) == [
        "~ spec.xs: [true, 2] -> [1, 2]"
    ]
