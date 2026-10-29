# ConfigMap and Secret Consumers, View (#410 P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show, for the selected ConfigMap or Secret, which Pods consume it and what a change means for each, through the shared evidence renderer (#502, the view half of slice P1 of #410; ships #486, S6 of #409).

**Architecture:** `ui/evidence_text.py` is the one place an `Observation` becomes text. `ui/widgets/config_propagation_screen.py` shows one `PropagationReport` (from #503) with the consumers grouped by mode. `ui/config_propagation_controller.py` runs `explain_propagation` in a worker under a run token and opens the view only while the token is current. A `:consumers` command and palette entry reach it; the composition root hands it the session's `KubeClient` as an `EvidenceClient`.

**Tech Stack:** Python 3.11+, Textual, Rich `Text`, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-28-config-propagation-design.md`, and the presentation contract in `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`. Where this plan departs from them, the section below says why.
- **One line per source:** `<resource> <group/version> <scope>: <state> — <basis sentence> · observed <age>`, stale marked by the source's own `FreshnessPolicy`.
- **Widgets never interpret bases:** every `Basis` has one sentence in `evidence_text`.
- **Literal names:** every cluster-provided name is a plain `Text`, never parsed as markup.
- **Conclusions from absence:** "no Pod references it" only when the Pod list is conclusive; otherwise "Not established" with the reason.
- **Availability:** the entry is unavailable only without a connection, on the wrong view, or without a selection.
- **Late results:** dropped after a context switch, new credentials or a newer run, or when the user has left the view, scope or selection the run started from; the context switch cancels the worker group, and a failed worker is reported, not dropped.
- **No values, no writes:** the view shows references only; restarting is a suggestion.
- **Size limits:** `scripts/check_source_size.py` is not edited; `__main__.py` and `ui/app.py` make room by moving code.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **The command is `:consumers`, not `:propagation`.** `:propagation` would make `prop` ambiguous with `:proposals` in the command bar's first-word completion, which a test pins. The palette entry is still **Explain configuration propagation**, and `propagation` is one of its search terms.
- **The context switch cancels the group by name.** The design names a `cancel_config_propagation_workers()` hook; the coordinator already cancels `hint-events` by name with `cancel_worker_group`, so it does the same here and needs no new constructor argument. The controller's `cancel_workers()` remains for other callers.
- **`EvidenceClient` lives in `k8s`.** The controller needs evidence reads, discovery and the session generation from one client; naming that in `k8s/evidence_requests.py` lets `KubeClient` declare it and the UI depend on the boundary instead of `KubeClient`.
- **The view is not yet bindable in the keybinding editor.** Design decision 5 keeps it off every default key and makes it bindable in `:keys`, but the editor only remaps `APP_BINDINGS` entries, each of which has a default key. Supporting an action that ships unbound changes the keymap contract, so it is its own change, #510. Until then the view is reached with `:consumers` and the palette.
- **The observability builders move to `composition_support.py`** to make room in `__main__.py`, which was three lines under its limit.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/composition_support.py` | modify | gains the observability builders from `__main__.py` |
| `src/korvid/__main__.py` | modify | imports `_build_observability` back; wires the controller, command and client |
| `src/korvid/k8s/evidence_requests.py` | modify | `EvidenceClient` |
| `src/korvid/k8s/client.py` | modify | `KubeClient` implements `EvidenceClient` |
| `src/korvid/ui/evidence_text.py` | create | basis sentences, ages, source text, observation lines |
| `src/korvid/ui/widgets/config_propagation_screen.py` | create | rows by mode, notes, the modal screen |
| `src/korvid/ui/config_propagation_controller.py` | create | availability, the run, late results, Pod navigation |
| `src/korvid/ui/worker_failures.py` | create | the worker groups whose failures the app reports, `config-propagation` among them |
| `src/korvid/ui/messages.py`, `ui/command.py`, `ui/command_router.py`, `ui/action_policy.py` | modify | `:consumers` and its palette availability |
| `src/korvid/ui/context_switch_coordinator.py` | modify | cancel the worker group before the swap |
| `src/korvid/ui/app.py`, `ui/app_runtime.py` | modify | the `evidence_client` input |
| `tests/version_matrix/fixtures.yaml`, `test_scenarios.py` | modify | a consumer Pod and its scenario |
| `.github/workflows/k8s-version-matrix.yml` | modify | path filters for the new modules |
| `docs/tui.md`, `docs/keybindings.md`, `docs/kubernetes-support.md`, `docs/release-notes/unreleased.md` | modify | user documentation |

---

### Task 1: Make room in the composition root

**Files:**
- Modify: `src/korvid/__main__.py`, `src/korvid/composition_support.py`
- Test: `tests/test_observability_wiring.py`

- [ ] **Step 1: Move the builders**

Move `_OBSERVABILITY_EXTRA_ROOTS`, `_OBSERVABILITY_INSTALL_HINT`, `_build_observability`, `_validate_observability` and `_connectors` from `__main__.py` to `composition_support.py`, after `ObservabilityWiring`, importing `ObservabilityBackend` there. In `__main__.py`, import the builder back:

```python
from korvid.composition_support import (
    _build_observability as _build_observability,
)
```

- [ ] **Step 2: Patch where the builder looks things up**

In `tests/test_observability_wiring.py`, the two `TestMissingExtra` tests patch `korvid.composition_support._missing_extra_packages` instead of `korvid.__main__`'s.

- [ ] **Step 3: Run the tests that touch it**

Run: `uv run --frozen pytest -p no:tach tests/test_observability_wiring.py tests/test_agent_cleanup.py tests/test_optional_extras.py tests/test_main_wiring.py -q`
Expected: all pass. `python scripts/check_source_size.py` passes, and `__main__.py` drops by about 130 lines.

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor: build the observability connectors in composition_support"
```

### Task 2: The shared evidence renderer

**Files:**
- Create: `src/korvid/ui/evidence_text.py`
- Test: `tests/ui/test_evidence_text.py`

**Interfaces:**
- Produces: `BASIS_SENTENCES`, `basis_sentence(basis) -> str`, `age_text(observed_at, now) -> str`, `source_text(source) -> str`, `observation_line(observation, policy, now) -> Text`, `not_established(what) -> Text`.

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_evidence_text.py` with:

```python
"""The shared evidence renderer (#486, S6 of #409)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rich.text import Text

from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    ProviderSource,
    Source,
    SourceScope,
    Verb,
)
from korvid.ui.evidence_text import (
    age_text,
    basis_sentence,
    not_established,
    observation_line,
)

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
POLICY = FreshnessPolicy()
CONFIG_MAPS = ApiSource("", "configmaps", SourceScope.namespace("team-a"), "v1", Verb.LIST)


def _observed(source: Source, state: EvidenceState, basis: Basis, seconds: float) -> Observation:
    return Observation(source, state, basis, NOW - timedelta(seconds=seconds))


def test_every_basis_has_its_own_sentence() -> None:
    sentences = [basis_sentence(basis) for basis in Basis]

    assert all(sentences)
    assert len(set(sentences)) == len(Basis)


@pytest.mark.parametrize(
    ("basis", "words"),
    [
        (Basis.FORBIDDEN, "permissions"),
        (Basis.NOT_SERVED, "does not serve"),
        (Basis.SERVER_ERROR, "returned an error"),
        (Basis.TRUNCATED, "page limit"),
        (Basis.DEADLINE, "deadline"),
        (Basis.TOO_LARGE, "too large"),
        (Basis.CONTINUATION_EXPIRED, "changed while"),
    ],
)
def test_denied_unsupported_unavailable_and_partial_read_differently(
    basis: Basis, words: str
) -> None:
    assert words in basis_sentence(basis)


def test_an_api_line_names_the_resource_version_scope_state_basis_and_age() -> None:
    observation = _observed(CONFIG_MAPS, EvidenceState.AVAILABLE, Basis.OBSERVED, 2)

    line = observation_line(observation, POLICY, NOW)

    assert isinstance(line, Text)
    assert line.plain == "configmaps v1 in namespace team-a: available — read · observed 2s ago"


def test_a_grouped_cluster_wide_source_names_its_group() -> None:
    events = ApiSource("events.k8s.io", "events", SourceScope.cluster(), "v1", Verb.LIST)
    observation = _observed(events, EvidenceState.DENIED, Basis.FORBIDDEN, 5)

    assert observation_line(observation, POLICY, NOW).plain == (
        "events events.k8s.io/v1 cluster-wide: denied — "
        "your permissions do not allow this read · observed 5s ago"
    )


def test_an_unresolved_source_names_its_group_without_a_version() -> None:
    events = ApiSource("events.k8s.io", "events", SourceScope.namespace("a"))
    observation = _observed(events, EvidenceState.UNSUPPORTED, Basis.NOT_SERVED, 1)

    assert observation_line(observation, POLICY, NOW).plain.startswith(
        "events events.k8s.io in namespace a: unsupported — "
    )


def test_discovery_and_provider_sources_name_what_they_read() -> None:
    discovery = _observed(
        DiscoverySource.group_version("apps", "v1"),
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
        3,
    )
    provider = _observed(
        ProviderSource("prometheus", "cpu", SourceScope.node("n1")),
        EvidenceState.UNSUPPORTED,
        Basis.PROVIDER_NOT_CONFIGURED,
        3,
    )

    assert observation_line(discovery, POLICY, NOW).plain.startswith(
        "discovery /apis/apps/v1 cluster-wide: unavailable — API discovery failed"
    )
    assert observation_line(provider, POLICY, NOW).plain.startswith(
        "prometheus cpu on node n1: unsupported — no provider is configured"
    )


def test_staleness_follows_the_sources_own_policy() -> None:
    observation = _observed(CONFIG_MAPS, EvidenceState.AVAILABLE, Basis.OBSERVED, 60)
    short = FreshnessPolicy(max_age=timedelta(seconds=30))
    long = FreshnessPolicy(max_age=timedelta(minutes=5))

    assert observation_line(observation, short, NOW).plain.endswith("observed 1m ago · stale")
    assert observation_line(observation, long, NOW).plain.endswith("observed 1m ago")


def test_an_observation_never_made_is_not_called_stale() -> None:
    observation = Observation(CONFIG_MAPS, EvidenceState.UNKNOWN, Basis.NOT_STARTED)

    assert observation_line(observation, POLICY, NOW).plain == (
        "configmaps v1 in namespace team-a: unknown — not read yet · not observed"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.4, "observed just now"),
        (59, "observed 59s ago"),
        (61, "observed 1m ago"),
        (2 * 3600 + 5, "observed 2h ago"),
        (3 * 86400, "observed 3d ago"),
        (-5, "observed in the future"),
    ],
)
def test_ages_are_whole_units(seconds: float, text: str) -> None:
    assert age_text(NOW - timedelta(seconds=seconds), NOW) == text


def test_cluster_provided_names_are_rendered_literally() -> None:
    hostile = ApiSource("", "configmaps", SourceScope.namespace("[bold]x[/bold]"), "v1", Verb.LIST)
    observation = _observed(hostile, EvidenceState.AVAILABLE, Basis.OBSERVED, 1)

    line = observation_line(observation, POLICY, NOW)

    assert "in namespace [bold]x[/bold]:" in line.plain
    assert line.spans == []


def test_not_established_says_why() -> None:
    line = not_established("whether every consumer is listed")

    assert line.plain == "Not established: whether every consumer is listed"
    assert not_established("[red]x").plain == "Not established: [red]x"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_evidence_text.py -q`
Expected: `ModuleNotFoundError: No module named 'korvid.ui.evidence_text'`.

- [ ] **Step 3: Write the module**

Create `src/korvid/ui/evidence_text.py` with:

```python
"""The one place evidence observations become text (#486, S6 of #409).

Every diagnostic panel shows its sources the same way, one line each:

    <resource> <group/version> <scope>: <state> — <basis sentence> · observed <age>

Widgets call these functions and never read a `Basis` themselves, so a
denied read, an API the cluster does not serve and a server error read the
same everywhere. Cluster-provided names go into a plain `Text`, which never
parses markup.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from rich.text import Text

from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    FreshnessPolicy,
    Observation,
    ScopeKind,
    Source,
    SourceScope,
    is_fresh,
)

#: One sentence for every basis. A new `Basis` without one fails the tests.
BASIS_SENTENCES: Mapping[Basis, str] = MappingProxyType(
    {
        Basis.NOT_STARTED: "not read yet",
        Basis.IN_FLIGHT: "reading",
        Basis.OBSERVED: "read",
        Basis.OBJECT_ABSENT: "the object does not exist",
        Basis.NO_DATA: "the provider returned no data",
        Basis.TRUNCATED: "stopped at the page limit",
        Basis.DEADLINE: "stopped at the deadline",
        Basis.TOO_LARGE: "a response was too large to read",
        Basis.CONTINUATION_EXPIRED: "the list changed while it was being read",
        Basis.SUBSCOPE_FAILED: "part of the scope could not be read",
        Basis.STALE_STATUS: "its status lags behind its specification",
        Basis.NOT_SERVED: "the cluster does not serve this API",
        Basis.OPERATION_UNSUPPORTED: "the API does not allow this read",
        Basis.NO_KNOWN_VERSION: "the cluster serves only versions korvid cannot read",
        Basis.PROJECTION_UNSUPPORTED: "the API cannot return metadata only",
        Basis.PROVIDER_NOT_CONFIGURED: "no provider is configured",
        Basis.FEATURE_DISABLED: "the feature is disabled",
        Basis.FORBIDDEN: "your permissions do not allow this read",
        Basis.UNAUTHENTICATED: "the cluster did not accept your credentials",
        Basis.DISCOVERY_FORBIDDEN: "your permissions do not allow API discovery",
        Basis.THROTTLED: "the API server asked korvid to slow down",
        Basis.SERVER_ERROR: "the API server returned an error",
        Basis.UNEXPECTED_STATUS: "the API server returned an unexpected status",
        Basis.TRANSPORT: "the API server could not be reached",
        Basis.MALFORMED: "the response could not be parsed",
        Basis.DISCOVERY_FAILED: "API discovery failed",
        Basis.DISCOVERY_STALE: "API discovery is out of date",
    }
)

_UNITS = ((86400, "d"), (3600, "h"), (60, "m"), (1, "s"))


def basis_sentence(basis: Basis) -> str:
    """What `basis` means, as a sentence fragment a user can act on."""
    return BASIS_SENTENCES[basis]


def age_text(observed_at: datetime | None, now: datetime) -> str:
    """How long ago an observation was made, in its largest whole unit."""
    if observed_at is None:
        return "not observed"
    seconds = (now - observed_at).total_seconds()
    if seconds < 0:
        return "observed in the future"
    for size, unit in _UNITS:
        if seconds >= size:
            return f"observed {int(seconds // size)}{unit} ago"
    return "observed just now"


def _scope_text(scope: SourceScope) -> str:
    if scope.kind is ScopeKind.CLUSTER:
        return "cluster-wide"
    if scope.kind is ScopeKind.NAMESPACE:
        return f"in namespace {scope.name}"
    if scope.kind is ScopeKind.NODE:
        return f"on node {scope.name}"
    return f"from provider {scope.name}"


def source_text(source: Source) -> str:
    """What a source reads, and where."""
    if isinstance(source, ApiSource):
        api = source.group or "core"
        if source.version:
            api = f"{source.group}/{source.version}" if source.group else source.version
        return f"{source.resource} {api} {_scope_text(source.scope)}"
    if isinstance(source, DiscoverySource):
        return f"discovery {source.path} {_scope_text(source.scope)}"
    return f"{source.provider_id} {source.query_kind} {_scope_text(source.scope)}"


def observation_line(observation: Observation, policy: FreshnessPolicy, now: datetime) -> Text:
    """One source's line: what it read, its state and why, and how old it is.

    `policy` is the source's own, the one its declaration and `conclusive`
    use, so the line never calls fresh what the baseline treats as stale.
    """
    line = (
        f"{source_text(observation.source)}: {observation.state.value} — "
        f"{basis_sentence(observation.basis)} · {age_text(observation.observed_at, now)}"
    )
    if observation.observed_at is not None and not is_fresh(observation, policy, now):
        line = f"{line} · stale"
    return Text(line)


def not_established(what: str) -> Text:
    """A conclusion the evidence does not support, said as such."""
    return Text(f"Not established: {what}")
```

- [ ] **Step 4: Run the tests, and check the mutations**

Expected: `22 passed`. Each change must fail a test: mark stale without an observation; never mark stale; parse the line as markup; drop the group from a grouped version; use `>` for unit boundaries.

- [ ] **Step 5: Commit**

```bash
git commit -m "ui: render evidence observations with one shared renderer"
```

### Task 3: The view

**Files:**
- Create: `src/korvid/ui/widgets/config_propagation_screen.py`
- Test: `tests/ui/test_config_propagation_screen.py`

**Interfaces:**
- Consumes: `PropagationReport`, `declarations` (#503); `observation_line`, `not_established`, `basis_sentence` (Task 2).
- Produces: `Heading`, `ConsumerRow`, `table_rows(consumers)`, `evidence_lines(report, now)`, `report_notes(report, now)`, `ConfigPropagationScreen(report, clock, *, refresh_seconds=5.0)`, a `ModalScreen[Consumer | None]` that re-renders its evidence lines and notes against `clock` while open, so aged evidence is marked stale and stops proving absence.

- [ ] **Step 1: Write the failing tests**

Create `tests/ui/test_config_propagation_screen.py` with:

```python
"""The configuration propagation view (#410): rows, notes and keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rich.text import Text
from textual.app import App
from textual.widgets import DataTable, Static

from korvid.core.capability import (
    ApiSource,
    Baseline,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.config_consumers import (
    Consumer,
    ConsumptionMode,
    ContainerCategory,
    PodIdentity,
)
from korvid.core.config_propagation import ConfigTarget, PropagationReport
from korvid.ui.widgets.config_propagation_screen import (
    ConfigPropagationScreen,
    ConsumerRow,
    Heading,
    evidence_lines,
    report_notes,
    table_rows,
)

from .waits import screen_ready, until

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
TARGET = ConfigTarget("ConfigMap", "team-a", "settings", "uid-settings")
SCOPE = SourceScope.namespace("team-a")
OBJECT = ApiSource(
    "",
    "configmaps",
    SCOPE,
    "v1",
    Verb.GET,
    projection=Projection.METADATA,
    name="settings",
    expected_uid="uid-settings",
)
PODS = ApiSource("", "pods", SCOPE, "v1", Verb.LIST)
POD = PodIdentity("team-a", "api-1", "uid-api-1", "ReplicaSet", "api-7d9f")


def _seen(
    source: ApiSource, basis: Basis = Basis.OBSERVED, observed_uid: str | None = None
) -> Observation:
    state = {
        Basis.OBSERVED: EvidenceState.AVAILABLE,
        Basis.OBJECT_ABSENT: EvidenceState.AVAILABLE,
        Basis.TRUNCATED: EvidenceState.PARTIAL,
        Basis.FORBIDDEN: EvidenceState.DENIED,
    }[basis]
    return Observation(source, state, basis, NOW - timedelta(seconds=2), observed_uid=observed_uid)


def _report(
    *consumers: Consumer,
    object_basis: Basis = Basis.OBSERVED,
    pods_basis: Basis = Basis.OBSERVED,
    observed_uid: str | None = "uid-settings",
) -> PropagationReport:
    uid = observed_uid if object_basis is Basis.OBSERVED else None
    return PropagationReport(
        TARGET,
        _seen(OBJECT, object_basis, uid),
        _seen(PODS, pods_basis),
        consumers,
        Baseline(EvidenceState.AVAILABLE),
    )


ENV = Consumer(
    POD,
    ConsumptionMode.ENV_FROM,
    "spec.containers[0].envFrom[0].configMapRef",
    False,
    "app",
    ContainerCategory.CONTAINER,
)
KEY = Consumer(
    POD,
    ConsumptionMode.ENV_KEY,
    "spec.containers[0].env[1].valueFrom.configMapKeyRef",
    True,
    "app",
    ContainerCategory.CONTAINER,
    env_name="LOG_LEVEL",
    key="log-level",
)
VOLUME = Consumer(
    POD,
    ConsumptionMode.VOLUME,
    "spec.volumes[0].configMap",
    False,
    "app",
    ContainerCategory.CONTAINER,
    volume="config",
    mount_path="/etc/app",
)
SUBPATH = Consumer(
    POD,
    ConsumptionMode.SUBPATH,
    "spec.volumes[0].configMap",
    False,
    "setup",
    ContainerCategory.INIT,
    volume="config",
    mount_path="/etc/app.yaml",
    sub_path="app.yaml",
)
UNMOUNTED = Consumer(POD, ConsumptionMode.UNMOUNTED, "spec.volumes[1].configMap", volume="spare")


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain for line in lines]


def test_rows_are_grouped_by_mode_under_one_heading_each() -> None:
    rows = table_rows((VOLUME, ENV, SUBPATH, KEY))

    assert [type(row).__name__ for row in rows] == [
        "Heading",
        "ConsumerRow",
        "ConsumerRow",
        "Heading",
        "ConsumerRow",
        "Heading",
        "ConsumerRow",
    ]
    headings = [row.text for row in rows if isinstance(row, Heading)]
    assert headings[0].startswith("Environment variables — read when the container starts")
    assert headings[1].startswith("Volumes — updated in place by the kubelet, eventually")
    assert "unless the object is immutable" in headings[1]
    assert headings[2].startswith("subPath mounts — never updated")
    assert [row.consumer for row in rows if isinstance(row, ConsumerRow)] == [
        ENV,
        KEY,
        VOLUME,
        SUBPATH,
    ]


def test_a_row_names_the_pod_container_and_reference_but_no_value() -> None:
    rows = {
        row.consumer: row.cells
        for row in table_rows((KEY, VOLUME, SUBPATH, UNMOUNTED))
        if isinstance(row, ConsumerRow)
    }

    assert rows[KEY] == ("api-1", "app", "env[1] LOG_LEVEL ← key log-level (optional)")
    assert rows[VOLUME] == ("api-1", "app", "volume config → /etc/app")
    assert rows[SUBPATH] == (
        "api-1",
        "setup (init container)",
        "volume config → /etc/app.yaml (subPath app.yaml)",
    )
    assert rows[UNMOUNTED] == ("api-1", "— (Pod)", "volume spare")


def test_the_evidence_lines_come_from_the_shared_renderer() -> None:
    lines = _plain(evidence_lines(_report(ENV), NOW))

    assert lines == [
        "configmaps v1 in namespace team-a: available — read · observed 2s ago",
        "pods v1 in namespace team-a: available — read · observed 2s ago",
    ]


def test_no_consumer_is_said_only_when_the_pod_list_is_conclusive() -> None:
    complete = _plain(report_notes(_report(), NOW))
    truncated = _plain(report_notes(_report(pods_basis=Basis.TRUNCATED), NOW))
    denied = _plain(report_notes(_report(pods_basis=Basis.FORBIDDEN), NOW))

    assert any(
        n.startswith("No Pod in namespace team-a references this ConfigMap") for n in complete
    )
    (none,) = [n for n in complete if n.startswith("No Pod")]
    assert "configMap, secret or projected volumes" in none
    assert "CSI and other volume plugins are not checked" in none
    assert not any(n.startswith("No Pod") for n in truncated + denied)
    assert (
        "Not established: whether every consumer is listed: stopped at the page limit" in truncated
    )
    assert (
        "Not established: whether every consumer is listed: your permissions do not allow this read"
    ) in denied


def test_immutability_is_always_not_established() -> None:
    notes = _plain(report_notes(_report(ENV), NOW))

    assert (
        "Not established: whether the ConfigMap is immutable (only its metadata is read)" in notes
    )


def test_a_gone_or_recreated_object_is_said() -> None:
    gone = _plain(report_notes(_report(ENV, object_basis=Basis.OBJECT_ABSENT), NOW))
    recreated = _plain(report_notes(_report(VOLUME, observed_uid="uid-new"), NOW))

    assert any("no longer exists" in n for n in gone)
    assert any("was recreated" in n and "volume mounts" in n for n in recreated)
    assert not any("was recreated" in n for n in _plain(report_notes(_report(VOLUME), NOW)))


def test_an_absence_that_has_aged_is_no_longer_called_a_deletion() -> None:
    report = _report(ENV, object_basis=Basis.OBJECT_ABSENT)

    fresh = _plain(report_notes(report, NOW))
    aged = _plain(report_notes(report, NOW + timedelta(minutes=5)))

    assert any("no longer exists" in n for n in fresh)
    assert not any("no longer exists" in n for n in aged)
    assert any(
        "did not exist when it was read" in n and "may have been created since" in n for n in aged
    )


def test_an_ephemeral_container_row_explains_a_restart_will_not_help() -> None:
    debugger = Consumer(
        POD,
        ConsumptionMode.ENV_FROM,
        "spec.ephemeralContainers[0].envFrom[0].configMapRef",
        False,
        "debugger",
        ContainerCategory.EPHEMERAL,
    )

    mounted = Consumer(
        POD,
        ConsumptionMode.VOLUME,
        "spec.volumes[0].configMap",
        False,
        "debugger",
        ContainerCategory.EPHEMERAL,
        volume="config",
        mount_path="/etc/app",
    )

    with_ephemeral = _plain(report_notes(_report(debugger), NOW))
    without = _plain(report_notes(_report(ENV), NOW)) + _plain(report_notes(_report(mounted), NOW))

    (note,) = [n for n in with_ephemeral if n.startswith("An ephemeral container")]
    assert "cannot be restarted" in note
    assert "add a new ephemeral container" in note
    assert not any(n.startswith("An ephemeral container") for n in without)


def test_an_init_container_row_explains_when_it_runs() -> None:
    with_init = _plain(report_notes(_report(SUBPATH), NOW))
    without = _plain(report_notes(_report(ENV), NOW))

    assert any(n.startswith("An init container runs to completion") for n in with_init)
    assert not any(n.startswith("An init container") for n in without)


class _Host(App[None]):
    def __init__(self, screen: ConfigPropagationScreen) -> None:
        super().__init__()
        self.prompt = screen
        self.result: list[Consumer | None] = []

    def on_mount(self) -> None:
        self.push_screen(self.prompt, self.result.append)


async def test_enter_on_a_consumer_row_returns_that_consumer() -> None:
    app = _Host(ConfigPropagationScreen(_report(ENV, VOLUME), lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        table = app.screen.query_one(DataTable)
        assert table.cursor_row == 1
        await pilot.press("down", "down", "enter")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [VOLUME]


async def test_enter_on_a_heading_does_nothing_and_escape_closes() -> None:
    app = _Host(ConfigPropagationScreen(_report(ENV), lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        await pilot.press("up", "enter")
        await pilot.pause()
        assert app.result == []
        await pilot.press("escape")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [None]


async def test_the_title_shows_cluster_names_literally() -> None:
    hostile = ConfigTarget("ConfigMap", "team-a", "[bold]settings[/bold]")
    report = PropagationReport(
        hostile,
        _seen(OBJECT, observed_uid="uid-settings"),
        _seen(PODS),
        (),
        Baseline(EvidenceState.AVAILABLE),
    )
    app = _Host(ConfigPropagationScreen(report, lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        title = app.screen.query_one("#propagation-title", Static)
        assert "[bold]settings[/bold]" in str(title.render())


def test_a_list_that_has_aged_past_its_policy_no_longer_proves_absence() -> None:
    later = NOW + timedelta(minutes=5)

    notes = _plain(report_notes(_report(), later))

    assert not any(n.startswith("No Pod") for n in notes)
    assert "Not established: whether every consumer is listed: the Pod list is stale" in notes


async def test_an_open_view_ages_its_evidence_without_reading_again() -> None:
    now = [NOW]
    app = _Host(ConfigPropagationScreen(_report(), lambda: now[0], refresh_seconds=0.05))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        evidence = app.screen.query_one("#propagation-evidence", Static)
        notes = app.screen.query_one("#propagation-notes", Static)
        assert "stale" not in str(evidence.render())
        assert "No Pod in namespace team-a" in str(notes.render())

        now[0] = NOW + timedelta(minutes=5)
        await until(pilot, lambda: "· stale" in str(evidence.render()), label="evidence aged")

        assert "observed 5m ago" in str(evidence.render())
        assert "No Pod in namespace team-a" not in str(notes.render())
        assert "the Pod list is stale" in str(notes.render())
```

- [ ] **Step 2: Run them to verify they fail**

Expected: `ModuleNotFoundError` for the screen module.

- [ ] **Step 3: Write the screen**

Create `src/korvid/ui/widgets/config_propagation_screen.py` with:

```python
"""Which Pods consume a ConfigMap or Secret, and what a change means for each (#410).

`ConfigPropagationScreen` shows one `PropagationReport`: a line per evidence
source from the shared renderer, then the consumers grouped by how they
consume the object, then what the evidence does not establish. It performs
no I/O, but re-renders the evidence lines and notes against its clock while
it is open, so evidence that ages past its policy is marked stale and stops
proving that no Pod references the object. Enter on a consumer row dismisses with that `Consumer`, so the
caller can open its Pod; Escape dismisses with `None`.

Every cell is a plain `Text`, so a cluster-provided name containing markup
is shown literally.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.capability import EvidenceState, is_fresh
from korvid.core.config_consumers import Consumer, ConsumptionMode, ContainerCategory
from korvid.core.config_propagation import PropagationReport, declarations
from korvid.ui.evidence_text import basis_sentence, not_established, observation_line

#: The sections, in display order, with what a change means for each.
_SECTIONS: tuple[tuple[frozenset[ConsumptionMode], str], ...] = (
    (
        frozenset({ConsumptionMode.ENV_FROM, ConsumptionMode.ENV_KEY}),
        "Environment variables — read when the container starts; a change needs a "
        "rollout restart, which asks for your approval",
    ),
    (
        frozenset({ConsumptionMode.VOLUME}),
        "Volumes — updated in place by the kubelet, eventually, unless the object is "
        "immutable; whether the application rereads them is not verified",
    ),
    (
        frozenset({ConsumptionMode.SUBPATH}),
        "subPath mounts — never updated; a change needs a rollout restart, which asks "
        "for your approval",
    ),
    (
        frozenset({ConsumptionMode.UNMOUNTED}),
        "Unmounted volumes — no container mounts them, so nothing reads them",
    ),
    (
        frozenset({ConsumptionMode.IMAGE_PULL}),
        "Image pull secrets — used only when an image is pulled",
    ),
)
_COLUMNS = ("Pod", "Container", "Reference")
_IN_CONTAINER = re.compile(r"^spec\.(?:containers|initContainers|ephemeralContainers)\[\d+\]\.")
_INIT_NOTE = (
    "An init container runs to completion before the other containers start; once it "
    "has finished, a change reaches it only when the Pod restarts."
)
_EPHEMERAL_NOTE = (
    "An ephemeral container cannot be restarted, and a rollout restart creates Pods "
    "without it: to read a change to its environment or subPath mount, add a new "
    "ephemeral container."
)
#: The modes an ephemeral container cannot pick a change up through.
_READ_AT_START = frozenset(
    {ConsumptionMode.ENV_FROM, ConsumptionMode.ENV_KEY, ConsumptionMode.SUBPATH}
)


@dataclass(frozen=True, slots=True)
class Heading:
    """A section heading: the mode, then what a change means for it."""

    text: str


@dataclass(frozen=True, slots=True)
class ConsumerRow:
    """One consumer and its Pod, Container and Reference cells."""

    consumer: Consumer
    cells: tuple[str, str, str]


TableRow = Heading | ConsumerRow


def _container_cell(consumer: Consumer) -> str:
    if consumer.category is None:
        return "— (Pod)"
    name = consumer.container or "?"
    if consumer.category is ContainerCategory.CONTAINER:
        return name
    return f"{name} ({consumer.category.value})"


def _volume_text(consumer: Consumer) -> str:
    volume = f"volume {consumer.volume or '(unnamed)'}"
    if consumer.mode is ConsumptionMode.UNMOUNTED:
        return volume
    text = f"{volume} → {consumer.mount_path or '?'}"
    if consumer.sub_path is not None:
        text = f"{text} (subPath {consumer.sub_path})"
    return text


def _reference_cell(consumer: Consumer) -> str:
    relative = _IN_CONTAINER.sub("", consumer.path)
    if consumer.mode is ConsumptionMode.ENV_KEY:
        variable = relative.split(".valueFrom", 1)[0]
        text = f"{variable} {consumer.env_name or '?'} ← key {consumer.key or '?'}"
    elif consumer.mode is ConsumptionMode.ENV_FROM:
        text = relative
    elif consumer.mode is ConsumptionMode.IMAGE_PULL:
        text = consumer.path.removeprefix("spec.")
    else:
        text = _volume_text(consumer)
    return f"{text} (optional)" if consumer.optional else text


def table_rows(consumers: Sequence[Consumer]) -> list[TableRow]:
    """The consumers under their section headings; an empty section is left out."""
    rows: list[TableRow] = []
    for modes, heading in _SECTIONS:
        section = [consumer for consumer in consumers if consumer.mode in modes]
        if section:
            rows.append(Heading(heading))
            rows += (
                ConsumerRow(c, (c.pod.name, _container_cell(c), _reference_cell(c)))
                for c in section
            )
    return rows


def evidence_lines(report: PropagationReport, now: datetime) -> list[Text]:
    """The object's and the Pod list's lines, each judged by its own policy."""
    policies = {declaration.key: declaration.policy for declaration in declarations(report.target)}
    return [
        observation_line(report.object, policies["config-object"], now),
        observation_line(report.pods, policies["pods"], now),
    ]


def _object_notes(report: PropagationReport, now: datetime) -> list[Text]:
    kind = report.target.kind
    if report.object_absent:
        if not is_fresh(report.object, declarations(report.target)[0].policy, now):
            return [
                Text(f"The {kind} did not exist when it was read; it may have been created since.")
            ]
        tail = " The Pods below still reference it." if report.consumers else ""
        return [Text(f"The {kind} no longer exists.{tail}")]
    if report.recreated:
        return [
            Text(
                f"The {kind} was recreated since it was selected: only the new one's "
                "evidence is shown, and whether existing volume mounts see it is "
                "not established."
            )
        ]
    return []


def _completeness_note(report: PropagationReport, now: datetime) -> Text | None:
    target = report.target
    if report.consumers_conclusive(now):
        if report.consumers:
            return None
        return Text(
            f"No Pod in namespace {target.namespace} references this {target.kind} through "
            "envFrom, env valueFrom, configMap, secret or projected volumes, or "
            "imagePullSecrets. CSI and other volume plugins are not checked."
        )
    pods = report.pods
    stale = pods.state is EvidenceState.AVAILABLE and not is_fresh(
        pods, declarations(target)[1].policy, now
    )
    reason = "the Pod list is stale" if stale else basis_sentence(pods.basis)
    return not_established(f"whether every consumer is listed: {reason}")


def report_notes(report: PropagationReport, now: datetime) -> list[Text]:
    """What the evidence says about the object, and what it does not establish."""
    notes = _object_notes(report, now)
    if any(consumer.category is ContainerCategory.INIT for consumer in report.consumers):
        notes.append(Text(_INIT_NOTE))
    if any(
        consumer.category is ContainerCategory.EPHEMERAL and consumer.mode in _READ_AT_START
        for consumer in report.consumers
    ):
        notes.append(Text(_EPHEMERAL_NOTE))
    completeness = _completeness_note(report, now)
    if completeness is not None:
        notes.append(completeness)
    notes.append(
        not_established(
            f"whether the {report.target.kind} is immutable (only its metadata is read)"
        )
    )
    return notes


class ConfigPropagationScreen(ModalScreen[Consumer | None]):
    """One ConfigMap's or Secret's consumers, by mode, with their evidence."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ConfigPropagationScreen {
        layout: vertical;
        background: $background;
    }
    ConfigPropagationScreen #propagation-title {
        padding: 0 1;
        text-style: bold;
    }
    ConfigPropagationScreen #propagation-evidence, ConfigPropagationScreen #propagation-notes {
        padding: 0 1;
        color: $text-muted;
    }
    ConfigPropagationScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        report: PropagationReport,
        clock: Callable[[], datetime],
        *,
        refresh_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self.report = report
        self._clock = clock
        self._refresh_seconds = refresh_seconds
        self._rows: dict[str, Consumer] = {}

    def compose(self) -> ComposeResult:
        target = self.report.target
        yield Footer()
        yield Static(
            f"{target.kind} {target.namespace}/{target.name}",
            id="propagation-title",
            markup=False,
        )
        yield Static(id="propagation-evidence")
        yield DataTable[Text](id="propagation-table")
        yield Static(id="propagation-notes")

    def _render_text(self) -> None:
        now = self._clock()
        evidence = Text("\n").join(evidence_lines(self.report, now))
        self.query_one("#propagation-evidence", Static).update(evidence)
        notes = Text("\n").join(report_notes(self.report, now))
        self.query_one("#propagation-notes", Static).update(notes)

    def on_mount(self) -> None:
        self._render_text()
        self.set_interval(self._refresh_seconds, self._render_text)
        table: DataTable[Text] = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        first: int | None = None
        for index, row in enumerate(table_rows(self.report.consumers)):
            if isinstance(row, Heading):
                title, _, meaning = row.text.partition(" — ")
                table.add_row(Text(title, style="bold"), Text(""), Text(meaning), key=f"h{index}")
                continue
            key = f"r{index}"
            self._rows[key] = row.consumer
            table.add_row(*(Text(cell) for cell in row.cells), key=key)
            first = index if first is None else first
        if first is not None:
            table.move_cursor(row=first)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a consumer row returns it; a heading does nothing."""
        consumer = self._rows.get(str(event.row_key.value))
        if consumer is not None:
            self.dismiss(consumer)

    def action_close(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run the tests, and check the mutations**

Expected: `14 passed`. Each change must fail a test: show empty sections; treat any Pod list as conclusive; dismiss on a heading; always add the init note; always call the object recreated; render the title as markup.

- [ ] **Step 5: Commit**

```bash
git commit -m "ui: show a ConfigMap's or Secret's consumers by mode"
```

### Task 4: The client boundary and the controller

**Files:**
- Modify: `src/korvid/k8s/evidence_requests.py`, `src/korvid/k8s/client.py`
- Create: `src/korvid/ui/config_propagation_controller.py`
- Test: `tests/k8s/test_evidence_requests.py`, `tests/ui/test_config_propagation_controller.py`

**Interfaces:**
- Produces: `EvidenceClient(EvidenceReader)` with `session_generation` and `discovery_snapshot()`; `CONFIG_PROPAGATION_GROUP`; `ConfigPropagationController(*, ui, view, context, client, navigate, clock)` with `unavailable_reason()`, `open_selected()`, `cancel_workers()`.

- [ ] **Step 1: Name the client boundary**

In `src/korvid/k8s/evidence_requests.py`, after `EvidenceReader`:

```python
class EvidenceClient(EvidenceReader):
    """What a diagnostic needs of its client: evidence reads, discovery and the session.

    `KubeClient` is the implementation. A diagnostic resolves its sources from
    `discovery_snapshot()` and records `session_generation` in its run token,
    so a result read under other credentials is dropped.
    """

    @property
    @abstractmethod
    def session_generation(self) -> int:
        """An opaque counter that changes on every connect and context switch."""

    @abstractmethod
    async def discovery_snapshot(self) -> RawDiscovery:
        """This session's raw discovery."""
```

`KubeClient` then derives from `evidence_requests.EvidenceClient` instead of `EvidenceReader`. A test asserts `issubclass(KubeClient, EvidenceClient)`.

- [ ] **Step 2: Write the controller's failing tests**

Create `tests/ui/test_config_propagation_controller.py` with:

```python
"""The configuration propagation controller: availability, runs and late results (#410)."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from typing import Any

from korvid.core.config_consumers import ConsumptionMode, PodIdentity
from korvid.core.config_propagation import ConfigTarget
from korvid.core.evidence_reads import DEFAULT_MAX_BYTES, LIST_PAGE_SIZE, MAX_BYTES
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.ui.action_availability import AvailabilityCode
from korvid.ui.config_propagation_controller import (
    CONFIG_PROPAGATION_GROUP,
    ConfigPropagationController,
)
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.config_propagation_screen import ConfigPropagationScreen
from korvid.ui.workspace_controller import ContextGuard

from .test_session_timeline_controller import FakeUiSurface

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
CONFIG_MAPS = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
SECRETS = ResourceMeta("Secret", "secrets", "", "v1", True)
PODS = ResourceMeta("Pod", "pods", "", "v1", True)
FOREIGN = ResourceMeta("ConfigMap", "configmaps", "vendor.example.com", "v1", True)
SETTINGS_POD = {
    "metadata": {"name": "api-1", "namespace": "team-a", "uid": "uid-api-1"},
    "spec": {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "settings"}}]}]},
}


class FakeView(ViewState):
    def __init__(self, kind: str = "configmaps", selected: tuple[str, str] | None = None) -> None:
        self.kind = kind
        self.scope = "team-a"
        self.selected = selected
        self.alias_map = {
            "configmaps": CONFIG_MAPS,
            "secrets": SECRETS,
            "pods": PODS,
            "configmaps.vendor.example.com": FOREIGN,
        }

    def current_kind(self) -> str:
        return self.kind

    def canonical_kind(self, kind: str) -> str:
        return kind

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return self.alias_map

    def selected_ns_name(self, *, notify: bool = True) -> tuple[str | None, str | None]:
        return self.selected if self.selected is not None else (None, None)

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return f"uid-{name}"

    def current_scope(self) -> str:
        return self.scope

    def resources(self, kind: str, scope: str) -> list[Any]:
        return []  # pragma: no cover

    def readonly(self) -> bool:
        return False  # pragma: no cover

    def default_namespace(self) -> str | None:
        return None  # pragma: no cover

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural  # pragma: no cover

    def write_locus(self, namespace: str | None) -> str:
        return ""  # pragma: no cover


class FakeContext(ContextGuard):
    def __init__(self) -> None:
        self.current = 1
        self.in_switch = False
        self.allowed = True

    def epoch(self) -> int:
        return self.current

    def switching(self) -> bool:
        return self.in_switch

    def reads_allowed(self) -> bool:
        return self.allowed

    def crossed(self, epoch: int) -> bool:
        return self.in_switch or epoch != self.current


class FakeClient(EvidenceClient):
    def __init__(self) -> None:
        self.generation = 3
        self.gets: list[tuple[EvidenceTarget, str, int]] = []
        self.lists: list[tuple[EvidenceTarget, int, str, int]] = []

    @property
    def session_generation(self) -> int:
        return self.generation

    async def discovery_snapshot(self) -> RawDiscovery:
        served = tuple(
            ServedResource(name, frozenset({"get", "list"}))
            for name in ("configmaps", "secrets", "pods")
        )
        return RawDiscovery(
            self.generation,
            NOW,
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", ()),
            (GroupVersionOutcome("", "v1", served),),
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, continuation, max_bytes))
        return EvidencePage((SETTINGS_POD,))

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name, max_bytes))
        return {"metadata": {"name": name, "namespace": "team-a", "uid": f"uid-{name}"}}


class Harness:
    def __init__(self, *, client: FakeClient | None = None, view: FakeView | None = None) -> None:
        self.ui = FakeUiSurface(close_worker_coroutines=False)
        self.view = view or FakeView(selected=("team-a", "settings"))
        self.context = FakeContext()
        self.client = FakeClient() if client is None else client
        self.navigations: list[tuple[str, str, int, str | None]] = []
        self.controller = ConfigPropagationController(
            ui=self.ui,
            view=self.view,
            context=self.context,
            client=self.client,
            navigate=self._navigate,
            clock=lambda: NOW,
        )

    async def _navigate(self, namespace: str, name: str, epoch: int, uid: str | None) -> None:
        self.navigations.append((namespace, name, epoch, uid))

    async def run(self, index: int = -1) -> None:
        work = self.ui.workers[index]
        assert isinstance(work, Awaitable)
        await work


def _code(harness: Harness) -> AvailabilityCode | None:
    reason = harness.controller.unavailable_reason()
    return None if reason is None else reason.code


def test_it_is_available_on_a_selected_config_map_or_secret() -> None:
    assert _code(Harness()) is None
    assert _code(Harness(view=FakeView("secrets", ("team-a", "creds")))) is None


def test_it_says_why_it_is_unavailable() -> None:
    unconnected = ConfigPropagationController(
        ui=FakeUiSurface(),
        view=FakeView(selected=("team-a", "settings")),
        context=FakeContext(),
        client=None,
        navigate=Harness()._navigate,
    )

    reason = unconnected.unavailable_reason()
    assert reason is not None
    assert reason.code is AvailabilityCode.MISSING_CAPABILITY
    assert _code(Harness(view=FakeView("pods", ("team-a", "api-1")))) is AvailabilityCode.WRONG_VIEW
    assert (
        _code(Harness(view=FakeView("configmaps.vendor.example.com", ("team-a", "x"))))
        is AvailabilityCode.WRONG_VIEW
    )
    assert _code(Harness(view=FakeView("configmaps", None))) is AvailabilityCode.NO_SELECTION


async def test_opening_reads_the_selected_object_and_shows_its_consumers() -> None:
    harness = Harness()

    harness.controller.open_selected()
    await harness.run()

    assert harness.ui.worker_groups == [(CONFIG_PROPAGATION_GROUP, False)]
    (screen,) = harness.ui.screens
    assert isinstance(screen, ConfigPropagationScreen)
    assert screen.report.target == ConfigTarget("ConfigMap", "team-a", "settings", "uid-settings")
    assert [(c.pod, c.mode) for c in screen.report.consumers] == [
        (PodIdentity("team-a", "api-1", "uid-api-1"), ConsumptionMode.ENV_FROM)
    ]
    assert harness.client.gets == [
        (
            EvidenceTarget("", "v1", "configmaps", "team-a", metadata_only=True),
            "settings",
            DEFAULT_MAX_BYTES,
        )
    ]
    assert harness.client.lists == [
        (EvidenceTarget("", "v1", "pods", "team-a"), LIST_PAGE_SIZE, "", MAX_BYTES)
    ]


def test_opening_where_it_is_unavailable_says_why_and_reads_nothing() -> None:
    harness = Harness(view=FakeView("pods", ("team-a", "api-1")))

    harness.controller.open_selected()

    assert harness.ui.workers == []
    assert [n.severity for n in harness.ui.notifications] == ["warning"]


def test_opening_while_reads_are_held_reads_nothing() -> None:
    harness = Harness()
    harness.context.allowed = False

    harness.controller.open_selected()

    assert harness.ui.workers == []


async def test_a_newer_run_drops_an_older_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.controller.open_selected()
    await harness.run(0)

    assert harness.ui.screens == []
    await harness.run(1)
    assert len(harness.ui.screens) == 1


async def test_a_context_switch_drops_the_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.context.current = 2
    await harness.run()

    assert harness.ui.screens == []


async def test_new_credentials_drop_the_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.client.generation = 4
    await harness.run()

    assert harness.ui.screens == []


async def test_choosing_a_consumer_opens_its_pod_in_the_run_epoch() -> None:
    harness = Harness()
    harness.controller.open_selected()
    await harness.run()
    (screen,) = harness.ui.screens
    assert isinstance(screen, ConfigPropagationScreen)
    (consumer,) = screen.report.consumers

    assert harness.ui.screen_callback is not None
    harness.ui.screen_callback(None)
    assert len(harness.ui.workers) == 1
    harness.context.current = 2
    harness.ui.screen_callback(consumer)
    await harness.run()

    assert harness.navigations == [("team-a", "api-1", 1, "uid-api-1")]


async def test_a_dialog_opened_during_the_reads_keeps_the_view_closed() -> None:
    harness = Harness()
    harness.controller.open_selected()
    harness.ui.screen_depth = lambda: 2  # type: ignore[method-assign]  # a dialog opened

    await harness.run()

    assert harness.ui.screens == []


async def test_leaving_the_view_or_the_selection_keeps_the_result_closed() -> None:
    moved = Harness()
    rescoped = Harness()
    reselected = Harness()

    for harness in (moved, rescoped, reselected):
        harness.controller.open_selected()
    moved.view.kind = "pods"
    rescoped.view.scope = "team-b"
    reselected.view.selected = ("team-a", "other")
    for harness in (moved, rescoped, reselected):
        await harness.run()

    assert (moved.ui.screens, rescoped.ui.screens, reselected.ui.screens) == ([], [], [])


async def test_the_worker_group_is_cancelled_on_request() -> None:
    harness = Harness()

    await harness.controller.cancel_workers()

    assert harness.ui.cancelled_groups == [CONFIG_PROPAGATION_GROUP]
```

- [ ] **Step 3: Write the controller**

Create `src/korvid/ui/config_propagation_controller.py` with:

```python
"""Open the configuration propagation view for the selected ConfigMap or Secret (#410).

One exclusive worker reads the object's metadata and its namespace's Pods
through `explain_propagation`, under a run token. The view opens only while
that token is current: a context switch, new credentials or a newer run
drops the result, as `RunTracker` decides. So does another dialog that is
open when the reads finish, or a change of view, scope or selection since
they started, as the relationship view does. The context-switch coordinator
cancels the worker group before it swaps the client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from korvid.core.api_versions import observe_discovery
from korvid.core.capability import RunToken
from korvid.core.config_consumers import Consumer
from korvid.core.config_propagation import ConfigTarget, explain_propagation
from korvid.core.run_tokens import RunTracker
from korvid.k8s.evidence_requests import EvidenceClient
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.config_propagation_screen import ConfigPropagationScreen
from korvid.ui.workspace_controller import ContextGuard

CONFIG_PROPAGATION_GROUP = "config-propagation"
_KINDS = frozenset({"ConfigMap", "Secret"})
_UNCONNECTED = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Configuration propagation needs a cluster connection"
)
_WRONG_VIEW = UnavailableReason(
    AvailabilityCode.WRONG_VIEW, "Open ConfigMaps or Secrets to explain their propagation"
)
_NO_SELECTION = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a ConfigMap or Secret")

#: Opens a consumer's Pod: (namespace, name, epoch, expected UID).
PodNavigator = Callable[[str, str, int, str | None], Awaitable[None]]
#: The view, scope and (namespace, name) selection a run was started from.
_Origin = tuple[str, str, tuple[str | None, str | None]]


def _now() -> datetime:
    return datetime.now(UTC)


class ConfigPropagationController:
    """Runs the propagation diagnostic and shows its result while it is current."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        client: EvidenceClient | None,
        navigate: PodNavigator,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._ui = ui
        self._view = view
        self._context = context
        self._client = client
        self._navigate = navigate
        self._clock = clock
        self._tracker = RunTracker(
            epoch=context.epoch,
            session_generation=lambda: client.session_generation if client is not None else 0,
            switching=context.switching,
        )

    def _selected_kind(self) -> str | None:
        meta = self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
        if meta is None or meta.group or meta.kind not in _KINDS:
            return None
        return meta.kind

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why the view cannot open now, or None.

        Only for a missing connection, the wrong view or no selection: a
        source the cluster refuses never disables it, the view says so.
        """
        if self._client is None:
            return _UNCONNECTED
        if self._selected_kind() is None:
            return _WRONG_VIEW
        namespace, name = self._view.selected_ns_name(notify=False)
        if not namespace or not name:
            return _NO_SELECTION
        return None

    def open_selected(self) -> None:
        """Explain the selected ConfigMap or Secret in a worker."""
        reason = self.unavailable_reason()
        if reason is not None:
            self._ui.notify(reason.message, severity="warning", markup=False)
            return
        kind = self._selected_kind()
        namespace, name = self._view.selected_ns_name()
        if not self._context.reads_allowed() or kind is None or not namespace or not name:
            return
        target = ConfigTarget(kind, namespace, name, self._view.selected_uid(namespace, name))
        token = self._tracker.next_run()
        self._ui.run_worker(
            self._explain(target, token, self._context.epoch(), self._origin()),
            exclusive=True,
            group=CONFIG_PROPAGATION_GROUP,
            exit_on_error=False,
        )

    def _origin(self) -> _Origin:
        """The view, scope and selected row a run explains."""
        return (
            self._view.current_kind(),
            self._view.current_scope(),
            self._view.selected_ns_name(notify=False),
        )

    async def _explain(
        self, target: ConfigTarget, token: RunToken, epoch: int, origin: _Origin
    ) -> None:
        client = self._client
        if client is None:
            return
        snapshot = observe_discovery(await client.discovery_snapshot())
        report = await explain_propagation(client, snapshot, target, token=token, clock=self._clock)
        # Another dialog is open, or the user has moved on, as the reads finish.
        if self._ui.screen_depth() > 1 or self._origin() != origin:
            return
        screen = ConfigPropagationScreen(report, self._clock)
        self._tracker.apply_if_current(
            token,
            lambda: self._ui.push_screen(screen, lambda chosen: self._open_pod(chosen, epoch)),
        )

    def _open_pod(self, consumer: Consumer | None, epoch: int) -> None:
        if consumer is None:
            return
        pod = consumer.pod
        self._ui.run_worker(
            self._navigate(pod.namespace, pod.name, epoch, pod.uid),
            group=CONFIG_PROPAGATION_GROUP,
            exit_on_error=False,
        )

    async def cancel_workers(self) -> None:
        """Stop a run holding the old client, before a context switch swaps it."""
        await self._ui.cancel_workers(CONFIG_PROPAGATION_GROUP)
```

- [ ] **Step 4: Run the tests, and check the mutations**

Expected: `12 passed`. Each change must fail a test: accept a ConfigMap kind from another group; ignore an open dialog; apply a result without the run token; track a fixed session generation; navigate in the current epoch rather than the run's.

- [ ] **Step 5: Commit**

```bash
git commit -m "k8s: name what a diagnostic needs of its client"
git commit -m "ui: run the propagation diagnostic for the selected object"
```

### Task 5: The command and the wiring

**Files:**
- Modify: `src/korvid/ui/messages.py`, `src/korvid/ui/command.py`, `src/korvid/ui/command_router.py`, `src/korvid/ui/action_policy.py`, `src/korvid/ui/context_switch_coordinator.py`, `src/korvid/ui/app.py`, `src/korvid/ui/app_runtime.py`, `src/korvid/__main__.py`
- Test: `tests/ui/test_command_router.py`, `tests/ui/test_action_policy.py`, `tests/ui/test_context_switch_coordinator.py`, `tests/test_main_wiring.py`, `tests/ui/test_config_propagation_workflow.py`

- [ ] **Step 1: Write the failing tests**

- `test_command_router.py`: `BuiltinOperation.CONFIG_PROPAGATION` reaches a `FakePropagation.open_selected` once, and without an owner warns "Configuration propagation is unavailable in this session".
- `test_action_policy.py`: `compose_command_reasons(..., propagation=...)` answers `consumers` with the owner's reason, read live.
- `test_context_switch_coordinator.py`: `_SUCCESSFUL_ORDER` has `f"workers-cancelled:{CONFIG_PROPAGATION_GROUP}"` right after `"workspace-quiesce"`.
- `test_main_wiring.py`: `_wire_and_run` passes `evidence_client=kube` to `KorvidApp`.
- Create `tests/ui/test_config_propagation_workflow.py` with:

```python
```

- [ ] **Step 2: Add the operation and the command**

In `ui/messages.py`, `BuiltinOperation` gains `CONFIG_PROPAGATION = "consumers"`. In `ui/command.py`, before the `:q` descriptor:

```python
    CommandDescriptor(
        aliases=("consumers", "config-consumers"),
        help=(
            (
                ":consumers|:config-consumers",
                "ConfigMap or Secret consumers: Pods, mount modes, and what a change means",
            ),
        ),
        operation=BuiltinOperation.CONFIG_PROPAGATION,
        maximum_arguments=0,
        palette=PaletteCommand(
            "Explain configuration propagation",
            "consumers",
            ("propagation", "configmap", "secret", "mount", "subpath", "restart"),
        ),
    ),
```

The help text has no `h…e…l…m` subsequence: the palette also matches descriptions fuzzily, and a test pins that `helm` finds only the Helm rows.

- [ ] **Step 3: Route it, and answer its availability**

`ui/command_router.py` gains a `PropagationCommands` protocol (`open_selected()`), a `propagation` constructor argument, and `BuiltinOperation.CONFIG_PROPAGATION: self._open_propagation`, which warns when there is no owner. `compose_command_reasons` gains `propagation` and maps it to `"consumers"`.

- [ ] **Step 4: Cancel the group on a context switch**

In `ContextSwitchCoordinator._teardown`, right after `await self._workspace().quiesce_for_context_switch()`:

```python
        self._surface.cancel_worker_group(CONFIG_PROPAGATION_GROUP)
```

- [ ] **Step 5: Wire it**

`KorvidApp.__init__` and `AppRuntimeInputs` gain `evidence_client: EvidenceClient | None = None`. In `_construct_app_runtime`:

```python
    config_propagation = ConfigPropagationController(
        ui=AppUiSurface(app),
        view=view,
        context=context,
        client=inputs.evidence_client,
        navigate=lambda namespace, name, epoch, uid: workspace_ref.get().jump_to_object(
            "pods", namespace, name, epoch=epoch, expected_uid=uid
        ),
    )
```

and pass `propagation=config_propagation.unavailable_reason` to `compose_command_reasons`, `propagation=config_propagation` to `CommandRouter`, and `evidence_client=kube` to `KorvidApp` in `_wire_and_run`. The controller shows its unavailable reason with `markup=False`, as every owner does.

- [ ] **Step 6: Report a failed run**

`ui/worker_failures.py` maps each `exit_on_error=False` worker group to its failure label, `config-propagation` included, and `KorvidApp.on_worker_state_changed` looks the label up there instead of an `if`/`elif` chain. A workflow test makes discovery raise and expects "Configuration propagation failed" with the error. This also frees ten lines in `ui/app.py`.

- [ ] **Step 7: Run the UI suite and the size check**

Run: `uv run --frozen pytest -p no:tach -n 8 tests/ui/ tests/test_main_wiring.py -q` and `python scripts/check_source_size.py`
Expected: all pass, and `python scripts/check_source_size.py` passes.

- [ ] **Step 8: Commit**

```bash
git commit -m "ui: open the propagation view with :consumers"
```

### Task 6: Verify it on every supported version

**Files:**
- Modify: `tests/version_matrix/fixtures.yaml`, `tests/version_matrix/test_scenarios.py`, `.github/workflows/k8s-version-matrix.yml`, `docs/kubernetes-support.md`

- [ ] **Step 1: Add the consumer Pod**

After the `settings` ConfigMap in `fixtures.yaml`, a Pod `consumer` on `registry.k8s.io/pause:3.10` reads `settings` through `envFrom`, mounts it at `/etc/settings`, mounts its `password` key with `subPath`, and has a literal env value `matrix-pod-literal`.

- [ ] **Step 2: Add the scenario**

```python
async def test_a_config_maps_consumers_are_read_by_mode(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)

    report = await explain_propagation(
        kube, await snapshot_of(kube), ConfigTarget("ConfigMap", NAMESPACE, "settings")
    )

    assert _state(report.object) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert _state(report.pods) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert sorted((c.pod.name, c.container, c.mode) for c in report.consumers) == [
        ("consumer", "app", ConsumptionMode.ENV_FROM),
        ("consumer", "app", ConsumptionMode.SUBPATH),
        ("consumer", "app", ConsumptionMode.VOLUME),
    ]
    assert report.consumers_conclusive(datetime.now(UTC))
    assert "matrix-configmap-payload" not in repr(report)
    assert "matrix-pod-literal" not in repr(report)
```

- [ ] **Step 3: Cover the new modules in the workflow's path filters, and list the scenario in `docs/kubernetes-support.md`**

- [ ] **Step 4: Commit**

```bash
git commit -m "tests: verify ConfigMap consumers by mode on every supported version"
```

### Task 7: Document it

- [ ] **Step 1:** `docs/tui.md` gains a "Configuration propagation" section with the modes table; `docs/keybindings.md` lists `:consumers`; `docs/release-notes/unreleased.md` announces it.
- [ ] **Step 2:** Run `uv run --frozen mkdocs build --strict` and `uv run --frozen pytest -p no:tach tests/test_docs_*.py -q`; both pass.
- [ ] **Step 3: Commit**

```bash
git commit -m "docs: explain configuration propagation and :consumers"
```

### Task 8: Run the full gate

- [ ] **Step 1:** Run `make check`. Expected: ruff, format, mypy, tach, the source-size check and the test suite pass, with coverage at least 80%.
