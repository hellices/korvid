"""The connector boundary: what may be asked, what it may cost, what it says.

Transport lives in `prometheus` and `loki`; everything here is policy and
presentation, so it stays importable without the `observability` extra
and testable without a socket.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal

from korvid.core.secrets import MASK_PLACEHOLDER

#: Failure classes a caller can act on differently. Kept distinct because
#: "the endpoint is unreachable" and "your token cannot read that
#: namespace" need different responses from the person reading them.
ErrorKind = Literal["config", "auth", "permission", "network", "timeout", "limit", "backend"]

_ERROR_KINDS: frozenset[str] = frozenset(
    {"config", "auth", "permission", "network", "timeout", "limit", "backend"}
)

#: The metric questions korvid knows how to ask. A closed set: the blast
#: radius of a tool call is then a property of korvid's code rather than
#: of a model's output.
Signal = Literal["cpu", "memory", "restarts", "request_rate", "error_rate", "latency_p95"]

SIGNALS: tuple[Signal, ...] = (
    "cpu",
    "memory",
    "restarts",
    "request_rate",
    "error_rate",
    "latency_p95",
)


class ConnectorError(Exception):
    """A connector failure, carrying the class the caller should react to.

    The message names the endpoint host and what to change. It never
    carries the credential: the token is read at call time, used in a
    header, and does not travel any further.
    """

    def __init__(self, kind: str, message: str) -> None:
        if kind not in _ERROR_KINDS:
            raise ValueError(f"unknown connector error kind {kind!r}")
        super().__init__(message)
        #: One of `ErrorKind`.
        self.kind = kind


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """The cost ceiling every query passes before it leaves the process.

    Raises:
        ValueError: if any ceiling is non-positive — an unlimited limit is
            a configuration mistake, not a permissive setting.
    """

    timeout_seconds: float = 10.0
    default_window_minutes: int = 60
    max_window_minutes: int = 360
    max_series: int = 50
    max_lines: int = 200
    max_response_bytes: int = 1024 * 1024
    max_concurrency: int = 2

    def __post_init__(self) -> None:
        # `> 0` alone admits `True` and `float("inf")`, either of which
        # would mean the bounded-call contract has no bound. Checked here
        # and not only in the config parser, because a connector can be
        # built directly (PR #280 review).
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        for name in (
            "default_window_minutes",
            "max_window_minutes",
            "max_series",
            "max_lines",
            "max_response_bytes",
            "max_concurrency",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.default_window_minutes > self.max_window_minutes:
            raise ValueError("default_window_minutes must not exceed max_window_minutes")


@dataclass(frozen=True, slots=True)
class QueryScope:
    """What the question is about, in Kubernetes terms rather than labels."""

    namespace: str
    workload: str | None = None
    pod: str | None = None

    def effective(self) -> QueryScope:
        """The scope as it will actually constrain the query.

        A pod takes precedence over a workload, so a workload supplied
        alongside one constrains nothing. Normalising once, here, keeps
        every later use honest: the query, the reported scope and the set
        of configured-sensitive values all describe the same thing. Left
        un-normalised, an ignored workload named `ERROR` under a masked
        label erased `ERROR` from every returned log line (PR #280
        review).
        """
        if self.pod and self.workload:
            return QueryScope(namespace=self.namespace, pod=self.pod)
        return self

    def describe(self) -> str:
        """The scope as one header line, with every value flattened.

        Scope values are model-supplied tool arguments, and the header is
        korvid's own claim about the result. A value carrying a newline
        would add a line to the provenance block — a citation could then
        arrive with a `truncated: no` the connector never made.
        """
        parts = [f"namespace={flatten(self.namespace)}"]
        if self.workload:
            parts.append(f"workload={flatten(self.workload)}")
        if self.pod:
            parts.append(f"pod={flatten(self.pod)}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Series:
    """One labelled value: the aggregate of a signal over the window."""

    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True, slots=True)
class MetricResult:
    """A bounded metric answer that describes its own provenance and limits."""

    source: str
    endpoint: str
    signal: str
    scope: QueryScope
    window_minutes: int
    query: str
    unit: str
    series: tuple[Series, ...] = ()
    truncated: bool = False
    #: When the backend says the samples were taken, in UTC. A window is
    #: relative; without this a citation cannot be rechecked later.
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class LogLine:
    timestamp: str
    labels: Mapping[str, str]
    line: str


@dataclass(frozen=True, slots=True)
class LogResult:
    """A bounded log answer that describes its own provenance and limits."""

    source: str
    endpoint: str
    scope: QueryScope
    window_minutes: int
    query: str
    lines: tuple[LogLine, ...] = ()
    truncated: bool = False


def resolve_window(minutes: object, limits: QueryLimits) -> int:
    """The window to query, or a refusal.

    An over-long window is refused rather than clamped: silently
    shrinking it answers a different question from the one asked, and the
    caller has no way to tell.

    Raises:
        ConnectorError: `limit` for a non-integer, non-positive, or
            over-maximum window.
    """
    if minutes is None:
        return limits.default_window_minutes
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ConnectorError("limit", f"window minutes must be an integer, got {minutes!r}")
    if minutes <= 0:
        raise ConnectorError("limit", f"window minutes must be positive, got {minutes}")
    if minutes > limits.max_window_minutes:
        raise ConnectorError(
            "limit",
            f"window of {minutes} minutes exceeds the configured maximum of "
            f"{limits.max_window_minutes} minutes",
        )
    return minutes


def resolve_limit(value: object, *, maximum: int, label: str) -> int:
    """The row cap to request, or a refusal.

    Raises:
        ConnectorError: `limit` for a non-integer, non-positive, or
            over-maximum value.
    """
    if value is None:
        return maximum
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorError("limit", f"{label} must be an integer, got {value!r}")
    if value <= 0:
        raise ConnectorError("limit", f"{label} must be positive, got {value}")
    if value > maximum:
        raise ConnectorError(
            "limit", f"{label} of {value} exceeds the configured maximum of {maximum}"
        )
    return value


def masked_labels(labels: Mapping[str, str], mask: frozenset[str]) -> dict[str, str]:
    """`labels` with every configured name's value replaced.

    Applied while the result is built, so the value never reaches the
    result object at all - a projection rather than a display filter. The
    name is matched case-insensitively because a label's capitalisation is
    the log shipper's choice, not the operator's.
    """
    if not mask:
        return dict(labels)
    return {
        key: (MASK_PLACEHOLDER if key.lower() in mask else value) for key, value in labels.items()
    }


def flatten(value: str) -> str:
    """`value` on one line: every character that could end a line becomes a space.

    `str.isprintable()` is False for every line and paragraph separator
    Unicode defines, not only `\n` and `\r`, so the check is the whole
    rule rather than a list someone has to keep complete.
    """
    return "".join(char if char.isprintable() else " " for char in value)


def mask_in(text: str, secrets: Iterable[str]) -> str:
    """`text` with each configured-sensitive value replaced.

    Masking response labels is not enough: the value the operator declared
    sensitive is also the value that was *asked about*, so it appears in
    the echoed scope and in the rendered query (PR #280 review). Both are
    korvid's own text, so replacing it here is exact rather than a guess.
    """
    # Longest first, deduplicated: replacing in the order given lets a
    # short secret consume the front of a longer one and leave the rest
    # readable — `("a", "abc")` turns `abc` into `<mask>bc` (PR #280
    # review). Length order makes the result independent of the order the
    # caller happened to assemble them in.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, MASK_PLACEHOLDER)
    return text


def _labels(labels: Mapping[str, str]) -> str:
    # Flattened for the same reason the header is: a label value is the
    # backend's text, and a newline in one would forge a line of korvid's.
    return " ".join(f"{flatten(key)}={flatten(value)}" for key, value in sorted(labels.items()))


def _header(
    *,
    source: str,
    endpoint: str,
    scope: QueryScope,
    window_minutes: int,
    query: str,
    truncated: bool,
    extra: tuple[str, ...] = (),
) -> list[str]:
    return [
        f"source: {flatten(source)}",
        f"endpoint: {flatten(endpoint)}",
        f"scope: {scope.describe()}",
        f"window: {window_minutes}m",
        *extra,
        # Flattened like every other header field: PromQL escaping covers
        # `\n` and `\r`, but not the Unicode separators `str.splitlines`
        # also breaks on (PR #280 review).
        f"query: {flatten(query)}",
        f"truncated: {'yes' if truncated else 'no'}",
    ]


def _rendered(
    header_fields: dict[str, Any],
    entries: list[str],
    empty_note: str,
    *,
    truncated: bool,
    limit: int | None,
) -> str:
    """Header plus as many entries as `limit` allows, honest about the rest.

    The header is never dropped: provenance is what a citation needs, and
    entries are what it can afford to lose. If any entry is dropped the
    header says `truncated: yes`, so a result cut to fit can never read as
    complete — which is what a downstream cap left behind (PR #280
    review).
    """
    kept: list[str] = []
    dropped = False
    if limit is None:
        kept = entries
    else:
        # Rendered with truncation already assumed, so admitting an entry
        # can never push the header over the budget afterwards.
        budget = limit - len("\n".join(_header(**header_fields, truncated=True))) - 2
        for entry in entries:
            # Skipped, not stopped at: log lines vary by orders of
            # magnitude, and one early giant would otherwise discard every
            # short line after it (PR #280 review).
            if len(entry) + 1 > budget:
                dropped = True
                continue
            kept.append(entry)
            budget -= len(entry) + 1
    lines = _header(**header_fields, truncated=truncated or dropped)
    if not kept:
        lines.append(empty_note if not entries else "every entry was dropped to fit the budget")
        return "\n".join(lines)
    lines.append("")
    lines.extend(kept)
    return "\n".join(lines)


def render_metrics(result: MetricResult, *, limit: int | None = None) -> str:
    """The model-facing text for a metric answer.

    Every field a claim citing this result would need to be checked is on
    the page: which backend answered, what was asked, over what window,
    and whether the answer was capped.
    """
    return _rendered(
        {
            "source": result.source,
            "endpoint": result.endpoint,
            "scope": result.scope,
            "window_minutes": result.window_minutes,
            "query": result.query,
            "extra": (
                f"signal: {result.signal}",
                f"unit: {result.unit}",
                *((f"observed at: {result.observed_at}",) if result.observed_at else ()),
            ),
        },
        [
            f"{_labels(series.labels) or '(no labels)'}  {series.value:g} {result.unit}"
            for series in result.series
        ],
        "no series matched this scope and window",
        truncated=result.truncated,
        limit=limit,
    )


def render_logs(result: LogResult, *, limit: int | None = None) -> str:
    """The model-facing text for a log answer."""
    return _rendered(
        {
            "source": result.source,
            "endpoint": result.endpoint,
            "scope": result.scope,
            "window_minutes": result.window_minutes,
            "query": result.query,
            "extra": (f"lines: {len(result.lines)}",),
        },
        [
            f"{f'{entry.timestamp} {_labels(entry.labels)}'.rstrip()}  {entry.line}"
            for entry in result.lines
        ],
        "no log lines matched this scope and window",
        truncated=result.truncated,
        limit=limit,
    )


class MetricsConnector(ABC):
    """A bounded, read-only source of metric signals.

    Implementations must not raise anything but `ConnectorError`: the
    caller turns that into one actionable sentence, and an unexpected
    exception type would reach the model as a stack-shaped string.
    """

    #: Stable identifier used in results and in the tool's error text.
    source: str

    @abstractmethod
    async def query(
        self, *, signal: str, scope: QueryScope, window_minutes: object = None
    ) -> MetricResult: ...

    @abstractmethod
    async def aclose(self) -> None: ...


class LogsConnector(ABC):
    """A bounded, read-only source of centralized log lines."""

    source: str

    @abstractmethod
    async def search(
        self,
        *,
        scope: QueryScope,
        window_minutes: object = None,
        contains: str | None = None,
        limit: object = None,
    ) -> LogResult: ...

    @abstractmethod
    async def aclose(self) -> None: ...
