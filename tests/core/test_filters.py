"""Filter expression parsing + matching (issue #44).

Syntax (space-separated tokens, AND-combined):
plain substring · `~fuzzy` · `/regex/` or `re:regex` · `!` negation ·
`-l key=value[,k2=v2]` label selector · `-s` hide Succeeded/Completed.
"""

from __future__ import annotations

from korvid.core.filters import parse_filter

# ---------------------------------------------------------------------------
# Plain substring (existing behavior preserved)
# ---------------------------------------------------------------------------


def test_empty_text_is_inactive_and_matches_everything() -> None:
    f = parse_filter("")
    assert not f.active
    assert f.error is None
    assert f.matches("anything")


def test_plain_token_is_case_insensitive_substring_on_name() -> None:
    f = parse_filter("web")
    assert f.active
    assert f.matches("my-WEB-app")
    assert not f.matches("database")


# ---------------------------------------------------------------------------
# Fuzzy (~) and regex (/…/ or re:)
# ---------------------------------------------------------------------------


def test_fuzzy_token_matches_subsequence() -> None:
    f = parse_filter("~wba")
    assert f.matches("web-backend-a")  # w…b…a in order
    assert not f.matches("abw")  # order matters


def test_regex_slash_form() -> None:
    f = parse_filter("/^web-[0-9]+$/")
    assert f.matches("web-12")
    assert not f.matches("web-abc")


def test_regex_prefix_form() -> None:
    f = parse_filter("re:^db-")
    assert f.matches("db-main")
    assert not f.matches("mydb-main")


def test_invalid_regex_sets_error_and_matches_all() -> None:
    f = parse_filter("/[unclosed/")
    assert f.error is not None
    assert f.matches("anything")  # broken predicate is ignored, never raises


# ---------------------------------------------------------------------------
# Negation (!)
# ---------------------------------------------------------------------------


def test_negated_substring_excludes_matches() -> None:
    f = parse_filter("!canary")
    assert f.matches("web-stable")
    assert not f.matches("web-canary")


def test_negated_regex() -> None:
    f = parse_filter("!/^kube-/")
    assert f.matches("coredns")
    assert not f.matches("kube-proxy-x")


# ---------------------------------------------------------------------------
# Label selector (-l)
# ---------------------------------------------------------------------------


def test_label_equality_selector() -> None:
    f = parse_filter("-l app=web")
    assert f.matches("x", labels={"app": "web", "tier": "front"})
    assert not f.matches("x", labels={"app": "db"})
    assert not f.matches("x", labels={})


def test_label_selector_multiple_pairs_all_required() -> None:
    f = parse_filter("-l app=web,tier=front")
    assert f.matches("x", labels={"app": "web", "tier": "front"})
    assert not f.matches("x", labels={"app": "web", "tier": "back"})


def test_label_existence_selector() -> None:
    f = parse_filter("-l app")
    assert f.matches("x", labels={"app": "anything"})
    assert not f.matches("x", labels={"tier": "front"})


def test_label_selector_without_argument_is_error() -> None:
    f = parse_filter("-l")
    assert f.error is not None
    assert f.matches("anything")


def test_repeated_label_selectors_are_and_combined() -> None:
    f = parse_filter("-l app=web -l tier=front")
    assert f.matches("x", labels={"app": "web", "tier": "front"})
    assert not f.matches("x", labels={"app": "db", "tier": "front"})
    assert not f.matches("x", labels={"app": "web", "tier": "back"})


def test_malformed_later_selector_keeps_earlier_one() -> None:
    f = parse_filter("-l app=web -l ,")
    assert f.error is not None
    assert f.matches("x", labels={"app": "web"})
    assert not f.matches("x", labels={"app": "db"})


def test_empty_label_key_is_error_and_ignored() -> None:
    f = parse_filter("-l =web")
    assert f.error is not None
    assert f.matches("anything", labels={})


def test_dangling_negation_is_error_and_ignored() -> None:
    f = parse_filter("!")
    assert f.error is not None
    assert f.matches("anything")


def test_option_token_after_dash_l_is_missing_selector_error() -> None:
    f = parse_filter("-l -s")
    assert f.error is not None
    # `-s` is still parsed as its own option, not eaten as a selector.
    assert not f.matches("job", phase="Succeeded")
    assert f.matches("web", phase="Running")


def test_empty_fuzzy_operand_is_error_and_ignored() -> None:
    f = parse_filter("~")
    assert f.error is not None
    assert f.matches("anything")


def test_negated_empty_fuzzy_is_error_and_ignored() -> None:
    f = parse_filter("!~")
    assert f.error is not None
    assert f.matches("anything")


def test_negated_empty_regex_is_error_and_ignored() -> None:
    f = parse_filter("!//")
    assert f.error is not None
    assert f.matches("anything")


def test_unterminated_regex_is_error_and_ignored() -> None:
    f = parse_filter("/foo")
    assert f.error is not None
    assert f.matches("anything")


def test_negated_unterminated_regex_is_error_and_ignored() -> None:
    f = parse_filter("!/[")
    assert f.error is not None
    assert f.matches("anything")


# ---------------------------------------------------------------------------
# Hide completed (-s)
# ---------------------------------------------------------------------------


def test_hide_completed_excludes_succeeded_and_completed_phases() -> None:
    f = parse_filter("-s")
    assert not f.matches("job-pod", phase="Succeeded")
    assert not f.matches("job-pod", phase="Completed")
    assert f.matches("web", phase="Running")
    assert f.matches("no-phase-resource")  # non-pod rows unaffected


# ---------------------------------------------------------------------------
# Combination + description
# ---------------------------------------------------------------------------


def test_tokens_are_and_combined() -> None:
    f = parse_filter("web -l app=web -s")
    assert f.matches("web-1", labels={"app": "web"}, phase="Running")
    assert not f.matches("db-1", labels={"app": "web"}, phase="Running")
    assert not f.matches("web-1", labels={"app": "db"}, phase="Running")
    assert not f.matches("web-1", labels={"app": "web"}, phase="Succeeded")


def test_describe_names_active_parts() -> None:
    f = parse_filter("web -l app=web -s")
    text = f.describe()
    assert "web" in text
    assert "app=web" in text
    assert "hide-completed" in text


def test_describe_reports_invalid_regex() -> None:
    f = parse_filter("/[bad/")
    assert "invalid regex" in f.describe()
