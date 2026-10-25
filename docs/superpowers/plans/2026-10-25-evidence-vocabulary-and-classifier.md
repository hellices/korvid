# Evidence Vocabulary and Read-Outcome Classifier (#409 S1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every capability-aware diagnostic three shared pieces:
- a vocabulary for what korvid knows about each evidence source;
- a rule for when absence counts as proof;
- a classifier for failed reads.

This is #490, slice S1 of #409.

**Architecture:** Two new pure modules in `core/`:
- `capability.py`: states, bases, sources, observations, freshness, declarations and combining rules;
- `capability_classifier.py`: read requests, bounded `Status` parsing and classification.

The k8s layer gains `KubeClientError` subclasses that name a failure's category. Each raise site picks the subclass, so classification never parses messages. Pulse is untouched. A test pins how its coverage states map onto the vocabulary.

**Tech Stack:** Python 3.11+ standard library (`dataclasses`, `enum.StrEnum`, `json`), pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md` (merged in #485). Where this plan departs from it, the section below says why.
- **Layers:**
  - `core` may import only `k8s` and `option_keys`;
  - `k8s` imports no other korvid layer;
  - nothing new imports Textual or an optional extra.
- **`client.py` cap:** `scripts/check_source_size.py` caps `src/korvid/k8s/client.py` at 1,944 lines. It has 1,942 today. Do not raise the cap.
- **Code rules:**
  - no `assert` in `src/` (ruff S101);
  - every `pytest.raises` takes `match=`;
  - maximum complexity 10;
  - Google-style docstrings;
  - `from __future__ import annotations` in every new module.
- **`detail` text:** at most 200 characters, from fixed templates and resource identities, never from response bodies or messages.
- **Error bodies:** the classifier parses at most 16 KiB (`16 * 1024` UTF-8 bytes).
- **Default maximum age:** 30 seconds, Pulse's staleness window (`korvid.core.pulse._STALE_AFTER`).
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **The classifier lives in its own module,** `korvid.core.capability_classifier`, next to the vocabulary. This follows the `pulse`/`pulse_collector` pattern. `capability.py` stays standard-library only, and `capability_classifier.py` adds `korvid.k8s.errors`. Task 4 updates the design's architecture table to match.
- **`ReadKind` has no `PROVIDER` member.** The design's classification table has no provider column, and provider collectors wait for a consumer (decision 7).
- **Categories are subclasses.** `KubeTimeoutError`, `KubeTransportError`, `KubeMalformedError` and `KubeTooLargeError` each fix the class attribute `category`. Choosing the subclass at the raise site sets the category there. Merging `_request_dict`'s two identical "connection failed" branches keeps `client.py` within its cap.
- **An uncategorized `KubeClientError` is classified as `TRANSPORT`.** Examples are a client that is not connected, or a request that could not be built. In each case the request produced no answer.
- **Pulse's own raise sites (`k8s/pulse.py`) stay uncategorized until S3 (#487).** S3 generalizes that bounded transport. Its request-attempt cap is a transport failure, not a size limit, and that decision belongs with the generalization.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/k8s/errors.py` | modify | `KubeErrorCategory` and the four categorized `KubeClientError` subclasses |
| `src/korvid/k8s/client.py` | modify | categorized errors on the shared request and discovery path |
| `src/korvid/k8s/deployment_outcomes.py` | modify | `KubeMalformedError` for invalid LIST pages and repeated continuations |
| `src/korvid/core/capability.py` | create | the vocabulary and the combining rules |
| `src/korvid/core/capability_classifier.py` | create | `ReadRequest`, `parse_status`, `classify` |
| `tests/k8s/test_error_categories.py` | create | every categorized raise site |
| `tests/core/test_capability.py` | create | states, bases, sources, observations, freshness, declarations |
| `tests/core/test_capability_baseline.py` | create | `EvidenceSet.baseline`, `by_scope` |
| `tests/core/test_capability_classifier.py` | create | the classification table and `Status` parsing |
| `tests/core/test_capability_pulse_mapping.py` | create | Pulse's coverage states against the vocabulary |
| `tests/test_optional_extras.py` | modify | the new modules import without extras |
| `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md` | modify | the architecture table names `capability_classifier` |

---

### Task 1: Categorize Kubernetes client failures

**Files:**
- Modify: `src/korvid/k8s/errors.py`
- Modify: `src/korvid/k8s/client.py`: the import block, plus `_request_discovery_json`, `discover_resources`, `_request_dict`, `_response_dict`, `_malformed_response_error` and `_parse_resource_list`
- Modify: `src/korvid/k8s/deployment_outcomes.py`: `parse_list_page` and `iter_raw_object_pages`
- Test: `tests/k8s/test_error_categories.py`

**Interfaces:**
- Produces:
  - `KubeErrorCategory = Literal["timeout", "transport", "malformed", "too_large"]`;
  - `KubeClientError.category: ClassVar[KubeErrorCategory | None]`, which is `None` on the base class;
  - the subclasses `KubeTimeoutError`, `KubeTransportError`, `KubeMalformedError` and `KubeTooLargeError`, whose `category` is `"timeout"`, `"transport"`, `"malformed"` and `"too_large"`.

- [ ] **Step 1: Write the failing test**

Create `tests/k8s/test_error_categories.py`:

```python
"""Each raise site on the shared request path names its failure category (#409).

The evidence classifier maps a `KubeClientError` by its `category` and never
parses messages, so every site that knows what went wrong has to say so.
"""

from __future__ import annotations

import ssl
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError, InvalidURL

from korvid.k8s.client import KubeClient
from korvid.k8s.deployment_outcomes import iter_raw_object_pages, parse_list_page
from korvid.k8s.discovery import PODS_META
from korvid.k8s.errors import (
    KubeClientError,
    KubeErrorCategory,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTooLargeError,
    KubeTransportError,
)


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (KubeClientError, None),
        (KubeTimeoutError, "timeout"),
        (KubeTransportError, "transport"),
        (KubeMalformedError, "malformed"),
        (KubeTooLargeError, "too_large"),
    ],
)
def test_each_error_class_names_one_category(
    error_type: type[KubeClientError], category: KubeErrorCategory | None
) -> None:
    error = error_type("Kubernetes API request failed")

    assert isinstance(error, KubeClientError)
    assert error.category == category
    assert str(error) == "Kubernetes API request failed"


def _client(*, failure: BaseException | None = None, body: bytes = b"{}") -> KubeClient:
    response = AsyncMock()
    response.status = 200
    response.reason = "OK"
    response.read.return_value = body
    api = AsyncMock()
    api.call_api.return_value = response
    api.call_api.side_effect = failure
    client = KubeClient()
    client._api = api
    return client


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        pytest.param(TimeoutError(), "timeout", id="timeout"),
        pytest.param(ssl.SSLError("certificate"), "transport", id="tls"),
        pytest.param(ClientConnectionError("refused"), "transport", id="aiohttp"),
        pytest.param(ConnectionResetError("reset"), "transport", id="socket"),
        pytest.param(InvalidURL("https://cluster.invalid"), "transport", id="invalid-url"),
        pytest.param(ValueError("header"), None, id="client-configuration"),
    ],
)
async def test_request_failures_name_their_category(
    failure: BaseException, category: KubeErrorCategory | None
) -> None:
    with pytest.raises(KubeClientError, match="Kubernetes API") as excinfo:
        await _client(failure=failure)._request_json("/api/v1")

    assert excinfo.value.category == category


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"token":"secret"', id="invalid-json"),
        pytest.param(b'["not", "an", "object"]', id="not-an-object"),
    ],
)
async def test_an_unusable_response_is_malformed(body: bytes) -> None:
    with pytest.raises(KubeMalformedError, match="malformed"):
        await _client(body=body)._request_json("/api/v1")


async def test_a_discovery_timeout_is_a_timeout() -> None:
    client = KubeClient()

    with (
        patch.object(client, "_request_json", AsyncMock(side_effect=TimeoutError())),
        pytest.raises(KubeTimeoutError, match="discovery timed out"),
    ):
        await client.discover_resources()


@pytest.mark.parametrize(
    ("documents", "message"),
    [
        pytest.param(
            {"/api/v1": {"resources": []}, "/apis": {"groups": "invalid"}},
            "invalid group list",
            id="group-list",
        ),
        pytest.param(
            {"/api/v1": {"resources": "invalid"}}, "invalid resource list", id="resource-list"
        ),
    ],
)
async def test_an_invalid_discovery_document_is_malformed(
    documents: dict[str, dict[str, Any]], message: str
) -> None:
    client = KubeClient()

    async def answer(path: str) -> dict[str, Any]:
        return documents[path]

    with (
        patch.object(client, "_request_json", AsyncMock(side_effect=answer)),
        pytest.raises(KubeMalformedError, match=message),
    ):
        await client.discover_resources()


def test_an_invalid_list_page_is_malformed() -> None:
    with pytest.raises(KubeMalformedError, match="malformed response"):
        parse_list_page({"items": "invalid"})


async def test_a_repeated_continuation_is_malformed() -> None:
    request = AsyncMock(return_value={"items": [], "metadata": {"continue": "same"}})
    pages = iter_raw_object_pages(
        PODS_META,
        "default",
        page_size=5,
        list_path=lambda meta, namespace: "/api/v1/namespaces/default/pods",
        request_json=request,
        observe=lambda *args, **kwargs: None,
        observe_error=lambda path, error: None,
    )

    with pytest.raises(KubeMalformedError, match="continuation did not advance"):
        await anext(pages)

    assert [call.kwargs["query_params"] for call in request.await_args_list] == [
        [("limit", "5")],
        [("limit", "5"), ("continue", "same")],
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_error_categories.py -q`
Expected: a collection error, `ImportError: cannot import name 'KubeErrorCategory'`.

- [ ] **Step 3: Add the categories**

Replace the contents of `src/korvid/k8s/errors.py` with:

