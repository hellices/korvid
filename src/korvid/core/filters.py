"""Filter expression parsing + matching for resource tables (issue #44).

Pure functions/dataclasses — no Textual imports. The filter bar text is a
sequence of space-separated tokens, AND-combined:

- plain text — case-insensitive substring on the resource name (unchanged
  legacy behavior)
- `~pattern` — fuzzy subsequence match on the name
- `/pattern/` or `re:pattern` — regex on the name; an invalid regex sets
  `error` and the token is ignored (the filter never raises)
- `!<token>` — negates any of the name forms above
- `-l key=value[,k2=v2]` — label selector (equality; a bare key tests
  existence), matched client-side against `metadata.labels`
- `-s` — hide rows whose phase is `Succeeded`/`Completed` (finished Jobs
  burying live workloads)
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

#: Phases hidden by the `-s` token (pod views only; rows without a phase pass).
_COMPLETED_PHASES = frozenset({"Succeeded", "Completed"})

_NamePredicate = Callable[[str], bool]


def _fuzzy(pattern: str) -> _NamePredicate:
    """Subsequence match: every pattern char appears in order in the name."""
    lowered = pattern.lower()

    def match(name: str) -> bool:
        it = iter(name.lower())
        return all(ch in it for ch in lowered)

    return match


def _substring(pattern: str) -> _NamePredicate:
    lowered = pattern.lower()
    return lambda name: lowered in name.lower()


@dataclass(frozen=True)
class ResourceFilter:
    """A parsed filter expression; build with `parse_filter`."""

    text: str
    #: Human-readable parse problem (invalid regex, `-l` without selector);
    #: broken tokens are ignored so matching never raises.
    error: str | None = None
    _name_predicates: tuple[_NamePredicate, ...] = ()
    _label_selector: tuple[tuple[str, str | None], ...] = ()
    _hide_completed: bool = False
    _parts: tuple[str, ...] = field(default=())

    @property
    def active(self) -> bool:
        """True when any predicate (or a parse error worth surfacing) exists."""
        return bool(
            self._name_predicates or self._label_selector or self._hide_completed or self.error
        )

    def matches(
        self,
        name: str,
        labels: Mapping[str, str] | None = None,
        phase: str | None = None,
    ) -> bool:
        """Whether a row passes every token of the filter.

        Args:
            name: Resource name.
            labels: The row's `metadata.labels`; None when unknown.
            phase: Pod phase/display status; None for kinds without one.
        """
        if not all(predicate(name) for predicate in self._name_predicates):
            return False
        if self._label_selector:
            actual = labels or {}
            for key, expected in self._label_selector:
                if key not in actual:
                    return False
                if expected is not None and actual[key] != expected:
                    return False
        return not (self._hide_completed and phase in _COMPLETED_PHASES)

    def describe(self) -> str:
        """Short indicator text for the table header ('' when inactive)."""
        parts = list(self._parts)
        if self.error:
            parts.append(self.error)
        return " · ".join(parts)


def _parse_label_selector(
    selector: str,
) -> tuple[tuple[tuple[str, str | None], ...], str | None]:
    pairs: list[tuple[str, str | None]] = []
    for part in selector.split(","):
        part = part.strip()
        if not part:
            return (), f"empty term in label selector {selector!r}"
        key, eq, value = part.partition("=")
        if not key:
            return (), f"empty key in label selector {selector!r}"
        pairs.append((key, value if eq else None))
    return tuple(pairs), None


def _parse_name_token(token: str) -> tuple[_NamePredicate | None, str | None, str]:
    """One name-matching token → (predicate, error, description)."""
    if token.startswith("~"):
        if len(token) == 1:
            # An empty fuzzy pattern matches every name; negated it would
            # silently hide every row — reject the half-typed token instead.
            return None, "missing pattern after '~'", ""
        return _fuzzy(token[1:]), None, f"~{token[1:]}"
    pattern: str | None = None
    if token.startswith("/"):
        # Any slash-prefixed token is regex syntax; resource names cannot
        # contain '/', so falling back to substring would empty the table.
        if len(token) < 2 or not token.endswith("/"):
            return None, f"unterminated regex {token!r}", ""
        pattern = token[1:-1]
    elif token.startswith("re:"):
        pattern = token[3:]
    if pattern is not None:
        if not pattern:
            return None, "empty regex pattern", ""
        try:
            compiled = re.compile(pattern)
        except re.error:
            return None, f"invalid regex {pattern!r}", ""
        return (lambda name: compiled.search(name) is not None), None, f"/{pattern}/"
    return _substring(token), None, token


def _add_name_token(
    token: str,
    predicates: list[_NamePredicate],
    parts: list[str],
) -> str | None:
    """Parse one name token into `predicates`/`parts`; returns an error or None."""
    negated = token.startswith("!")
    if negated and len(token) == 1:
        # A dangling `!` would negate the match-all empty substring and
        # silently hide every row; surface it as a parse error instead.
        return "missing pattern after '!'"
    predicate, tok_error, description = _parse_name_token(token[1:] if negated else token)
    if tok_error is not None:
        return tok_error
    if predicate is None:
        return None
    if negated:

        def negate(name: str, _inner: _NamePredicate = predicate) -> bool:
            return not _inner(name)

        predicates.append(negate)
        parts.append(f"!{description}")
    else:
        predicates.append(predicate)
        parts.append(description)
    return None


def parse_filter(text: str) -> ResourceFilter:
    """Parse the filter bar text into a `ResourceFilter`.

    Broken tokens (invalid regex, dangling `-l` or `!`) set `error` and are
    skipped so a half-typed expression can never crash the render path.
    """
    predicates: list[_NamePredicate] = []
    selector_pairs: list[tuple[str, str | None]] = []
    hide_completed = False
    error: str | None = None
    parts: list[str] = []

    tokens = text.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-l":
            if i + 1 >= len(tokens) or tokens[i + 1] in ("-l", "-s"):
                # A following option token is not a selector — report the
                # missing argument and let the option parse on its own.
                error = "label selector missing after -l"
                i += 1
                continue
            pairs, sel_error = _parse_label_selector(tokens[i + 1])
            if sel_error is not None:
                error = sel_error
            else:
                selector_pairs.extend(pairs)
                parts.append(f"-l {tokens[i + 1]}")
            i += 2
            continue
        if token == "-s":
            hide_completed = True
            parts.append("hide-completed")
            i += 1
            continue
        tok_error = _add_name_token(token, predicates, parts)
        if tok_error is not None:
            error = tok_error
        i += 1

    return ResourceFilter(
        text=text,
        error=error,
        _name_predicates=tuple(predicates),
        _label_selector=tuple(selector_pairs),
        _hide_completed=hide_completed,
        _parts=tuple(parts),
    )
