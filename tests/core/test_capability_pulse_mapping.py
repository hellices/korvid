"""Pulse's coverage states, mapped onto the evidence vocabulary (#409).

Pulse keeps its own collector until the convergence follow-up, and this table
is the bridge until then. Each row is checked against what Pulse and the
classifier do with the same failure, so a change on either side fails here
until the two are reconciled.
"""

from __future__ import annotations

from typing import get_args

import pytest

from korvid.core import pulse
from korvid.core.capability import DEFAULT_MAX_AGE, ApiSource, EvidenceState, SourceScope, Verb
from korvid.core.capability_classifier import ReadKind, ReadRequest, classify
from korvid.core.pulse import PulseCoverageState
from korvid.core.pulse_collector import PulseCollector, PulseSourceResult
from korvid.k8s.errors import (
    ApiStatusError,
    KubeClientError,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTooLargeError,
    KubeTransportError,
)
from korvid.k8s.pulse import PulsePage, PulseReader, PulseSource

#: Pulse state -> (the state when the result kept objects, when it kept none).
#: An ambiguous Pulse state maps to the weaker claim: `unavailable` folds a 405
#: together with a 404 that may only mean stale discovery, and `capped` also
#: covers a byte cap hit on the first page.
PULSE_TO_EVIDENCE: dict[str, tuple[EvidenceState, EvidenceState]] = {
    "loading": (EvidenceState.UNKNOWN, EvidenceState.UNKNOWN),
    "complete": (EvidenceState.AVAILABLE, EvidenceState.AVAILABLE),
    "partial": (EvidenceState.PARTIAL, EvidenceState.UNAVAILABLE),
    "capped": (EvidenceState.PARTIAL, EvidenceState.UNAVAILABLE),
    "forbidden": (EvidenceState.DENIED, EvidenceState.DENIED),
    "unavailable": (EvidenceState.UNAVAILABLE, EvidenceState.UNAVAILABLE),
    "failed": (EvidenceState.UNAVAILABLE, EvidenceState.UNAVAILABLE),
    "stale": (EvidenceState.UNAVAILABLE, EvidenceState.UNAVAILABLE),
}

PODS = PulseSource("pods", "", "v1", "pods")
LIST = ReadRequest(
    ReadKind.LIST, ApiSource("", "pods", SourceScope.namespace("team-a"), "v1", Verb.LIST)
)
CONTINUATION = ReadRequest(LIST.kind, LIST.source, continuation=True)
FAILURES = [
    ApiStatusError(401, "Unauthorized"),
    ApiStatusError(403, "Forbidden"),
    ApiStatusError(404, "Not Found"),
    ApiStatusError(405, "Method Not Allowed"),
    ApiStatusError(406, "Not Acceptable"),
    ApiStatusError(410, "Gone"),
    ApiStatusError(429, "Too Many Requests"),
    ApiStatusError(500, "Internal Server Error"),
    ApiStatusError(400, "Bad Request"),
    KubeTimeoutError("timed out"),
    KubeTransportError("connection failed"),
    KubeMalformedError("malformed"),
    KubeClientError("not connected"),
    KubeTooLargeError("byte cap"),
]


class _Reader(PulseReader):
    def __init__(self, *outcomes: PulsePage | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, str | None, str]] = []

    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        self.calls.append((source.key, namespace, continuation))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _collect(
    *outcomes: PulsePage | Exception,
) -> tuple[PulseSourceResult, list[tuple[str, str | None, str]]]:
    reader = _Reader(*outcomes)
    (result,) = await PulseCollector(reader, (PODS,)).collect("team-a")
    return result, reader.calls


def _mapped(result: PulseSourceResult) -> EvidenceState:
    with_objects, without = PULSE_TO_EVIDENCE[result.coverage.state]
    return with_objects if result.objects else without


def test_every_pulse_state_has_a_mapping() -> None:
    assert set(PULSE_TO_EVIDENCE) == set(get_args(PulseCoverageState))


def test_the_default_maximum_age_is_pulses_staleness_window() -> None:
    assert DEFAULT_MAX_AGE == pulse._STALE_AFTER


@pytest.mark.parametrize(
    "page",
    [pytest.param(PulsePage(({},)), id="objects"), pytest.param(PulsePage(()), id="empty")],
)
async def test_a_complete_pulse_source_is_available(page: PulsePage) -> None:
    result, _calls = await _collect(page)

    assert result.coverage.state == "complete"
    assert _mapped(result) is EvidenceState.AVAILABLE


@pytest.mark.parametrize("failure", FAILURES)
async def test_a_first_page_failure_maps_to_the_classifiers_state(failure: Exception) -> None:
    result, calls = await _collect(failure)

    assert calls == [("pods", "team-a", "")]
    assert not result.objects
    assert _mapped(result) is classify(failure, LIST).state


@pytest.mark.parametrize("failure", FAILURES)
async def test_a_failure_after_kept_objects_maps_to_the_classifiers_state(
    failure: Exception,
) -> None:
    result, calls = await _collect(PulsePage(({},), "next"), failure)

    assert calls == [("pods", "team-a", ""), ("pods", "team-a", "next")]
    assert result.objects
    assert _mapped(result) is classify(failure, CONTINUATION).state