```python
"""korvid exceptions normalizing Kubernetes client failures (k8s-layer only).

Callers import these types instead of third-party transport/API exceptions so
the kubernetes_asyncio implementation never leaks past the k8s layer.
"""

from __future__ import annotations

from typing import ClassVar, Literal

#: What went wrong with a request that produced no usable HTTP answer, so that
#: a caller can classify the failure without parsing its message.
KubeErrorCategory = Literal["timeout", "transport", "malformed", "too_large"]


class KubeClientError(Exception):
    """Raised when a Kubernetes request fails without an HTTP API status.

    Raise the subclass that names what went wrong, so that `category` says it.
    The base class leaves `category` as `None`: the request produced no
    usable answer for a reason no subclass describes, such as a client that
    was never connected.
    """

    category: ClassVar[KubeErrorCategory | None] = None


class KubeTimeoutError(KubeClientError):
    """A Kubernetes request did not finish before its deadline."""

    category: ClassVar[KubeErrorCategory | None] = "timeout"


class KubeTransportError(KubeClientError):
    """A Kubernetes request failed below HTTP: the connection, TLS or socket."""

    category: ClassVar[KubeErrorCategory | None] = "transport"


class KubeMalformedError(KubeClientError):
    """A Kubernetes response could not be decoded, or had the wrong shape."""

    category: ClassVar[KubeErrorCategory | None] = "malformed"


class KubeTooLargeError(KubeClientError):
    """A Kubernetes response exceeded its byte or object budget."""

    category: ClassVar[KubeErrorCategory | None] = "too_large"


class ApiStatusError(Exception):
    """Raised by the k8s layer when an API request returns an HTTP error status."""

    def __init__(
        self,
        status: int,
        reason: str,
        body: str = "",
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"API {status}: {reason}")
        self.status = status
        self.reason = reason
        #: Raw response body (a Kubernetes ``Status`` JSON when available):
        #: callers that must tell apart same-status responses - e.g. an
        #: eviction's PDB denial vs API Priority and Fairness throttling,
        #: both 429 - inspect it.
        self.body = body
        self.retry_after_seconds: float | None = retry_after_seconds
```

