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


class TestBuildMetricQuery:
    def _scope_selector(self) -> str:
        return build_selector({"namespace": "prod"})

    @pytest.mark.parametrize("signal", SIGNALS)
    def test_every_declared_signal_renders_a_query(self, signal: str) -> None:
        query = build_metric_query(signal, self._scope_selector(), 30)
        assert "[30m]" in query
        assert 'namespace="prod"' in query

    @pytest.mark.parametrize("signal", SIGNALS)
    def test_every_declared_signal_declares_a_unit(self, signal: str) -> None:
        assert metric_unit(signal)

    def test_an_unknown_signal_is_refused_and_the_message_lists_the_known_ones(self) -> None:
        with pytest.raises(ConnectorError, match="cpu") as caught:
            build_metric_query("exfiltrate", self._scope_selector(), 30)
        assert caught.value.kind == "config"

    def test_the_signal_name_is_not_interpolated_into_the_query(self) -> None:
        """An unknown signal must not become a query fragment."""
        with pytest.raises(ConnectorError, match="unknown signal"):
            build_metric_query('cpu"} or up{a="', self._scope_selector(), 30)

    def test_the_window_is_the_only_numeric_input(self) -> None:
        query = build_metric_query("cpu", self._scope_selector(), 5)
        assert "[5m]" in query
        assert "[30m]" not in query
