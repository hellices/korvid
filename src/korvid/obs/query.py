"""Render PromQL/LogQL from a signal name and a Kubernetes scope.

The model names a signal and a scope; korvid renders the query. That
separation is only worth anything if a label *value* the model chose
cannot break out of the string literal it lands in, so every value goes
through `escape_label_value` (and, for regex matchers, `escape_regex_value`
first) before it is placed.

PromQL and LogQL share selector syntax, so one builder serves both.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from korvid.obs.connector import SIGNALS, ConnectorError

#: Long enough for any Kubernetes name (253) plus a label prefix, short
#: enough that a hostile value cannot bloat the query the backend parses.
MAX_VALUE_CHARS = 512

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

#: One entry per `Signal`. `{selector}` receives the rendered selector and
#: `{range}` the window; nothing else is interpolated. Metric names are the
#: cAdvisor/kube-state-metrics/Prometheus-client conventions, which is what
#: makes this a catalogue rather than a query surface: a cluster whose
#: metrics are named differently gets an empty result, not a wrong one.
_TEMPLATES: dict[str, tuple[str, str]] = {
    "cpu": (
        "sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{selector}[{range}]))",
        "cores",
    ),
    "memory": (
        "max by (namespace, pod) (max_over_time(container_memory_working_set_bytes{selector}"
        "[{range}]))",
        "bytes",
    ),
    "restarts": (
        "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{selector}"
        "[{range}]))",
        "restarts",
    ),
    "request_rate": (
        "sum by (namespace, pod) (rate(http_requests_total{selector}[{range}]))",
        "requests/s",
    ),
    "error_rate": (
        "sum by (namespace, pod) (rate(http_requests_total{selector_with_5xx}[{range}]))",
        "requests/s",
    ),
    "latency_p95": (
        "histogram_quantile(0.95, sum by (namespace, pod, le) "
        "(rate(http_request_duration_seconds_bucket{selector}[{range}])))",
        "seconds",
    ),
}


def escape_label_value(value: str) -> str:
    """Escape `value` for a double-quoted PromQL/LogQL string literal.

    The backslash rule is first in `_ESCAPES` for a reason: escaping the
    quote first would leave the backslash of `\\"` unescaped, and the pair
    would read back as an escaped quote rather than as a backslash
    followed by the end of the string.
    """
    return "".join(_ESCAPES.get(char, char) for char in value)


def escape_regex_value(value: str) -> str:
    """Escape `value` so it matches itself literally inside a regex matcher.

    Both passes are required: regex escaping stops `.*` from widening the
    match, string escaping stops a quote from closing the literal. Neither
    substitutes for the other.
    """
    return escape_label_value(re.escape(value))


def _validate(label: str, value: str) -> None:
    if not value:
        raise ConnectorError("config", f"label {label!r} has an empty value")
    if len(value) > MAX_VALUE_CHARS:
        raise ConnectorError(
            "config", f"label {label!r} value is too long ({len(value)} > {MAX_VALUE_CHARS})"
        )


def build_selector(
    exact: Mapping[str, str], regex: Mapping[str, str] | None = None, *, suffix: str = ""
) -> str:
    """Render a `{label="value", other=~"regex"}` selector.

    Args:
        exact: Labels matched literally.
        regex: Labels matched by a regex built from an escaped literal.
        suffix: Extra matcher text appended verbatim; korvid-owned only
            (the 5xx class for the error-rate signal), never model input.

    Returns:
        The rendered selector, with labels in a stable sorted order.

    Raises:
        ConnectorError: `config` when no label is given (an unscoped
            selector asks the backend for everything), or when a value is
            empty or over-long.
    """
    matchers: list[tuple[str, str]] = []
    for label, value in exact.items():
        _validate(label, value)
        matchers.append((label, f'{label}="{escape_label_value(value)}"'))
    for label, value in (regex or {}).items():
        _validate(label, value)
        matchers.append((label, f'{label}=~"{escape_regex_value(value)}.*"'))
    if not matchers:
        raise ConnectorError("config", "a query needs at least one scope label")
    rendered = ", ".join(text for _, text in sorted(matchers))
    if suffix:
        rendered = f"{rendered}, {suffix}"
    return "{" + rendered + "}"


def build_line_filter(contains: str | None) -> str:
    """Render an optional LogQL line filter from a plain substring.

    A line filter cannot widen the label scope, which is why free text is
    allowed here and nowhere else. It is still escaped: an unescaped quote
    would close the literal and let the rest be read as another stage.

    Raises:
        ConnectorError: `config` when the substring is over-long.
    """
    if contains is None or not contains.strip():
        return ""
    if len(contains) > MAX_VALUE_CHARS:
        raise ConnectorError(
            "config", f"log filter is too long ({len(contains)} > {MAX_VALUE_CHARS})"
        )
    return f' |= "{escape_label_value(contains)}"'


def metric_unit(signal: str) -> str:
    """The unit the signal's values are in.

    Raises:
        ConnectorError: `config` for a signal outside the catalogue.
    """
    return _template(signal)[1]


def _template(signal: str) -> tuple[str, str]:
    entry = _TEMPLATES.get(signal)
    if entry is None:
        raise ConnectorError(
            "config", f"unknown signal {signal!r}; known signals are {', '.join(SIGNALS)}"
        )
    return entry


def build_metric_query(signal: str, selector: str, window_minutes: int) -> str:
    """Render the catalogue query for `signal` over `selector`.

    Raises:
        ConnectorError: `config` for a signal outside the catalogue. The
            unknown name is quoted in the message and never interpolated
            into a query.
    """
    template, _unit = _template(signal)
    window = f"{window_minutes}m"
    if "{selector_with_5xx}" in template:
        with_5xx = selector[:-1] + ', code=~"5.."}' if selector.endswith("}") else selector
        return template.replace("{selector_with_5xx}", with_5xx).replace("{range}", window)
    return template.replace("{selector}", selector).replace("{range}", window)
