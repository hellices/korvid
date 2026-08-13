"""Query construction for PromQL/LogQL selectors (issue #193).

The model never writes a query. It names a signal and a Kubernetes scope,
and these functions render the query. That only holds if a hostile label
*value* cannot break out of the string it is placed in, which is what
most of this file tests.
"""

from __future__ import annotations

import pytest

from korvid.obs.connector import SIGNALS, ConnectorError
from korvid.obs.query import (
    build_line_filter,
    build_metric_query,
    build_selector,
    escape_label_value,
    escape_regex_value,
    metric_unit,
)
from tests.obs import skeleton

#: One string carrying every character that ends a quoted value, starts a
#: new matcher, or ends the selector.
HOSTILE = 'x"} or up{job="admin'


class TestEscaping:
    def test_a_quote_cannot_close_the_value(self) -> None:
        assert escape_label_value('a"b') == 'a\\"b'

    def test_a_backslash_is_escaped_before_the_quote_is(self) -> None:
        """`\\` then `"` must not compose into an escaped quote."""
        assert escape_label_value('a\\"b') == 'a\\\\\\"b'

    @pytest.mark.parametrize("char", ["\n", "\r", "\t"])
    def test_control_characters_are_escaped_not_emitted(self, char: str) -> None:
        assert char not in escape_label_value(f"a{char}b")

    def test_the_hostile_value_keeps_every_metacharacter_inert(self) -> None:
        escaped = escape_label_value(HOSTILE)
        # Every quote in the output is preceded by a backslash, so none of
        # them can terminate the string literal it sits in.
        assert '\\"' in escaped
        assert escaped.count('"') == escaped.count('\\"')

    def test_a_regex_value_is_regex_escaped_as_well(self) -> None:
        escaped = escape_regex_value("a.*b")
        assert "a.*b" not in escaped
        assert "\\." in escaped

    def test_a_regex_value_is_also_string_escaped(self) -> None:
        """Regex escaping alone still leaves a quote able to close the value."""
        assert '"' not in escape_regex_value('a"b').replace('\\"', "")


class TestBuildSelector:
    def test_an_exact_matcher_is_rendered_for_each_label(self) -> None:
        assert build_selector({"namespace": "prod", "pod": "api-1"}) == (
            '{namespace="prod", pod="api-1"}'
        )

    def test_a_regex_matcher_uses_the_regex_operator(self) -> None:
        selector = build_selector({"namespace": "prod"}, regex={"pod": "api-.*"})
        assert 'pod=~"' in selector

    def test_a_hostile_value_cannot_add_a_matcher(self) -> None:
        """Metacharacters inside the literal contribute no structure."""
        assert skeleton(build_selector({"namespace": HOSTILE})) == '{namespace=""}'

    def test_an_empty_selector_is_refused(self) -> None:
        """An unscoped selector would ask the backend for everything."""
        with pytest.raises(ConnectorError, match="scope"):
            build_selector({})

    def test_an_empty_label_value_is_refused(self) -> None:
        with pytest.raises(ConnectorError, match="namespace"):
            build_selector({"namespace": ""})

    def test_an_over_long_value_is_refused(self) -> None:
        with pytest.raises(ConnectorError, match="too long"):
            build_selector({"namespace": "a" * 1024})

    def test_labels_are_rendered_in_a_stable_order(self) -> None:
        first = build_selector({"pod": "a", "namespace": "b"})
        second = build_selector({"namespace": "b", "pod": "a"})
        assert first == second


class TestBuildLineFilter:
    def test_no_filter_renders_nothing(self) -> None:
        assert build_line_filter(None) == ""

    def test_a_substring_becomes_a_quoted_line_filter(self) -> None:
        assert build_line_filter("boom") == ' |= "boom"'

    def test_a_hostile_substring_cannot_start_a_new_stage(self) -> None:
        """`| label_format` after an unescaped quote would be a new stage."""
        assert skeleton(build_line_filter('boom" | label_format foo="bar')) == ' |= ""'

    def test_an_over_long_substring_is_refused(self) -> None:
        with pytest.raises(ConnectorError, match="too long"):
            build_line_filter("a" * 1024)

    def test_a_blank_substring_is_treated_as_absent(self) -> None:
        assert build_line_filter("   ") == ""


#: The scope every metric-query test asks about.
SCOPE = {"namespace": "prod"}


class TestBuildMetricQuery:
    @pytest.mark.parametrize("signal", SIGNALS)
    def test_every_declared_signal_renders_a_query(self, signal: str) -> None:
        query = build_metric_query(signal, SCOPE, window_minutes=30)
        assert "[30m]" in query
        assert 'namespace="prod"' in query

    @pytest.mark.parametrize("signal", SIGNALS)
    def test_every_declared_signal_declares_a_unit(self, signal: str) -> None:
        assert metric_unit(signal)

    def test_a_signal_that_needs_an_extra_matcher_declares_it_in_the_selector(self) -> None:
        """The 5xx class is part of what `error_rate` means, not an edit."""
        query = build_metric_query("error_rate", SCOPE, window_minutes=30)
        assert 'code=~"5.."' in query
        assert skeleton(query).count("{") == 1

    def test_a_signal_without_an_extra_matcher_gets_none(self) -> None:
        assert "code" not in build_metric_query("cpu", SCOPE, window_minutes=30)

    def test_an_unknown_signal_is_refused_and_the_message_lists_the_known_ones(self) -> None:
        with pytest.raises(ConnectorError, match="cpu") as caught:
            build_metric_query("exfiltrate", SCOPE, window_minutes=30)
        assert caught.value.kind == "config"

    def test_the_signal_name_is_not_interpolated_into_the_query(self) -> None:
        """An unknown signal must not become a query fragment."""
        with pytest.raises(ConnectorError, match="unknown signal"):
            build_metric_query('cpu"} or up{a="', SCOPE, window_minutes=30)

    def test_a_regex_matcher_reaches_the_rendered_query(self) -> None:
        query = build_metric_query("cpu", SCOPE, {"pod": "api-"}, window_minutes=30)
        assert 'pod=~"' in query

    def test_the_window_is_the_only_numeric_input(self) -> None:
        query = build_metric_query("cpu", SCOPE, window_minutes=5)
        assert "[5m]" in query
        assert "[30m]" not in query


class TestLabelNamesAreValidated:
    """Round-4 review: a label *name* is configuration, and it was interpolated.

    Values were escaped from the first commit, but `label_mappings` names
    reach the selector verbatim, so a configured name could close the
    selector and open another one.
    """

    def test_a_name_that_would_close_the_selector_is_refused(self) -> None:
        with pytest.raises(ConnectorError, match="label name") as caught:
            build_selector({'namespace="prod"} or {namespace': "x"})
        assert caught.value.kind == "config"

    @pytest.mark.parametrize(
        "name", ['a"b', "a b", "a-b", "a.b", "1abc", "", "a}b", "a,b", "a=b", "a\nb"]
    )
    def test_only_the_label_grammar_is_accepted(self, name: str) -> None:
        with pytest.raises(ConnectorError, match="label name"):
            build_selector({name: "x"})

    @pytest.mark.parametrize("name", ["namespace", "_private", "k8s_app_name", "A9"])
    def test_a_conventional_label_name_is_accepted(self, name: str) -> None:
        assert f'{name}="x"' in build_selector({name: "x"})

    def test_a_regex_matcher_name_is_validated_too(self) -> None:
        with pytest.raises(ConnectorError, match="label name"):
            build_selector({"namespace": "prod"}, regex={'pod"} or up{a': "x"})