- [ ] **Step 4: Run the test to verify the raise sites still fail**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_error_categories.py -q`
Expected:
- `test_each_error_class_names_one_category` passes;
- the raise-site tests fail, with `assert None == 'timeout'` or with a plain `KubeClientError` escaping `pytest.raises(KubeMalformedError)`.

- [ ] **Step 5: Raise the categorized errors**

In `src/korvid/k8s/client.py`:

1. Replace the import `from korvid.k8s.errors import ApiStatusError, KubeClientError` with:

```python
from korvid.k8s.errors import (
    ApiStatusError,
    KubeClientError,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTransportError,
)
```

2. In `_request_discovery_json`, raise `KubeTimeoutError(` in place of `KubeClientError(`. The message is unchanged ("Kubernetes API discovery timed out; …").
3. In `discover_resources`, raise `KubeMalformedError("Kubernetes API discovery returned an invalid group list")`.
4. In `_request_dict`, replace the four transport branches with three. The last two raised the same error, so they merge:

```python
    except TimeoutError:
        raise KubeTimeoutError(
            "Kubernetes API request timed out; check cluster connectivity and retry"
        ) from None
    except ssl.SSLError:
        raise KubeTransportError(
            "Kubernetes API TLS validation failed; check cluster certificates and retry"
        ) from None
    except (_AIOHTTP_CLIENT_ERROR, OSError):
        raise KubeTransportError(
            "Kubernetes API connection failed; check cluster connectivity and retry"
        ) from None
```

   Leave the `(ValueError, RecursionError)` branch raising `KubeClientError`. A request that could not be built has no category.
5. In `_response_dict`, raise `KubeMalformedError(` for "Kubernetes API returned malformed JSON; …".
6. Change `_malformed_response_error` to:

```python
def _malformed_response_error() -> KubeMalformedError:
    return KubeMalformedError(
        "Kubernetes API returned a malformed response; retry, then check the API server"
    )
```

7. In `_parse_resource_list`, raise `KubeMalformedError("Kubernetes API discovery returned an invalid resource list")`.

In `src/korvid/k8s/deployment_outcomes.py`:
- import `from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeMalformedError`;
- in `parse_list_page`, raise `KubeMalformedError(` for the malformed-response message;
- in `iter_raw_object_pages`, raise `KubeMalformedError("LIST continuation did not advance")`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_error_categories.py tests/k8s/test_discovery.py tests/k8s/test_discovery_versions.py tests/k8s/test_deployment_outcomes.py tests/k8s/test_list_paging.py -q`
Expected: all pass.

Run: `uv run --frozen python scripts/check_source_size.py`
Expected: exit 0, with `client.py` at 1,944 lines.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/k8s/errors.py src/korvid/k8s/client.py src/korvid/k8s/deployment_outcomes.py tests/k8s/test_error_categories.py
git commit -m "k8s: name the category of each Kubernetes client failure"
```

### Task 2: The evidence vocabulary

**Files:**
- Create: `src/korvid/core/capability.py`
- Test: `tests/core/test_capability.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces, all in `korvid.core.capability`:
  - `EvidenceState` (`UNKNOWN`, `AVAILABLE`, `PARTIAL`, `UNSUPPORTED`, `DENIED`, `UNAVAILABLE`);
  - `Basis` and `ALLOWED_BASES: Mapping[EvidenceState, frozenset[Basis]]`;
  - `ScopeKind` and `SourceScope(kind, name="")`, with the constructors `cluster()`, `namespace(name)`, `node(name)` and `provider(provider_id)`;
  - `Verb` (`LIST`, `GET`) and `Projection` (`OBJECT`, `METADATA`);
  - `ApiSource(group, resource, scope, version=None, verb=None, subresource="", projection=Projection.OBJECT, name="", expected_uid=None)`, with `.resolved`;
  - `DiscoveryEndpoint` and `DiscoverySource(endpoint, group="", version="")`, with `core_root()`, `group_root()`, `group_version(group, version)`, `.path` and `.scope`;
  - `ProviderSource(provider_id, query_kind, scope)`;
  - `Source = ApiSource | DiscoverySource | ProviderSource`;
  - `Role` (`RULES_OUT`, `CONFIRMS`);
  - `DEFAULT_MAX_AGE`, `FreshnessPolicy(max_age=DEFAULT_MAX_AGE, retention_limited=False)` and `DEFAULT_POLICY`;
  - `Declaration(key, required, role, policy=DEFAULT_POLICY)`;
  - `RunToken(epoch, session_generation, run_generation)`;
  - `Observation(source, state, basis, observed_at=None, items=0, observed_uid=None, detail="", token=None)`, with `.recreated`;
  - `is_fresh(observation, policy, now) -> bool` and `conclusive(observation, policy, now) -> bool`;
  - `downgrade_stale_status(observation, detail) -> Observation`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_capability.py`:

```python
"""The evidence vocabulary (#409): states, bases, sources and observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from korvid.core.capability import (
    ALLOWED_BASES,
    DEFAULT_MAX_AGE,
    ApiSource,
    Basis,
    Declaration,
    DiscoveryEndpoint,
    DiscoverySource,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    Projection,
    ProviderSource,
    Role,
    RunToken,
    ScopeKind,
    Source,
    SourceScope,
    Verb,
    conclusive,
    downgrade_stale_status,
    is_fresh,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
PODS = ApiSource("", "pods", TEAM)
CONFIG = ApiSource(
    "",
    "configmaps",
    TEAM,
    "v1",
    Verb.GET,
    projection=Projection.METADATA,
    name="app",
    expected_uid="uid-1",
)

#: The design's table of the bases each state allows, copied here so that a
#: change to either side fails this test.
EXPECTED_BASES = {
    EvidenceState.UNKNOWN: {Basis.NOT_STARTED, Basis.IN_FLIGHT},
    EvidenceState.AVAILABLE: {Basis.OBSERVED, Basis.OBJECT_ABSENT, Basis.NO_DATA},
    EvidenceState.PARTIAL: {
        Basis.TRUNCATED,
        Basis.DEADLINE,
        Basis.TOO_LARGE,
        Basis.CONTINUATION_EXPIRED,
        Basis.SUBSCOPE_FAILED,
        Basis.STALE_STATUS,
        # A read failure after an accepted page keeps its own basis.
        Basis.FORBIDDEN,
        Basis.UNAUTHENTICATED,
        Basis.THROTTLED,
        Basis.SERVER_ERROR,
        Basis.UNEXPECTED_STATUS,
        Basis.TRANSPORT,
        Basis.MALFORMED,
        Basis.DISCOVERY_STALE,
        Basis.PROJECTION_UNSUPPORTED,
    },
    EvidenceState.UNSUPPORTED: {
        Basis.NOT_SERVED,
        Basis.OPERATION_UNSUPPORTED,
        Basis.NO_KNOWN_VERSION,
        Basis.PROJECTION_UNSUPPORTED,
        Basis.PROVIDER_NOT_CONFIGURED,
        Basis.FEATURE_DISABLED,
    },
    EvidenceState.DENIED: {Basis.FORBIDDEN, Basis.UNAUTHENTICATED, Basis.DISCOVERY_FORBIDDEN},
    EvidenceState.UNAVAILABLE: {
        Basis.THROTTLED,
        Basis.SERVER_ERROR,
        Basis.UNEXPECTED_STATUS,
        Basis.TRANSPORT,
        Basis.MALFORMED,
        Basis.DEADLINE,
        Basis.TOO_LARGE,
        Basis.DISCOVERY_FAILED,
        Basis.DISCOVERY_STALE,
    },
}
ALLOWED_PAIRS = [
    (state, basis) for state, bases in EXPECTED_BASES.items() for basis in sorted(bases)
]
REFUSED_PAIRS = [
    (state, basis)
    for state in EvidenceState
    for basis in Basis
    if basis not in EXPECTED_BASES[state]
]


def _observation(
    state: EvidenceState = EvidenceState.AVAILABLE,
    basis: Basis = Basis.OBSERVED,
    *,
    at: datetime = NOW,
    source: Source = PODS,
    **fields: Any,
) -> Observation:
    observed_at = None if state is EvidenceState.UNKNOWN else at
    return Observation(source, state, basis, observed_at, **fields)


def test_the_allowed_bases_match_the_design() -> None:
    assert {state: set(bases) for state, bases in ALLOWED_BASES.items()} == EXPECTED_BASES


@pytest.mark.parametrize(("state", "basis"), ALLOWED_PAIRS)
def test_an_observation_accepts_each_allowed_basis(state: EvidenceState, basis: Basis) -> None:
    assert _observation(state, basis).basis is basis


@pytest.mark.parametrize(("state", "basis"), REFUSED_PAIRS)
def test_an_observation_refuses_every_other_basis(state: EvidenceState, basis: Basis) -> None:
    with pytest.raises(ValueError, match="cannot rest on"):
        _observation(state, basis)


def test_an_unknown_observation_has_no_time() -> None:
    with pytest.raises(ValueError, match="time exactly when"):
        Observation(PODS, EvidenceState.UNKNOWN, Basis.NOT_STARTED, NOW)


def test_a_known_observation_needs_a_time() -> None:
    with pytest.raises(ValueError, match="time exactly when"):
        Observation(PODS, EvidenceState.AVAILABLE, Basis.OBSERVED)


@pytest.mark.parametrize(
    "observed_at",
    [
        pytest.param(datetime(2026, 10, 25, 12, 0), id="naive"),
        pytest.param(datetime(2026, 10, 25, 21, 0, tzinfo=timezone(timedelta(hours=9))), id="kst"),
    ],
)
def test_observation_times_are_utc(observed_at: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        Observation(PODS, EvidenceState.AVAILABLE, Basis.OBSERVED, observed_at)


@pytest.mark.parametrize("items", [-1, True, 1.5, "3"])
def test_an_item_count_is_a_non_negative_integer(items: Any) -> None:
    with pytest.raises(ValueError, match="item count"):
        _observation(items=items)


def test_a_long_detail_is_shortened_to_200_characters() -> None:
    observation = _observation(detail="x" * 500)

    assert len(observation.detail) == 200
    assert observation.detail.endswith("…")


def test_a_different_uid_marks_a_recreated_object() -> None:
    assert _observation(source=CONFIG, observed_uid="uid-2").recreated
    assert not _observation(source=CONFIG, observed_uid="uid-1").recreated
    assert not _observation(source=CONFIG, basis=Basis.OBJECT_ABSENT).recreated
    assert not _observation(observed_uid="uid-2").recreated


def test_an_observed_object_with_an_expected_uid_records_the_uid_it_saw() -> None:
    with pytest.raises(ValueError, match="records the UID it saw"):
        _observation(source=CONFIG)


def test_an_observation_carries_the_token_its_read_started_under() -> None:
    token = RunToken(epoch=3, session_generation=1, run_generation=7)

    assert _observation(token=token).token == token
    assert RunToken(epoch=3, session_generation=1, run_generation=8) != token


@pytest.mark.parametrize(
    ("state", "basis", "expected"),
    [
        (EvidenceState.AVAILABLE, Basis.OBSERVED, True),
        (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT, True),
        (EvidenceState.AVAILABLE, Basis.NO_DATA, False),
        (EvidenceState.PARTIAL, Basis.TRUNCATED, False),
        (EvidenceState.DENIED, Basis.FORBIDDEN, False),
        (EvidenceState.UNKNOWN, Basis.NOT_STARTED, False),
    ],
)
def test_only_observed_available_evidence_is_conclusive(
    state: EvidenceState, basis: Basis, expected: bool
) -> None:
    assert conclusive(_observation(state, basis), FreshnessPolicy(), NOW) is expected


def test_evidence_older_than_the_maximum_age_is_not_conclusive() -> None:
    policy = FreshnessPolicy()

    assert conclusive(_observation(at=NOW - DEFAULT_MAX_AGE), policy, NOW)
    assert not conclusive(
        _observation(at=NOW - DEFAULT_MAX_AGE - timedelta(seconds=1)), policy, NOW
    )


def test_each_source_keeps_its_own_maximum_age() -> None:
    observation = _observation(at=NOW - timedelta(minutes=2))

    assert not conclusive(observation, FreshnessPolicy(), NOW)
    assert conclusive(observation, FreshnessPolicy(max_age=timedelta(minutes=5)), NOW)


def test_fresh_events_are_never_conclusive_about_absence() -> None:
    events = ApiSource("events.k8s.io", "events", TEAM)
    observation = _observation(source=events)
    retention = FreshnessPolicy(retention_limited=True)

    assert is_fresh(observation, retention, NOW)
    assert not conclusive(observation, retention, NOW)


def test_an_observation_timed_after_now_is_neither_fresh_nor_conclusive() -> None:
    observation = _observation(at=NOW + timedelta(seconds=1))

    assert not is_fresh(observation, FreshnessPolicy(), NOW)
    assert not conclusive(observation, FreshnessPolicy(), NOW)


def test_a_recreated_object_is_never_conclusive() -> None:
    recreated = _observation(source=CONFIG, observed_uid="uid-2")

    assert recreated.recreated
    assert not conclusive(recreated, FreshnessPolicy(), NOW)
    assert conclusive(_observation(source=CONFIG, observed_uid="uid-1"), FreshnessPolicy(), NOW)


def test_a_stale_status_downgrade_is_partial_and_never_conclusive() -> None:
    observation = downgrade_stale_status(_observation(), "status.hard lags spec.hard")

    assert (observation.state, observation.basis) == (EvidenceState.PARTIAL, Basis.STALE_STATUS)
    assert observation.detail == "status.hard lags spec.hard"
    assert not conclusive(observation, FreshnessPolicy(), NOW)


def test_only_available_evidence_can_be_downgraded() -> None:
    with pytest.raises(ValueError, match="only available evidence"):
        downgrade_stale_status(_observation(EvidenceState.PARTIAL, Basis.TRUNCATED), "lags")


@pytest.mark.parametrize("max_age", [timedelta(0), timedelta(seconds=-1)])
def test_a_maximum_age_is_positive(max_age: timedelta) -> None:
    with pytest.raises(ValueError, match="positive"):
        FreshnessPolicy(max_age=max_age)


def test_a_retention_limited_source_can_never_rule_anything_out() -> None:
    with pytest.raises(ValueError, match="never rule one out"):
        Declaration(
            "events",
            required=True,
            role=Role.RULES_OUT,
            policy=FreshnessPolicy(retention_limited=True),
        )


def test_a_retention_limited_source_can_confirm() -> None:
    declaration = Declaration(
        "events", required=True, role=Role.CONFIRMS, policy=FreshnessPolicy(retention_limited=True)
    )

    assert declaration.policy.retention_limited


def test_a_declaration_has_a_key() -> None:
    with pytest.raises(ValueError, match="has a key"):
        Declaration("", required=True, role=Role.RULES_OUT)


@pytest.mark.parametrize(
    ("scope", "kind", "name"),
    [
        (SourceScope.cluster(), ScopeKind.CLUSTER, ""),
        (SourceScope.namespace("team-a"), ScopeKind.NAMESPACE, "team-a"),
        (SourceScope.node("node-1"), ScopeKind.NODE, "node-1"),
        (SourceScope.provider("prometheus"), ScopeKind.PROVIDER, "prometheus"),
    ],
)
def test_scope_constructors(scope: SourceScope, kind: ScopeKind, name: str) -> None:
    assert (scope.kind, scope.name) == (kind, name)


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (ScopeKind.CLUSTER, "everything"),
        (ScopeKind.NAMESPACE, ""),
        (ScopeKind.NODE, ""),
        (ScopeKind.PROVIDER, ""),
    ],
)
def test_only_a_cluster_scope_is_unnamed(kind: ScopeKind, name: str) -> None:
    with pytest.raises(ValueError, match="cluster scope has no name"):
        SourceScope(kind, name)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"resource": ""}, "names a resource"),
        ({"version": "v1"}, "both a version and a verb"),
        ({"verb": Verb.LIST}, "both a version and a verb"),
        ({"version": "v1", "verb": Verb.LIST, "name": "app"}, "LIST source names no object"),
        ({"version": "v1", "verb": Verb.GET}, "GET source names its object"),
        ({"expected_uid": "uid-1"}, "expected UID needs an object name"),
    ],
)
def test_api_sources_are_well_formed(fields: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {"group": "", "resource": "configmaps", "scope": TEAM, **fields}

    with pytest.raises(ValueError, match=message):
        ApiSource(**arguments)


def test_a_resolved_source_names_its_version_and_verb() -> None:
    assert not PODS.resolved
    assert ApiSource("", "pods", TEAM, "v1", Verb.LIST).resolved


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (DiscoverySource.core_root(), "/api"),
        (DiscoverySource.group_root(), "/apis"),
        (DiscoverySource.group_version("", "v1"), "/api/v1"),
        (DiscoverySource.group_version("apps", "v1"), "/apis/apps/v1"),
    ],
)
def test_a_discovery_source_names_one_cluster_wide_endpoint(
    source: DiscoverySource, path: str
) -> None:
    assert source.path == path
    assert source.scope == SourceScope.cluster()


@pytest.mark.parametrize(
    "fields",
    [
        {"endpoint": DiscoveryEndpoint.GROUP_VERSION},
        {"endpoint": DiscoveryEndpoint.CORE_ROOT, "version": "v1"},
        {"endpoint": DiscoveryEndpoint.GROUP_ROOT, "group": "apps"},
    ],
)
def test_discovery_sources_are_well_formed(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="discovery"):
        DiscoverySource(**fields)


@pytest.mark.parametrize(("provider_id", "query_kind"), [("", "series"), ("prometheus", "")])
def test_a_provider_source_names_its_provider_and_query(provider_id: str, query_kind: str) -> None:
    with pytest.raises(ValueError, match="provider and query kind"):
        ProviderSource(provider_id, query_kind, SourceScope.provider("prometheus"))


def test_a_provider_observation_is_read_at_the_provider_scope() -> None:
    source = ProviderSource("prometheus", "series", SourceScope.provider("prometheus"))

    assert _observation(source=source).source.scope.kind is ScopeKind.PROVIDER
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'korvid.core.capability'`.

- [ ] **Step 3: Write the vocabulary**

Create `src/korvid/core/capability.py`:

```python
"""The evidence vocabulary for capability-aware diagnostics (#409).

A diagnostic reads evidence that a cluster may not serve, may refuse to this
principal, or may return only in part. This module names what korvid knows
about each source a diagnostic reads, an `EvidenceState` and the `Basis` for
it, and combines those facts so that absence counts as proof only when it is.

It performs no I/O. `korvid.core.capability_classifier` turns a failed read
into this vocabulary. The design is
`docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType


class EvidenceState(StrEnum):
    """What korvid knows about one evidence source.

    - `UNKNOWN`: not observed yet, or a read is in flight.
    - `AVAILABLE`: the source answered completely at its scope.
    - `PARTIAL`: some evidence, known to be incomplete.
    - `UNSUPPORTED`: evidence establishes that the source or operation is not
      served, or not configured, here.
    - `DENIED`: the server or provider refused this principal.
    - `UNAVAILABLE`: could not be determined.
    """

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class Basis(StrEnum):
    """Why an `EvidenceState` holds; `ALLOWED_BASES` lists the bases of each state."""

    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    OBSERVED = "observed"
    OBJECT_ABSENT = "object_absent"
    NO_DATA = "no_data"
    TRUNCATED = "truncated"
    DEADLINE = "deadline"
    TOO_LARGE = "too_large"
    CONTINUATION_EXPIRED = "continuation_expired"
    SUBSCOPE_FAILED = "subscope_failed"
    STALE_STATUS = "stale_status"
    NOT_SERVED = "not_served"
    OPERATION_UNSUPPORTED = "operation_unsupported"
    NO_KNOWN_VERSION = "no_known_version"
    PROJECTION_UNSUPPORTED = "projection_unsupported"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    FEATURE_DISABLED = "feature_disabled"
    FORBIDDEN = "forbidden"
    UNAUTHENTICATED = "unauthenticated"
    DISCOVERY_FORBIDDEN = "discovery_forbidden"
    THROTTLED = "throttled"
    SERVER_ERROR = "server_error"
    UNEXPECTED_STATUS = "unexpected_status"
    TRANSPORT = "transport"
    MALFORMED = "malformed"
    DISCOVERY_FAILED = "discovery_failed"
    DISCOVERY_STALE = "discovery_stale"


_DENIED_BASES = frozenset({Basis.FORBIDDEN, Basis.UNAUTHENTICATED, Basis.DISCOVERY_FORBIDDEN})
_UNAVAILABLE_BASES = frozenset(
    {
        Basis.THROTTLED,
        Basis.SERVER_ERROR,
        Basis.UNEXPECTED_STATUS,
        Basis.TRANSPORT,
        Basis.MALFORMED,
        Basis.DEADLINE,
        Basis.TOO_LARGE,
        Basis.DISCOVERY_FAILED,
        Basis.DISCOVERY_STALE,
    }
)
#: A read that fails after at least one accepted page keeps what it read and
#: is `PARTIAL` with the failure's own basis. Discovery reads have no pages, so
#: the two bases only they produce never appear here. `DISCOVERY_STALE` does:
#: it is what a LIST gets when a 404 or 405 contradicts discovery.
_FAILURE_AFTER_A_PAGE = frozenset(
    (_DENIED_BASES | _UNAVAILABLE_BASES | {Basis.PROJECTION_UNSUPPORTED})
    - {Basis.DISCOVERY_FORBIDDEN, Basis.DISCOVERY_FAILED}
)

#: The bases each state allows. An observation with any other pairing is
#: refused, so a failure can never be recorded as a missing capability.
ALLOWED_BASES: Mapping[EvidenceState, frozenset[Basis]] = MappingProxyType(
    {
        EvidenceState.UNKNOWN: frozenset({Basis.NOT_STARTED, Basis.IN_FLIGHT}),
        EvidenceState.AVAILABLE: frozenset({Basis.OBSERVED, Basis.OBJECT_ABSENT, Basis.NO_DATA}),
        EvidenceState.PARTIAL: frozenset(
            {
                Basis.TRUNCATED,
                Basis.DEADLINE,
                Basis.TOO_LARGE,
                Basis.CONTINUATION_EXPIRED,
                Basis.SUBSCOPE_FAILED,
                Basis.STALE_STATUS,
            }
        )
        | _FAILURE_AFTER_A_PAGE,
        EvidenceState.UNSUPPORTED: frozenset(
            {
                Basis.NOT_SERVED,
                Basis.OPERATION_UNSUPPORTED,
                Basis.NO_KNOWN_VERSION,
                Basis.PROJECTION_UNSUPPORTED,
                Basis.PROVIDER_NOT_CONFIGURED,
                Basis.FEATURE_DISABLED,
            }
        ),
        EvidenceState.DENIED: _DENIED_BASES,
        EvidenceState.UNAVAILABLE: _UNAVAILABLE_BASES,
    }
)


class ScopeKind(StrEnum):
    """Where a source's evidence is read."""

    CLUSTER = "cluster"
    NAMESPACE = "namespace"
    NODE = "node"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class SourceScope:
    """The scope a source is read at: the cluster, or one namespace, node or provider."""

    kind: ScopeKind
    name: str = ""

    def __post_init__(self) -> None:
        if (self.kind is ScopeKind.CLUSTER) == bool(self.name):
            raise ValueError("a cluster scope has no name, and every other scope has one")

    @classmethod
    def cluster(cls) -> SourceScope:
        """The whole cluster."""
        return cls(ScopeKind.CLUSTER)

    @classmethod
    def namespace(cls, name: str) -> SourceScope:
        """One namespace."""
        return cls(ScopeKind.NAMESPACE, name)

    @classmethod
    def node(cls, name: str) -> SourceScope:
        """One node."""
        return cls(ScopeKind.NODE, name)

    @classmethod
    def provider(cls, provider_id: str) -> SourceScope:
        """One configured provider, such as a metrics backend."""
        return cls(ScopeKind.PROVIDER, provider_id)


class Verb(StrEnum):
    """The read operation a source is resolved for."""

    LIST = "list"
    GET = "get"


class Projection(StrEnum):
    """Whether a read returns whole objects or only their metadata."""

    OBJECT = "object"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class ApiSource:
    """One Kubernetes API resource, read at one scope.

    `version` and `verb` stay `None` until discovery resolves the source, and a
    resolved source names both. A GET names its object, and may carry the UID
    the caller expects that object to have.
    """

    group: str
    resource: str
    scope: SourceScope
    version: str | None = None
    verb: Verb | None = None
    subresource: str = ""
    projection: Projection = Projection.OBJECT
    name: str = ""
    expected_uid: str | None = None

    def __post_init__(self) -> None:
        if not self.resource:
            raise ValueError("an API source names a resource")
        if (self.version is None) != (self.verb is None):
            raise ValueError("a resolved API source names both a version and a verb")
        if self.verb is Verb.LIST and self.name:
            raise ValueError("a LIST source names no object")
        if self.verb is Verb.GET and not self.name:
            raise ValueError("a GET source names its object")
        if self.expected_uid is not None and not self.name:
            raise ValueError("an expected UID needs an object name")

    @property
    def resolved(self) -> bool:
        """Whether discovery has chosen this source's version and verb."""
        return self.version is not None


class DiscoveryEndpoint(StrEnum):
    """Which discovery document a `DiscoverySource` names."""

    CORE_ROOT = "core_root"
    GROUP_ROOT = "group_root"
    GROUP_VERSION = "group_version"


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    """One discovery endpoint: `/api`, `/apis`, or one group version.

    The core group version is `/api/v1`; any other is `/apis/<group>/<version>`.
    Discovery is always read cluster-wide.
    """

    endpoint: DiscoveryEndpoint
    group: str = ""
    version: str = ""

    def __post_init__(self) -> None:
        is_root = self.endpoint is not DiscoveryEndpoint.GROUP_VERSION
        if is_root and (self.group or self.version):
            raise ValueError("a discovery root names no group or version")
        if not is_root and not self.version:
            raise ValueError("a discovery group version names its version")

    @classmethod
    def core_root(cls) -> DiscoverySource:
        """The core root, `/api`."""
        return cls(DiscoveryEndpoint.CORE_ROOT)

    @classmethod
    def group_root(cls) -> DiscoverySource:
        """The named-group root, `/apis`."""
        return cls(DiscoveryEndpoint.GROUP_ROOT)

    @classmethod
    def group_version(cls, group: str, version: str) -> DiscoverySource:
        """One group version; the core group is `""`."""
        return cls(DiscoveryEndpoint.GROUP_VERSION, group, version)

    @property
    def path(self) -> str:
        """The endpoint's request path."""
        if self.endpoint is DiscoveryEndpoint.CORE_ROOT:
            return "/api"
        if self.endpoint is DiscoveryEndpoint.GROUP_ROOT:
            return "/apis"
        return f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"

    @property
    def scope(self) -> SourceScope:
        """Discovery is read cluster-wide."""
        return SourceScope.cluster()


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """One query to a configured provider, such as a metrics backend.

    Vocabulary only for now: no collector reads one until a diagnostic needs it.
    """

    provider_id: str
    query_kind: str
    scope: SourceScope

    def __post_init__(self) -> None:
        if not self.provider_id or not self.query_kind:
            raise ValueError("a provider source names its provider and query kind")


Source = ApiSource | DiscoverySource | ProviderSource


class Role(StrEnum):
    """What a diagnostic may conclude from a source it declares.

    - `RULES_OUT`: it may conclude something from the source's absence, for
      example "no quota constrains this workload".
    - `CONFIRMS`: only what the source contains matters, for example a
      recorded rejection event.
    """

    RULES_OUT = "rules_out"
    CONFIRMS = "confirms"


#: How long an observation stays fresh unless its source says otherwise:
#: Pulse's staleness window.
DEFAULT_MAX_AGE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """How long a source's observations stay fresh, and whether its entries expire.

    A retention-limited source, such as Events, drops old entries: it can
    confirm a recorded fact but never show that one is absent.
    """

    max_age: timedelta = DEFAULT_MAX_AGE
    retention_limited: bool = False

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("a maximum age is positive")


#: The policy of a source that declares no other.
DEFAULT_POLICY = FreshnessPolicy()


@dataclass(frozen=True, slots=True)
class Declaration:
    """One source a diagnostic reads, under the key its observation is kept at."""

    key: str
    required: bool
    role: Role
    policy: FreshnessPolicy = DEFAULT_POLICY

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("a declaration has a key")
        if self.role is Role.RULES_OUT and self.policy.retention_limited:
            raise ValueError("a retention-limited source can confirm a fact but never rule one out")


@dataclass(frozen=True, slots=True)
class RunToken:
    """The context epoch, session generation and run generation a read started under.

    A result is applied only while all three still match the current ones.
    """

    epoch: int
    session_generation: int
    run_generation: int


_DETAIL_LIMIT = 200
_CONCLUSIVE_BASES = frozenset({Basis.OBSERVED, Basis.OBJECT_ABSENT})


def _expected_uid(source: Source) -> str | None:
    return source.expected_uid if isinstance(source, ApiSource) else None


@dataclass(frozen=True, slots=True)
class Observation:
    """What one read established about one source.

    Attributes:
        source: The source that answered.
        state: What korvid knows about the source.
        basis: Why that state holds; one of the state's `ALLOWED_BASES`.
        observed_at: When, in UTC. `None` exactly while the state is `UNKNOWN`.
        items: How many objects the read returned.
        observed_uid: The UID a GET actually returned. Required when the
            object was observed and the source expects a UID, so that the
            observation's incarnation is always known.
        detail: Fixed template text and resource identities, never response
            bodies or field values. Text over 200 characters is shortened.
        token: The run token the read started under.
    """

    source: Source
    state: EvidenceState
    basis: Basis
    observed_at: datetime | None = None
    items: int = 0
    observed_uid: str | None = None
    detail: str = ""
    token: RunToken | None = None

    def __post_init__(self) -> None:
        if self.basis not in ALLOWED_BASES[self.state]:
            raise ValueError(f"{self.state} evidence cannot rest on {self.basis}")
        if (self.state is EvidenceState.UNKNOWN) != (self.observed_at is None):
            raise ValueError("an observation has a time exactly when its state is known")
        if self.observed_at is not None and self.observed_at.utcoffset() != timedelta(0):
            raise ValueError("observation times are UTC")
        if isinstance(self.items, bool) or not isinstance(self.items, int) or self.items < 0:
            raise ValueError("an item count is a non-negative integer")
        if (
            self.basis is Basis.OBSERVED
            and self.observed_uid is None
            and _expected_uid(self.source)
        ):
            raise ValueError("an observed object with an expected UID records the UID it saw")
        if len(self.detail) > _DETAIL_LIMIT:
            object.__setattr__(self, "detail", self.detail[: _DETAIL_LIMIT - 1] + "…")

    @property
    def recreated(self) -> bool:
        """Whether a GET found a different incarnation than the one expected.

        A recreated object never inherits the old one's evidence (#410).
        """
        expected = _expected_uid(self.source)
        if expected is None or self.observed_uid is None:
            return False
        return self.observed_uid != expected


def is_fresh(observation: Observation, policy: FreshnessPolicy, now: datetime) -> bool:
    """Whether the observation is no older than the policy's maximum age.

    An observation timed after `now` is not fresh: a clock that disagrees with
    itself proves nothing.
    """
    observed_at = observation.observed_at
    return observed_at is not None and timedelta(0) <= now - observed_at <= policy.max_age


def conclusive(observation: Observation, policy: FreshnessPolicy, now: datetime) -> bool:
    """Whether a diagnostic may treat absence in this observation as a fact.

    That takes complete, observed evidence (`NO_DATA` never counts: a provider
    that returns no series has not shown that a value is zero) about the
    expected incarnation, read recently enough, from a source whose entries do
    not expire.
    """
    return (
        observation.state is EvidenceState.AVAILABLE
        and observation.basis in _CONCLUSIVE_BASES
        and not observation.recreated
        and not policy.retention_limited
        and is_fresh(observation, policy, now)
    )


def downgrade_stale_status(observation: Observation, detail: str) -> Observation:
    """Mark available evidence whose content shows it lags as `PARTIAL`/`STALE_STATUS`.

    Read time is not semantic freshness: a ResourceQuota whose `status.hard`
    disagrees with its `spec.hard` was read just now but describes an older
    state.

    Raises:
        ValueError: if the observation is not `AVAILABLE`.
    """
    if observation.state is not EvidenceState.AVAILABLE:
        raise ValueError("only available evidence can be downgraded as stale")
    return dataclasses.replace(
        observation, state=EvidenceState.PARTIAL, basis=Basis.STALE_STATUS, detail=detail
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability.py -q`
Expected: all pass.

Run: `uv run --frozen ruff check src/korvid/core/capability.py tests/core/test_capability.py && uv run --frozen mypy src/korvid/core/capability.py`
Expected: no findings.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/capability.py tests/core/test_capability.py
git commit -m "core: add the evidence vocabulary for capability-aware diagnostics"
```

### Task 3: Combining observations

**Files:**
- Modify: `src/korvid/core/capability.py` (imports; append `Baseline`, `EvidenceSet`)
- Test: `tests/core/test_capability_baseline.py`

**Interfaces:**
- Consumes: from Task 2, `Declaration`, `Observation`, `Role`, `ScopeKind`, `conclusive` and `is_fresh`.
- Produces:
  - `Baseline(state, blocking=())`;
  - `EvidenceSet(observations=None)`, with `get(key)`, `with_observation(key, observation)`, `baseline(declarations, now) -> Baseline` and `by_scope(kind) -> Mapping[EvidenceState, int]`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_capability_baseline.py`:

```python
"""Combining observations (#409): absence is proof only when it is conclusive."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from korvid.core.capability import (
    DEFAULT_MAX_AGE,
    ApiSource,
    Baseline,
    Basis,
    Declaration,
    EvidenceSet,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    Role,
    ScopeKind,
    SourceScope,
    Verb,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
STALE = DEFAULT_MAX_AGE + timedelta(seconds=1)
QUOTAS = Declaration("quotas", required=True, role=Role.RULES_OUT)
LIMITS = Declaration("limits", required=True, role=Role.RULES_OUT)
EVENTS = Declaration(
    "events", required=True, role=Role.CONFIRMS, policy=FreshnessPolicy(retention_limited=True)
)
METRICS = Declaration("metrics", required=False, role=Role.RULES_OUT)


def _seen(
    state: EvidenceState = EvidenceState.AVAILABLE,
    basis: Basis = Basis.OBSERVED,
    *,
    age: timedelta = timedelta(0),
    scope: SourceScope = TEAM,
) -> Observation:
    observed_at = None if state is EvidenceState.UNKNOWN else NOW - age
    return Observation(ApiSource("", "pods", scope), state, basis, observed_at)


DENIED = _seen(EvidenceState.DENIED, Basis.FORBIDDEN)
UNAVAILABLE = _seen(EvidenceState.UNAVAILABLE, Basis.TRANSPORT)
UNKNOWN = _seen(EvidenceState.UNKNOWN, Basis.IN_FLIGHT)
UNSUPPORTED = _seen(EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)


def test_complete_evidence_is_available() -> None:
    evidence = EvidenceSet(
        {"quotas": _seen(), "limits": _seen(basis=Basis.OBJECT_ABSENT), "events": _seen()}
    )

    assert evidence.baseline((QUOTAS, LIMITS, EVENTS), NOW) == Baseline(EvidenceState.AVAILABLE)


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(_seen(), id="observed"),
        pytest.param(_seen(basis=Basis.NO_DATA), id="no-data"),
    ],
)
def test_a_confirming_source_never_needs_to_be_conclusive(events: Observation) -> None:
    evidence = EvidenceSet({"events": events})

    assert evidence.baseline((EVENTS,), NOW) == Baseline(EvidenceState.AVAILABLE)


@pytest.mark.parametrize(
    "quotas",
    [
        pytest.param(_seen(basis=Basis.NO_DATA), id="no-data"),
        pytest.param(_seen(age=STALE), id="stale"),
        pytest.param(_seen(EvidenceState.PARTIAL, Basis.TRUNCATED), id="truncated"),
    ],
)
def test_a_ruling_out_source_must_be_conclusive(quotas: Observation) -> None:
    evidence = EvidenceSet({"quotas": quotas, "events": _seen()})

    assert evidence.baseline((QUOTAS, EVENTS), NOW) == Baseline(EvidenceState.PARTIAL, ("quotas",))


def test_a_confirming_source_must_be_fresh() -> None:
    evidence = EvidenceSet({"events": _seen(age=STALE)})

    assert evidence.baseline((EVENTS,), NOW) == Baseline(EvidenceState.PARTIAL, ("events",))


@pytest.mark.parametrize("role", [Role.RULES_OUT, Role.CONFIRMS])
def test_a_recreated_object_never_completes_the_baseline(role: Role) -> None:
    expected = ApiSource("", "configmaps", TEAM, "v1", Verb.GET, name="app", expected_uid="uid-1")
    recreated = Observation(
        expected, EvidenceState.AVAILABLE, Basis.OBSERVED, NOW, observed_uid="uid-2"
    )
    config = Declaration("config", required=True, role=role)

    assert EvidenceSet({"config": recreated}).baseline((config,), NOW) == Baseline(
        EvidenceState.PARTIAL, ("config",)
    )


def test_any_contributed_evidence_makes_an_incomplete_baseline_partial() -> None:
    evidence = EvidenceSet({"quotas": _seen(), "limits": DENIED})

    assert evidence.baseline((QUOTAS, LIMITS), NOW) == Baseline(EvidenceState.PARTIAL, ("limits",))


@pytest.mark.parametrize(
    ("quotas", "limits", "expected"),
    [
        pytest.param(UNAVAILABLE, DENIED, EvidenceState.DENIED, id="denied-over-unavailable"),
        pytest.param(UNSUPPORTED, DENIED, EvidenceState.DENIED, id="denied-over-unsupported"),
        pytest.param(
            UNKNOWN, UNAVAILABLE, EvidenceState.UNAVAILABLE, id="unavailable-over-unknown"
        ),
        pytest.param(UNSUPPORTED, UNKNOWN, EvidenceState.UNKNOWN, id="unknown-over-unsupported"),
        pytest.param(UNSUPPORTED, UNSUPPORTED, EvidenceState.UNSUPPORTED, id="unsupported"),
    ],
)
def test_without_evidence_a_failure_outranks_a_missing_capability(
    quotas: Observation, limits: Observation, expected: EvidenceState
) -> None:
    evidence = EvidenceSet({"quotas": quotas, "limits": limits})

    assert evidence.baseline((QUOTAS, LIMITS), NOW) == Baseline(expected, ("limits", "quotas"))


def test_a_source_with_no_observation_is_unknown() -> None:
    assert EvidenceSet().baseline((QUOTAS,), NOW) == Baseline(EvidenceState.UNKNOWN, ("quotas",))


@pytest.mark.parametrize(
    "metrics",
    [
        pytest.param({}, id="missing"),
        pytest.param({"metrics": DENIED}, id="denied"),
        pytest.param({"metrics": UNSUPPORTED}, id="unsupported"),
        pytest.param({"metrics": _seen(basis=Basis.NO_DATA)}, id="no-data"),
    ],
)
def test_an_optional_source_never_lowers_the_baseline(metrics: dict[str, Observation]) -> None:
    evidence = EvidenceSet({"quotas": _seen(), **metrics})

    assert evidence.baseline((QUOTAS, METRICS), NOW) == Baseline(EvidenceState.AVAILABLE)


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        pytest.param(
            {"quotas": DENIED, "limits": UNSUPPORTED, "events": UNAVAILABLE},
            Baseline(EvidenceState.DENIED, ("events", "limits", "quotas")),
            id="failures",
        ),
        pytest.param(
            {"quotas": _seen(), "limits": UNKNOWN, "events": _seen()},
            Baseline(EvidenceState.PARTIAL, ("limits",)),
            id="partial",
        ),
        pytest.param(
            {"quotas": _seen(), "limits": _seen(), "events": _seen()},
            Baseline(EvidenceState.AVAILABLE),
            id="complete",
        ),
    ],
)
def test_the_baseline_ignores_declaration_and_insertion_order(
    observations: dict[str, Observation], expected: Baseline
) -> None:
    results = {
        EvidenceSet({key: observations[key] for key in keys}).baseline(order, NOW)
        for order in itertools.permutations((QUOTAS, LIMITS, EVENTS, METRICS))
        for keys in itertools.permutations(observations)
    }

    assert results == {expected}


def test_declarations_need_distinct_keys() -> None:
    with pytest.raises(ValueError, match="own key"):
        EvidenceSet().baseline((QUOTAS, QUOTAS), NOW)


def test_a_baseline_needs_a_required_source() -> None:
    with pytest.raises(ValueError, match="at least one required source"):
        EvidenceSet().baseline((METRICS,), NOW)


def test_states_are_counted_per_scope_kind_and_never_collapsed() -> None:
    nodes = {f"kubelet-{name}": _seen(scope=SourceScope.node(name)) for name in "abc"}
    denied = {
        f"kubelet-{name}": _seen(
            EvidenceState.DENIED, Basis.FORBIDDEN, scope=SourceScope.node(name)
        )
        for name in "de"
    }
    evidence = EvidenceSet({**nodes, **denied, "quotas": _seen()})

    assert evidence.by_scope(ScopeKind.NODE) == {
        EvidenceState.AVAILABLE: 3,
        EvidenceState.DENIED: 2,
    }
    assert evidence.by_scope(ScopeKind.NAMESPACE) == {EvidenceState.AVAILABLE: 1}
    assert evidence.by_scope(ScopeKind.CLUSTER) == {}


def test_recording_an_observation_leaves_the_original_set_unchanged() -> None:
    original = EvidenceSet()
    updated = original.with_observation("quotas", _seen())

    assert original.get("quotas") is None
    assert updated.get("quotas") == _seen()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability_baseline.py -q`
Expected: a collection error, `ImportError: cannot import name 'Baseline'`.

- [ ] **Step 3: Add the combining rules**

In `src/korvid/core/capability.py`, replace `from collections.abc import Mapping` with:

```python
from collections import Counter
from collections.abc import Iterable, Mapping
```

Append to `src/korvid/core/capability.py`:

```python


#: When no required source contributed evidence, the baseline is the first of
#: these states that one of them is in, so a failure is never summarized as a
#: missing capability.
_FAILURE_PRECEDENCE = (
    EvidenceState.DENIED,
    EvidenceState.UNAVAILABLE,
    EvidenceState.UNKNOWN,
    EvidenceState.UNSUPPORTED,
)
_CONTRIBUTED = frozenset({EvidenceState.AVAILABLE, EvidenceState.PARTIAL})


@dataclass(frozen=True, slots=True)
class Baseline:
    """A diagnostic's overall evidence state, and what keeps it from being complete.

    `blocking` holds the sorted keys of every required source that keeps the
    baseline from `AVAILABLE`, not only the one the precedence picked.
    """

    state: EvidenceState
    blocking: tuple[str, ...] = ()


class EvidenceSet:
    """An immutable collection of observations, keyed by declaration key."""

    __slots__ = ("_observations",)

    def __init__(self, observations: Mapping[str, Observation] | None = None) -> None:
        self._observations: Mapping[str, Observation] = MappingProxyType(dict(observations or {}))

    def get(self, key: str) -> Observation | None:
        """The observation kept under `key`, if any."""
        return self._observations.get(key)

    def with_observation(self, key: str, observation: Observation) -> EvidenceSet:
        """A copy with `observation` kept under `key`."""
        return EvidenceSet({**self._observations, key: observation})

    def baseline(self, declarations: Iterable[Declaration], now: datetime) -> Baseline:
        """Combine the required sources into one state, in any declaration order.

        - `AVAILABLE` when every required `RULES_OUT` source is conclusive and
          every required `CONFIRMS` source is available and fresh.
        - `PARTIAL` when a required source still contributed evidence.
        - Otherwise the first of `DENIED`, `UNAVAILABLE`, `UNKNOWN` and
          `UNSUPPORTED` that a required source is in.

        Optional sources never lower the baseline, and a source with no
        observation is `UNKNOWN`.

        Raises:
            ValueError: if two declarations share a key, or none is required.
        """
        required = _required(declarations)
        blocking = tuple(sorted(item.key for item in required if not self._complete(item, now)))
        if not blocking:
            return Baseline(EvidenceState.AVAILABLE)
        states = {self._state(item.key) for item in required}
        if states & _CONTRIBUTED:
            return Baseline(EvidenceState.PARTIAL, blocking)
        return Baseline(next(state for state in _FAILURE_PRECEDENCE if state in states), blocking)

    def by_scope(self, kind: ScopeKind) -> Mapping[EvidenceState, int]:
        """Count the states of the observations read at one kind of scope.

        "Available on 3 of 5 nodes" is `by_scope(ScopeKind.NODE)`: a mixed
        cluster is counted, never collapsed into one answer.
        """
        return dict(
            Counter(
                observation.state
                for observation in self._observations.values()
                if observation.source.scope.kind is kind
            )
        )

    def _state(self, key: str) -> EvidenceState:
        observation = self._observations.get(key)
        return EvidenceState.UNKNOWN if observation is None else observation.state

    def _complete(self, declaration: Declaration, now: datetime) -> bool:
        observation = self._observations.get(declaration.key)
        if observation is None or observation.recreated:
            return False
        if declaration.role is Role.RULES_OUT:
            return conclusive(observation, declaration.policy, now)
        return observation.state is EvidenceState.AVAILABLE and is_fresh(
            observation, declaration.policy, now
        )


def _required(declarations: Iterable[Declaration]) -> tuple[Declaration, ...]:
    items = tuple(declarations)
    if len({item.key for item in items}) != len(items):
        raise ValueError("each declaration has its own key")
    required = tuple(item for item in items if item.required)
    if not required:
        raise ValueError("a baseline needs at least one required source")
    return required
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability.py tests/core/test_capability_baseline.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/capability.py tests/core/test_capability_baseline.py
git commit -m "core: combine evidence so that absence is proof only when conclusive"
```

### Task 4: The read-outcome classifier

**Files:**
- Create: `src/korvid/core/capability_classifier.py`
- Test: `tests/core/test_capability_classifier.py`
- Modify: `tests/test_optional_extras.py` (the `test_base_import_does_not_require_optional_extras` parameters)
- Modify: `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md` (architecture table, S1 row)

**Interfaces:**
- Consumes:
  - from Task 1, `ApiStatusError`, `KubeClientError` and `KubeErrorCategory`;
  - from Task 2, `ApiSource`, `Basis`, `DiscoverySource`, `EvidenceState` and `Projection`.
- Produces, all in `korvid.core.capability_classifier`:
  - `STATUS_BODY_LIMIT = 16 * 1024`;
  - `ReadKind` (`LIST`, `GET`, `DISCOVERY`);
  - `ReadRequest(kind, source, continuation=False)`;
  - `StatusDetails(reason, name, group, kind)` and `parse_status(body: str) -> StatusDetails | None`;
  - `Classification(state, basis, detail)` and `classify(failure: BaseException, request: ReadRequest) -> Classification`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_capability_classifier.py`:

```python
"""The read-outcome classifier (#409): one table for every diagnostic."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.capability_classifier import (
    STATUS_BODY_LIMIT,
    ReadKind,
    ReadRequest,
    StatusDetails,
    classify,
    parse_status,
)
from korvid.k8s.errors import (
    ApiStatusError,
    KubeClientError,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTooLargeError,
    KubeTransportError,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
CONFIG = ApiSource(
    "", "configmaps", TEAM, "v1", Verb.GET, projection=Projection.METADATA, name="app"
)
DEPLOYMENTS = ApiSource("apps", "deployments", TEAM, "v1", Verb.LIST)
QUOTAS = ApiSource("", "resourcequotas", TEAM, "v1", Verb.LIST, projection=Projection.METADATA)

GET = ReadRequest(ReadKind.GET, CONFIG)
LIST = ReadRequest(ReadKind.LIST, DEPLOYMENTS)
METADATA_LIST = ReadRequest(ReadKind.LIST, QUOTAS)
CONTINUATION = ReadRequest(ReadKind.LIST, DEPLOYMENTS, continuation=True)
METADATA_CONTINUATION = ReadRequest(ReadKind.LIST, QUOTAS, continuation=True)
DISCOVERY = ReadRequest(ReadKind.DISCOVERY, DiscoverySource.group_version("apps", "v1"))
READS = [GET, LIST, METADATA_LIST, CONTINUATION, METADATA_CONTINUATION, DISCOVERY]


def _status(code: int, reason: str = "", **details: str) -> ApiStatusError:
    body = {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "reason": reason,
        "details": details,
        "code": code,
    }
    return ApiStatusError(code, reason, json.dumps(body))


NOT_FOUND = _status(404, "NotFound", name="app", kind="configmaps")
FAILURES = [
    _status(401, "Unauthorized"),
    _status(403, "Forbidden"),
    NOT_FOUND,
    _status(404, "NotFound"),
    ApiStatusError(404, "Not Found", "404 page not found"),
    _status(405, "MethodNotAllowed"),
    _status(406, "NotAcceptable"),
    _status(410, "Expired"),
    _status(429, "TooManyRequests"),
    _status(503, "ServiceUnavailable"),
    _status(400, "BadRequest"),
    KubeTimeoutError("timed out"),
    KubeTransportError("connection failed"),
    KubeMalformedError("malformed"),
    KubeTooLargeError("over budget"),
    KubeClientError("not connected"),
]

AVAILABLE = EvidenceState.AVAILABLE
PARTIAL = EvidenceState.PARTIAL
UNSUPPORTED = EvidenceState.UNSUPPORTED
DENIED = EvidenceState.DENIED
UNAVAILABLE = EvidenceState.UNAVAILABLE


@pytest.mark.parametrize(
    ("failure", "read", "state", "basis"),
    [
        pytest.param(_status(401), LIST, DENIED, Basis.UNAUTHENTICATED, id="401-list"),
        pytest.param(_status(401), GET, DENIED, Basis.UNAUTHENTICATED, id="401-get"),
        pytest.param(_status(401), DISCOVERY, DENIED, Basis.UNAUTHENTICATED, id="401-discovery"),
        pytest.param(_status(403), LIST, DENIED, Basis.FORBIDDEN, id="403-list"),
        pytest.param(_status(403), GET, DENIED, Basis.FORBIDDEN, id="403-get"),
        pytest.param(
            _status(403), DISCOVERY, DENIED, Basis.DISCOVERY_FORBIDDEN, id="403-discovery"
        ),
        pytest.param(NOT_FOUND, GET, AVAILABLE, Basis.OBJECT_ABSENT, id="404-names-the-object"),
        pytest.param(
            _status(404, "NotFound", name="other", kind="configmaps"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-names-another-object",
        ),
        pytest.param(
            _status(404, "NotFound", name="app", kind="secrets"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-names-another-resource",
        ),
        pytest.param(
            _status(404, "NotFound", name="app", group="apps", kind="configmaps"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-names-another-group",
        ),
        pytest.param(
            _status(404, "NotFound"), GET, UNAVAILABLE, Basis.DISCOVERY_STALE, id="404-no-details"
        ),
        pytest.param(
            ApiStatusError(404, "Not Found", "404 page not found"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-no-status-body",
        ),
        pytest.param(NOT_FOUND, LIST, UNAVAILABLE, Basis.DISCOVERY_STALE, id="404-list"),
        pytest.param(NOT_FOUND, DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="404-discovery"),
        pytest.param(_status(405), LIST, UNAVAILABLE, Basis.DISCOVERY_STALE, id="405-list"),
        pytest.param(_status(405), GET, UNAVAILABLE, Basis.DISCOVERY_STALE, id="405-get"),
        pytest.param(
            _status(405), DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="405-discovery"
        ),
        pytest.param(
            _status(406),
            METADATA_LIST,
            UNSUPPORTED,
            Basis.PROJECTION_UNSUPPORTED,
            id="406-metadata",
        ),
        pytest.param(
            _status(406), GET, UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED, id="406-metadata-get"
        ),
        pytest.param(_status(406), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="406-objects"),
        pytest.param(
            _status(406), DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="406-discovery"
        ),
        pytest.param(
            _status(410, "Expired"),
            CONTINUATION,
            PARTIAL,
            Basis.CONTINUATION_EXPIRED,
            id="410-expired-continuation",
        ),
        pytest.param(
            _status(410, "Gone"), CONTINUATION, PARTIAL, Basis.UNEXPECTED_STATUS, id="410-gone"
        ),
        pytest.param(
            _status(410, "Expired"), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="410-first-page"
        ),
        pytest.param(_status(429), LIST, UNAVAILABLE, Basis.THROTTLED, id="429-list"),
        pytest.param(_status(429), GET, UNAVAILABLE, Basis.THROTTLED, id="429-get"),
        pytest.param(_status(429), DISCOVERY, UNAVAILABLE, Basis.THROTTLED, id="429-discovery"),
        pytest.param(_status(500), LIST, UNAVAILABLE, Basis.SERVER_ERROR, id="500-list"),
        pytest.param(_status(503), GET, UNAVAILABLE, Basis.SERVER_ERROR, id="503-get"),
        pytest.param(
            _status(503), DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="503-discovery"
        ),
        pytest.param(_status(400), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="400-list"),
        pytest.param(_status(409), GET, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="409-get"),
        pytest.param(
            ApiStatusError(0, ""), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="no-status"
        ),
        pytest.param(KubeTimeoutError("t"), LIST, UNAVAILABLE, Basis.TRANSPORT, id="timeout"),
        pytest.param(KubeTransportError("t"), GET, UNAVAILABLE, Basis.TRANSPORT, id="transport"),
        pytest.param(KubeClientError("t"), LIST, UNAVAILABLE, Basis.TRANSPORT, id="uncategorized"),
        pytest.param(KubeMalformedError("t"), LIST, UNAVAILABLE, Basis.MALFORMED, id="malformed"),
        pytest.param(KubeTooLargeError("t"), GET, UNAVAILABLE, Basis.TOO_LARGE, id="too-large"),
        pytest.param(
            KubeTimeoutError("t"),
            DISCOVERY,
            UNAVAILABLE,
            Basis.DISCOVERY_FAILED,
            id="timeout-discovery",
        ),
        pytest.param(
            KubeMalformedError("t"),
            DISCOVERY,
            UNAVAILABLE,
            Basis.DISCOVERY_FAILED,
            id="malformed-discovery",
        ),
        pytest.param(
            _status(401), CONTINUATION, PARTIAL, Basis.UNAUTHENTICATED, id="401-continued"
        ),
        pytest.param(_status(403), CONTINUATION, PARTIAL, Basis.FORBIDDEN, id="403-continued"),
        pytest.param(
            _status(404), CONTINUATION, PARTIAL, Basis.DISCOVERY_STALE, id="404-continued"
        ),
        pytest.param(
            _status(405), CONTINUATION, PARTIAL, Basis.DISCOVERY_STALE, id="405-continued"
        ),
        pytest.param(
            _status(406),
            METADATA_CONTINUATION,
            PARTIAL,
            Basis.PROJECTION_UNSUPPORTED,
            id="406-metadata-continued",
        ),
        pytest.param(
            KubeTooLargeError("t"), CONTINUATION, PARTIAL, Basis.TOO_LARGE, id="large-continued"
        ),
    ],
)
def test_the_classification_table(
    failure: BaseException, read: ReadRequest, state: EvidenceState, basis: Basis
) -> None:
    result = classify(failure, read)

    assert (result.state, result.basis) == (state, basis)


@pytest.mark.parametrize("read", READS)
@pytest.mark.parametrize("failure", FAILURES)
def test_every_classification_can_be_recorded(failure: BaseException, read: ReadRequest) -> None:
    result = classify(failure, read)
    observation = Observation(read.source, result.state, result.basis, NOW, detail=result.detail)

    assert (observation.state, observation.basis) == (result.state, result.basis)


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (_status(503, "token=secret"), "HTTP 503"),
        (KubeTimeoutError("token=secret"), "request timed out"),
        (KubeTransportError("token=secret"), "connection failed"),
        (KubeMalformedError("token=secret"), "malformed response"),
        (KubeTooLargeError("token=secret"), "response over budget"),
        (KubeClientError("token=secret"), "request failed"),
    ],
)
def test_details_come_from_fixed_templates(failure: BaseException, detail: str) -> None:
    assert classify(failure, LIST).detail == detail


def test_cancellation_is_re_raised_and_never_classified() -> None:
    with pytest.raises(asyncio.CancelledError, match="stop reading"):
        classify(asyncio.CancelledError("stop reading"), LIST)


def test_a_failure_outside_the_kubernetes_layer_propagates() -> None:
    with pytest.raises(RuntimeError, match="a bug"):
        classify(RuntimeError("a bug"), LIST)


@pytest.mark.parametrize(
    ("kind", "source", "continuation", "message"),
    [
        (ReadKind.DISCOVERY, DEPLOYMENTS, False, "discovery source"),
        (ReadKind.LIST, DiscoverySource.group_root(), False, "discovery source"),
        (ReadKind.GET, CONFIG, True, "only a LIST continues"),
        (ReadKind.GET, ApiSource("", "configmaps", TEAM), False, "names its object"),
    ],
)
def test_a_read_request_is_well_formed(
    kind: ReadKind, source: ApiSource | DiscoverySource, continuation: bool, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ReadRequest(kind, source, continuation)


def test_a_status_body_yields_its_reason_and_details() -> None:
    assert parse_status(NOT_FOUND.body) == StatusDetails("NotFound", "app", "", "configmaps")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("404 page not found", id="text"),
        pytest.param("[]", id="list"),
        pytest.param('{"kind": "Pod", "reason": "NotFound"}', id="another-kind"),
        pytest.param("[" * 5000, id="deeply-nested"),
    ],
)
def test_a_body_that_is_not_a_status_reads_as_none(body: str) -> None:
    assert parse_status(body) is None


def test_status_fields_of_the_wrong_type_read_as_empty() -> None:
    body = '{"kind": "Status", "reason": 3, "details": "app"}'

    assert parse_status(body) == StatusDetails("", "", "", "")


def test_a_status_body_over_the_budget_is_not_parsed() -> None:
    body = NOT_FOUND.body[:-1] + " " * STATUS_BODY_LIMIT + "}"

    assert json.loads(body)["reason"] == "NotFound"
    assert parse_status(body) is None
    assert classify(ApiStatusError(404, "Not Found", body), GET).basis is Basis.DISCOVERY_STALE


def test_the_body_budget_counts_utf8_bytes() -> None:
    message = "é" * (STATUS_BODY_LIMIT // 2)
    body = json.dumps(
        {"kind": "Status", "reason": "NotFound", "message": message}, ensure_ascii=False
    )

    assert len(body) <= STATUS_BODY_LIMIT < len(body.encode())
    assert parse_status(body) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability_classifier.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'korvid.core.capability_classifier'`.

- [ ] **Step 3: Write the classifier**

Create `src/korvid/core/capability_classifier.py`:

```python
"""Classify failed Kubernetes reads into the evidence vocabulary (#409).

One table decides what a 403, a 404, an expired continuation or a dropped
connection means for every diagnostic, so that none decides it alone. The
classifier reads the HTTP status, the category the k8s layer gave a client
failure, and at most `STATUS_BODY_LIMIT` bytes of a Kubernetes `Status`
body. It never parses error messages, and never classifies cancellation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from korvid.core.capability import ApiSource, Basis, DiscoverySource, EvidenceState, Projection
from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeErrorCategory

#: Readers keep at most this many bytes of an error body, and the classifier
#: parses no more.
STATUS_BODY_LIMIT = 16 * 1024


class ReadKind(StrEnum):
    """The kind of request that failed."""

    LIST = "list"
    GET = "get"
    DISCOVERY = "discovery"


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """What a failed request asked for: the context its exception does not carry.

    `continuation` is true when the request followed an accepted page, which is
    what makes its failure `PARTIAL`.
    """

    kind: ReadKind
    source: ApiSource | DiscoverySource
    continuation: bool = False

    def __post_init__(self) -> None:
        if (self.kind is ReadKind.DISCOVERY) != isinstance(self.source, DiscoverySource):
            raise ValueError("a discovery read names a discovery source, and no other read does")
        if self.continuation and self.kind is not ReadKind.LIST:
            raise ValueError("only a LIST continues")
        if (
            self.kind is ReadKind.GET
            and isinstance(self.source, ApiSource)
            and not self.source.name
        ):
            raise ValueError("a GET names its object")


@dataclass(frozen=True, slots=True)
class StatusDetails:
    """The parts of a Kubernetes `Status` body the classifier reads."""

    reason: str
    name: str
    group: str
    kind: str


@dataclass(frozen=True, slots=True)
class Classification:
    """The state and basis a failure maps to, with fixed detail text."""

    state: EvidenceState
    basis: Basis
    detail: str


_CATEGORY_BASIS: Mapping[KubeErrorCategory | None, Basis] = MappingProxyType(
    {
        None: Basis.TRANSPORT,
        "timeout": Basis.TRANSPORT,
        "transport": Basis.TRANSPORT,
        "malformed": Basis.MALFORMED,
        "too_large": Basis.TOO_LARGE,
    }
)
_CATEGORY_DETAIL: Mapping[KubeErrorCategory | None, str] = MappingProxyType(
    {
        None: "request failed",
        "timeout": "request timed out",
        "transport": "connection failed",
        "malformed": "malformed response",
        "too_large": "response over budget",
    }
)
_BASIS_STATE: Mapping[Basis, EvidenceState] = MappingProxyType(
    {
        Basis.UNAUTHENTICATED: EvidenceState.DENIED,
        Basis.FORBIDDEN: EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN: EvidenceState.DENIED,
        Basis.OBJECT_ABSENT: EvidenceState.AVAILABLE,
        Basis.PROJECTION_UNSUPPORTED: EvidenceState.UNSUPPORTED,
    }
)


def parse_status(body: str) -> StatusDetails | None:
    """Read `reason` and `details` from a Kubernetes `Status` body.

    Returns `None` for a body over `STATUS_BODY_LIMIT` UTF-8 bytes, or one that
    is not a `Status` object. A field that is not a string reads as `""`.
    """
    if len(body) > STATUS_BODY_LIMIT or len(body.encode("utf-8", "replace")) > STATUS_BODY_LIMIT:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "Status":
        return None
    details = document.get("details")
    if not isinstance(details, dict):
        details = {}
    return StatusDetails(
        reason=_text(document.get("reason")),
        name=_text(details.get("name")),
        group=_text(details.get("group")),
        kind=_text(details.get("kind")),
    )


def classify(failure: BaseException, request: ReadRequest) -> Classification:
    """Map a failed read to a state, a basis and a fixed detail.

    Only Kubernetes failures are classified: anything else, cancellation
    included, is re-raised unchanged. A failure on a continuation keeps the
    pages already read, so it is `PARTIAL` with the same basis.
    """
    if isinstance(failure, ApiStatusError):
        basis = _status_basis(failure, request)
        detail = f"HTTP {failure.status}"
    elif isinstance(failure, KubeClientError):
        is_discovery = request.kind is ReadKind.DISCOVERY
        basis = Basis.DISCOVERY_FAILED if is_discovery else _CATEGORY_BASIS[failure.category]
        detail = _CATEGORY_DETAIL[failure.category]
    else:
        raise failure
    if request.continuation:
        return Classification(EvidenceState.PARTIAL, basis, detail)
    return Classification(_BASIS_STATE.get(basis, EvidenceState.UNAVAILABLE), basis, detail)


def _status_basis(failure: ApiStatusError, request: ReadRequest) -> Basis:
    if failure.status == 401:
        return Basis.UNAUTHENTICATED
    if failure.status == 429:
        return Basis.THROTTLED
    if isinstance(request.source, DiscoverySource):
        return Basis.DISCOVERY_FORBIDDEN if failure.status == 403 else Basis.DISCOVERY_FAILED
    if failure.status == 403:
        return Basis.FORBIDDEN
    return _read_status_basis(failure, request, request.source)


def _read_status_basis(failure: ApiStatusError, request: ReadRequest, source: ApiSource) -> Basis:
    status = failure.status
    if status == 404:
        named = _names_object(failure, request, source)
        return Basis.OBJECT_ABSENT if named else Basis.DISCOVERY_STALE
    if status == 405:
        return Basis.DISCOVERY_STALE
    if status == 406 and source.projection is Projection.METADATA:
        return Basis.PROJECTION_UNSUPPORTED
    if status == 410 and request.continuation and _reason(failure) == "Expired":
        return Basis.CONTINUATION_EXPIRED
    if 500 <= status <= 599:
        return Basis.SERVER_ERROR
    return Basis.UNEXPECTED_STATUS


def _names_object(failure: ApiStatusError, request: ReadRequest, source: ApiSource) -> bool:
    if request.kind is not ReadKind.GET:
        return False
    status = parse_status(failure.body)
    return (
        status is not None
        and status.reason == "NotFound"
        and (status.name, status.group, status.kind) == (source.name, source.group, source.resource)
    )


def _reason(failure: ApiStatusError) -> str:
    status = parse_status(failure.body)
    return "" if status is None else status.reason


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
```

- [ ] **Step 4: Cover the new modules in the extras guard**

In `tests/test_optional_extras.py`, add `"korvid.core.capability"` and `"korvid.core.capability_classifier"` to the parameters of `test_base_import_does_not_require_optional_extras`. Its docstring then reads: "The composition root, UI, tool layer, agent session, outbound policy and evidence vocabulary are all importable without the [mcp]/[agent] extras installed."

- [ ] **Step 5: Name the classifier module in the design**

In the design's architecture table, change the Vocabulary row to `| Vocabulary | korvid.core.capability (new) | stdlib | states, bases, sources, observations, combining rules, run tokens |`. Below it, add `| Classifier | korvid.core.capability_classifier (new) | korvid.core.capability, korvid.k8s.errors | read requests, Status parsing, outcome classification |`. In the Evidence reads row, replace "the two above" with "the three above". In the S1 delivery row, after "`ReadRequest` and the classifier with `Status` parsing", add "(in `core/capability_classifier`)".

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability_classifier.py tests/test_optional_extras.py -q`
Expected: all pass.

Run: `uv run --frozen tach check && uv run --frozen mypy src/korvid/core/capability_classifier.py`
Expected: `All modules validated!` and no mypy findings.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/core/capability_classifier.py tests/core/test_capability_classifier.py tests/test_optional_extras.py docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md
git commit -m "core: classify failed reads with one table for every diagnostic"
```

### Task 5: Pin how Pulse's states map onto the vocabulary

**Files:**
- Test: `tests/core/test_capability_pulse_mapping.py`

**Interfaces:**
- Consumes:
  - `PulseCollector`, `PulseCoverageState`, `PulsePage`, `PulseReader`, `PulseSource`, `PulseLimitError` and `korvid.core.pulse._STALE_AFTER` (unchanged);
  - `DEFAULT_MAX_AGE` and `EvidenceState` from Task 2;
  - `classify`, `ReadKind` and `ReadRequest` from Task 4.
- Produces: no production code. Until the convergence follow-up, the test's table is the mapping.

- [ ] **Step 1: Write the pinning test**

Create `tests/core/test_capability_pulse_mapping.py`:

```python
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
    KubeTransportError,
)
from korvid.k8s.pulse import PulseLimitError, PulsePage, PulseReader, PulseSource

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
    PulseLimitError("byte cap"),
]


class _Reader(PulseReader):
    def __init__(self, *outcomes: PulsePage | Exception) -> None:
        self._outcomes = list(outcomes)
        self.continuations: list[str] = []

    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        self.continuations.append(continuation)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _collect(*outcomes: PulsePage | Exception) -> tuple[PulseSourceResult, list[str]]:
    reader = _Reader(*outcomes)
    (result,) = await PulseCollector(reader, (PODS,)).collect("team-a")
    return result, reader.continuations


def _mapped(result: PulseSourceResult) -> EvidenceState:
    with_objects, without = PULSE_TO_EVIDENCE[result.coverage.state]
    return with_objects if result.objects else without


def test_every_pulse_state_has_a_mapping() -> None:
    assert set(PULSE_TO_EVIDENCE) == set(get_args(PulseCoverageState))


def test_the_default_maximum_age_is_pulses_staleness_window() -> None:
    assert DEFAULT_MAX_AGE == pulse._STALE_AFTER


async def test_a_complete_pulse_source_is_available() -> None:
    result, _continuations = await _collect(PulsePage(({},)))

    assert result.coverage.state == "complete"
    assert _mapped(result) is EvidenceState.AVAILABLE


@pytest.mark.parametrize("failure", FAILURES)
async def test_a_first_page_failure_maps_to_the_classifiers_state(failure: Exception) -> None:
    result, continuations = await _collect(failure)

    assert continuations == [""]
    assert not result.objects
    assert _mapped(result) is classify(failure, LIST).state


@pytest.mark.parametrize("failure", FAILURES)
async def test_a_failure_after_kept_objects_maps_to_the_classifiers_state(
    failure: Exception,
) -> None:
    result, continuations = await _collect(PulsePage(({},), "next"), failure)

    assert continuations == ["", "next"]
    assert result.objects
    assert _mapped(result) is classify(failure, CONTINUATION).state
```

- [ ] **Step 2: Run the test**

Run: `uv run --frozen pytest -p no:tach tests/core/test_capability_pulse_mapping.py -q`
Expected: all pass. The test pins existing behavior, so it passes as soon as it is written.

- [ ] **Step 3: Prove the pin catches drift**

Temporarily change the `"unavailable"` row to `(EvidenceState.UNSUPPORTED, EvidenceState.UNSUPPORTED)` and rerun.
Expected: `test_a_first_page_failure_maps_to_the_classifiers_state` fails for the 404 and 405 failures.

Restore the row, rerun, and confirm the tests pass again.

- [ ] **Step 4: Commit**

```bash
git add tests/core/test_capability_pulse_mapping.py
git commit -m "test: pin how Pulse coverage maps onto the evidence vocabulary"
```

### Task 6: Run the full gate

- [ ] **Step 1: Run every check that `make check` runs**

```bash
uv run --frozen python scripts/check_source_size.py
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy src/
uv run --frozen tach check
uv run --frozen pytest -n 4 -q
```

Expected:
- every command exits 0;
- `client.py` is within its 1,944-line cap;
- the suite reports no failures.

- [ ] **Step 2: Check the design against this slice**

The PR reports each of #490's acceptance criteria with the test that covers it:

| Criterion | Test |
| --- | --- |
| Each state accepts only its own bases | `test_an_observation_refuses_every_other_basis` |
| `conclusive` is false for `NO_DATA`, stale and retention-limited evidence | `test_only_observed_available_evidence_is_conclusive`, `test_evidence_older_than_the_maximum_age_is_not_conclusive`, `test_fresh_events_are_never_conclusive_about_absence` |
| `baseline` is order-independent | `test_the_baseline_ignores_declaration_and_insertion_order` |
| Optional sources never lower the baseline | `test_an_optional_source_never_lowers_the_baseline` |
| The classifier implements the design's table | `test_the_classification_table` |
| Every classification can be recorded | `test_every_classification_can_be_recorded` |
| Classification never parses messages | `test_details_come_from_fixed_templates`, `test_error_categories.py` |
| Error bodies over 16 KiB are not parsed | `test_a_status_body_over_the_budget_is_not_parsed`, `test_the_body_budget_counts_utf8_bytes` |
| No Textual or optional extra | `test_base_import_does_not_require_optional_extras`, `tach check` |

