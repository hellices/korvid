"""Tests for shared Kubernetes label selector semantics (issue #281)."""

from korvid.k8s.selectors import matches_selector, parse_label_selector


def test_absent_and_empty_selectors_remain_distinct() -> None:
    absent = parse_label_selector(None)
    empty = parse_label_selector({})
    assert absent.present is False
    assert empty.present is True
    assert matches_selector(absent, {}, empty_matches=True) is False
    assert matches_selector(empty, {}, empty_matches=True) is True
    assert matches_selector(empty, {}, empty_matches=False) is False


def test_match_labels_and_expressions_follow_kubernetes_semantics() -> None:
    selector = parse_label_selector(
        {
            "matchLabels": {"app": "api"},
            "matchExpressions": [
                {"key": "tier", "operator": "In", "values": ["backend"]},
                {"key": "debug", "operator": "DoesNotExist"},
            ],
        }
    )
    assert matches_selector(selector, {"app": "api", "tier": "backend"}, empty_matches=False)
    assert not matches_selector(
        selector,
        {"app": "api", "tier": "backend", "debug": "true"},
        empty_matches=False,
    )


def test_unknown_operator_never_matches() -> None:
    selector = parse_label_selector(
        {"matchExpressions": [{"key": "app", "operator": "Equals", "values": ["api"]}]}
    )
    assert not matches_selector(selector, {"app": "api"}, empty_matches=True)
