# Context-aware Action Palette Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a searchable, context-aware `Ctrl-P` Action Palette that derives from Korvid's real bindings and typed commands, explains unavailable actions, and invokes existing routes without weakening approval safety.

**Architecture:** Extract the existing binding policy from the nearly capped `app.py`, then extend it with side-effect-free availability supplied by the controllers that own each guard. A pure palette model derives and ranks entries from `APP_BINDINGS` and `COMMANDS`; a dedicated Textual modal renders them. The app only opens the modal and dispatches its stable selection through `run_action` or `parse_command` after dismissal.

**Tech Stack:** Python 3.11+, Textual 8+, Rich `Text`, pytest/pytest-asyncio, Ruff, strict mypy, tach.

## Global Constraints

- Work in `/Users/hwang-inhwan/workspace/kube/.worktrees/issue-388-action-palette` on `feat/388-action-palette`.
- The branch is intentionally stacked on PR #397 head `1d88040f`; do not drop those commits before #397 lands.
- Do not add dependencies or modify `uv.lock`.
- Behind the corporate mirror, use `uv run --frozen`; if any tool rewrites `uv.lock`, restore only that generated lockfile change before committing.
- `APP_BINDINGS` and typed `COMMANDS` remain the only executable catalogs; do not add a palette-only action table.
- Resource-name navigation remains in `:` and never appears in palette results.
- Disable Textual's implicit system palette instead of subclassing its private methods.
- `Ctrl-P` is a remappable priority binding, but priority remaps to `y`, `n`, `Enter`, or `Escape` remain rejected.
- Never open the palette over any modal, command-bar edit, filter-bar edit, context switch, or shutdown.
- Palette selection must dismiss the palette before dispatch and must re-check current availability.
- Palette code never imports write implementations and never confirms an approval.
- Preserve explanatory key behavior: `check_action` uses `binding_enabled`; deeper `invocable` failures must not make existing keys silently inert.
- Keep `src/korvid/ui/app.py` at or below its 1,500-line ratchet by moving its existing action policy out before adding palette wiring.
- Every behavioral task follows RED → GREEN → refactor and ends with a commit carrying the required Copilot co-author trailer.
- Do not push, open a pull request, merge, or enable auto-merge without explicit maintainer instruction.

## File Structure

### New files

- `src/korvid/ui/action_policy.py` — existing binding gates plus typed, side-effect-free action availability.
- `src/korvid/ui/action_availability.py` — immutable availability codes and reasons shared by policy and existing controllers.
- `src/korvid/ui/action_palette.py` — immutable invocation/result models, catalog derivation, and deterministic ranking.
- `src/korvid/ui/widgets/action_palette.py` — the dedicated modal screen only.
- `tests/ui/test_action_policy.py` — binding-policy characterization and owner-composed availability.
- `tests/ui/test_action_palette.py` — pure catalog/ranking tests.
- `tests/ui/test_action_palette_screen.py` — modal input/list/layout behavior.
- `tests/ui/test_action_palette_workflow.py` — app integration, stale state, exact dispatch, and approval safety.

### Modified files

- `src/korvid/ui/app_bindings.py` — add `Ctrl-P`; expose the existing help grouping as reusable presentation metadata.
- `src/korvid/ui/widgets/help_screen.py` — consume the shared grouping helper.
- `src/korvid/ui/command.py` — add palette metadata inside each typed command descriptor.
- `src/korvid/ui/view_state.py` and `src/korvid/ui/app_surfaces.py` — support silent selection inspection.
- `src/korvid/ui/write_coordinator.py`, `resource_write_controller.py`, `helm_controller.py`, `forward_controller.py`, `transfer.py`, `shell_controller.py`, `operator_controller.py`, `log_controller.py` — expose side-effect-free reasons at existing guard owners and reuse them in dispatch guards.
- `src/korvid/ui/app_runtime.py`, `src/korvid/__main__.py`, `src/korvid/ui/app.py` — wire policy, open the modal, rebuild/revalidate the selected entry, and reuse existing dispatch.
- `tests/ui/test_write_coordinator.py`, `test_view_state_seam.py`, controller test files, `test_help_screen.py`, `test_command.py`, `test_keybindings.py`, `test_adaptive_footer.py`, and `test_app_structure.py` — preserve contracts while covering the new APIs.
- `tests/windows/native_app.py` and `tests/windows/test_native_terminal.py` — prove native `Ctrl-P` delivery and cancellation.
- `docs/keybindings.md`, `docs/tui.md`, and `docs/release-notes/unreleased.md` — document the complementary discovery surfaces.

---

### Task 1: Extract the existing binding policy without changing behavior

**Files:**
- Create: `src/korvid/ui/action_policy.py`
- Modify: `src/korvid/ui/app.py:1328-1425`
- Modify: `src/korvid/ui/app_runtime.py:125-159`
- Modify: `src/korvid/__main__.py:1180-1412`
- Test: `tests/ui/test_action_policy.py`
- Test: `tests/ui/test_adaptive_footer.py`
- Test: `tests/ui/test_app_structure.py`

**Interfaces:**
- Consumes: `ViewState`, `RESTARTABLE`, `SCALABLE`, `FORWARDABLE_KINDS`, Helm synthetic metas, and injected `agent_available()` / `log_pane_open()` callables.
- Produces: `ActionPolicy.binding_enabled(action: str) -> bool`.
- Produces: `AppRuntime.actions: ActionPolicy`; `KorvidApp.check_action` delegates to it.

- [ ] **Step 1: Write failing policy characterization tests**

Create `tests/ui/test_action_policy.py` with a minimal `FakeView(ViewState)` and these assertions:

```python
class FakeView(ViewState):
    def __init__(self, meta: ResourceMeta) -> None:
        self.meta = meta

    def current_kind(self) -> str:
        return self.meta.plural

    def current_scope(self) -> str:
        return "default"

    def canonical_kind(self, kind: str) -> str:
        return self.meta.plural

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return {self.meta.plural: self.meta}

    def resources(self, kind: str, scope: str) -> list[Summary]:
        return []

    def readonly(self) -> bool:
        return False

    def default_namespace(self) -> str | None:
        return "default"

    def selected_ns_name(
        self, *, notify: bool = True
    ) -> tuple[str | None, str | None]:
        return "default", "selected"

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return "uid-selected"

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural

    def write_locus(self, namespace: str | None) -> str:
        return f"in namespace {namespace}" if namespace else "cluster-wide"


def _policy(
    *,
    group: str,
    plural: str,
    synthetic: bool = False,
    log_pane_open: Callable[[], bool] = lambda: False,
    agent_available: Callable[[], bool] = lambda: True,
) -> ActionPolicy:
    meta = ResourceMeta(
        kind=plural.removesuffix("s").title(),
        plural=plural,
        group=group,
        version="v1",
        namespaced=plural != "nodes",
        synthetic=synthetic,
    )
    return ActionPolicy(
        view=FakeView(meta),
        agent_available=agent_available,
        log_pane_open=log_pane_open,
    )


def test_binding_policy_routes_overloaded_actions_by_resource_identity() -> None:
    policy = _policy(group="", plural="pods")
    assert policy.binding_enabled("logs") is True
    assert policy.binding_enabled("hint_details") is True
    assert policy.binding_enabled("helm_install") is False
    assert policy.binding_enabled("cordon_node") is False


def test_binding_policy_uses_pane_and_composition_state() -> None:
    log_open = False
    agent_available = False
    policy = _policy(
        group="",
        plural="pods",
        log_pane_open=lambda: log_open,
        agent_available=lambda: agent_available,
    )
    assert policy.binding_enabled("log_wrap") is False
    assert policy.binding_enabled("toggle_agent") is False
    assert policy.binding_enabled("help") is True


def test_binding_policy_preserves_helm_delete_exception() -> None:
    policy = _policy(
        group=HELM_RELEASES_META.group,
        plural=HELM_RELEASES_META.plural,
        synthetic=True,
    )
    assert policy.binding_enabled("delete_resource") is True
    assert policy.binding_enabled("edit_resource") is False
```

The fake must implement every abstract `ViewState` method and return one
`ResourceMeta` from `aliases()`.

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach tests/ui/test_action_policy.py -q
```

Expected: collection fails because `korvid.ui.action_policy.ActionPolicy` does
not exist.

- [ ] **Step 3: Move the current policy into the new module**

Create `src/korvid/ui/action_policy.py` with the current maps moved unchanged
from `KorvidApp`:

```python
from __future__ import annotations

from collections.abc import Callable

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.portforward import FORWARDABLE_KINDS
from korvid.ui.resource_write_controller import RESTARTABLE, SCALABLE
from korvid.ui.view_state import ViewState

_ACTION_VIEWS: dict[str, frozenset[tuple[str, str]]] = {
    "shell": frozenset({("", "pods"), ("", "nodes")}),
    "logs": frozenset({("", "pods")}),
    "logs_multi": frozenset({("", "pods")}),
    "hint_details": frozenset({("", "pods")}),
    "resize_pod": frozenset({("", "pods")}),
    "transfer": frozenset({("", "pods")}),
    "port_forward": frozenset(("", plural) for plural in FORWARDABLE_KINDS),
    "cordon_node": frozenset({("", "nodes")}),
    "uncordon_node": frozenset({("", "nodes")}),
    "drain_node": frozenset({("", "nodes")}),
    "rollout_restart": RESTARTABLE,
    "scale_resource": SCALABLE,
    "operator_install": frozenset(
        {
            (PACKAGES_GROUP, "packagemanifests"),
            (OPERATORS_GROUP, "installplans"),
        }
    ),
    "helm_install": frozenset(
        {(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}
    ),
    "helm_upgrade": frozenset(
        {(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}
    ),
    "helm_history": frozenset(
        {(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}
    ),
    "helm_rollback": frozenset(
        {(HELM_REVISIONS_META.group, HELM_REVISIONS_META.plural)}
    ),
}

_LOG_PANE_ACTIONS = frozenset(
    {"log_format", "log_wrap", "log_timestamps", "log_save", "log_previous"}
)
_SYNTHETIC_GATED_ACTIONS = frozenset({"delete_resource", "edit_resource"})


class ActionPolicy:
    def __init__(
        self,
        *,
        view: ViewState,
        agent_available: Callable[[], bool],
        log_pane_open: Callable[[], bool],
    ) -> None:
        self._view = view
        self._agent_available = agent_available
        self._log_pane_open = log_pane_open

    def binding_enabled(self, action: str) -> bool:
        if action == "toggle_agent" and not self._agent_available():
            return False
        if action in _LOG_PANE_ACTIONS:
            return self._log_pane_open()
        if action in _SYNTHETIC_GATED_ACTIONS:
            meta = self._current_meta()
            if (
                action == "delete_resource"
                and meta is not None
                and (meta.group, meta.plural)
                == (HELM_RELEASES_META.group, HELM_RELEASES_META.plural)
            ):
                return True
            return meta is None or not meta.synthetic
        views = _ACTION_VIEWS.get(action)
        if views is None:
            return True
        meta = self._current_meta()
        return meta is not None and (meta.group, meta.plural) in views

    def _current_meta(self) -> ResourceMeta | None:
        kind = self._view.canonical_kind(self._view.current_kind())
        return self._view.aliases().get(kind)
```

- [ ] **Step 4: Wire the policy at the composition root**

Add `actions: ActionPolicy` to `AppRuntime`, construct it in
`_construct_app_runtime`, bind it as `self._actions`, delete the moved maps and
helpers from `app.py`, and reduce `check_action` to:

```python
def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
    return self._actions.binding_enabled(action)
```

Add `"ActionPolicy"` to `RUNTIME_COMPONENTS` in
`tests/ui/test_app_structure.py`.

- [ ] **Step 5: Run focused regression tests**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_policy.py \
  tests/ui/test_adaptive_footer.py \
  tests/ui/test_app_structure.py -q
uv run --frozen ruff check src/korvid/ui/action_policy.py \
  src/korvid/ui/app.py src/korvid/ui/app_runtime.py src/korvid/__main__.py \
  tests/ui/test_action_policy.py tests/ui/test_app_structure.py
uv run --frozen mypy src/
uv run --frozen tach check
```

Expected: all tests and checks pass; `wc -l src/korvid/ui/app.py` is below
1,450 and `python scripts/check_source_size.py` exits zero.

- [ ] **Step 6: Commit the extraction**

```bash
git add src/korvid/ui/action_policy.py src/korvid/ui/app.py \
  src/korvid/ui/app_runtime.py src/korvid/__main__.py \
  tests/ui/test_action_policy.py tests/ui/test_app_structure.py
git commit -m "refactor(ui): extract the action binding policy (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Derive and rank palette entries from the real catalogs

**Files:**
- Create: `src/korvid/ui/action_palette.py`
- Create: `src/korvid/ui/action_availability.py`
- Modify: `src/korvid/ui/action_policy.py`
- Modify: `src/korvid/ui/app_bindings.py:1-132`
- Modify: `src/korvid/ui/widgets/help_screen.py:20-155`
- Modify: `src/korvid/ui/command.py:20-130`
- Test: `tests/ui/test_action_palette.py`
- Test: `tests/ui/test_help_screen.py`
- Test: `tests/ui/test_command.py`

**Interfaces:**
- Produces: `ActionAvailability`, `UnavailableReason`, and
  `AvailabilityCode` in `action_availability.py`.
- Produces: `AppActionInvocation`, `CommandInvocation`, `PaletteEntry`,
  `derive_action_entries`, `derive_command_entries`, and
  `rank_entries` in `action_palette.py`.
- Produces: `PaletteCommand` and explicit palette/omit metadata on every
  `CommandDescriptor`.

- [ ] **Step 1: Write failing catalog and ranking tests**

Create `tests/ui/test_action_palette.py` with:

```python
def _entry(
    entry_id: str,
    title: str,
    *,
    description: str = "",
    aliases: tuple[str, ...] = (),
    available: bool = True,
    order: int = 0,
) -> PaletteEntry:
    reason = (
        None
        if available
        else UnavailableReason(
            AvailabilityCode.NO_SELECTION,
            "select a node first",
        )
    )
    return PaletteEntry(
        id=entry_id,
        title=title,
        description=description,
        category="Actions",
        trigger="D",
        aliases=aliases,
        declaration_order=order,
        availability=ActionAvailability(True, reason),
        invocation=AppActionInvocation(entry_id.removeprefix("action:")),
    )


def _ranking_fixture(*, drain_available: bool = True) -> list[PaletteEntry]:
    return [
        _entry(
            "action:drain_node",
            "Drain",
            aliases=("drain node",),
            available=drain_available,
            order=0,
        ),
        _entry("action:drain_preview", "Drain preview", order=1),
        _entry("action:node_drainage", "Node drainage", order=2),
    ]


def test_action_entries_come_from_bindings_and_deduplicate_terminal_aliases() -> None:
    entries = derive_action_entries(
        APP_BINDINGS,
        overrides={"logs_multi": "ctrl+g"},
        availability=lambda _action: ActionAvailability.enabled(),
    )
    by_id = {entry.id: entry for entry in entries}
    assert by_id["action:logs_multi"].trigger == "Ctrl-G"
    assert sum(entry.id == "action:logs_multi" for entry in entries) == 1
    assert all("(" not in entry.invocation.action for entry in entries if isinstance(
        entry.invocation, AppActionInvocation
    ))


def test_command_entries_include_pulse_and_explain_every_omission() -> None:
    entries = derive_command_entries(
        COMMANDS,
        availability=lambda _command: ActionAvailability.enabled(),
    )
    assert any(entry.id == "command:pulse" for entry in entries)
    assert all(
        descriptor.palette is not None or descriptor.palette_omit_reason
        for descriptor in COMMANDS
    )


def test_ranking_prefers_exact_then_prefix_then_fuzzy() -> None:
    ranked = rank_entries(_ranking_fixture(), "drain")
    assert [entry.id for entry in ranked[:3]] == [
        "action:drain_node",
        "action:drain_preview",
        "action:node_drainage",
    ]


def test_exact_unavailable_result_stays_visible_with_its_reason() -> None:
    ranked = rank_entries(_ranking_fixture(drain_available=False), "drain")
    assert ranked[0].id == "action:drain_node"
    assert ranked[0].availability.reason is not None
```

Use explicit `PaletteEntry` fixtures whose aliases and descriptions make the
expected ordering unambiguous.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach tests/ui/test_action_palette.py -q
```

Expected: collection fails because the palette model does not exist.

- [ ] **Step 3: Add typed availability values**

Create `action_availability.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from korvid.ui.ui_surface import Severity


class AvailabilityCode(Enum):
    WRONG_VIEW = "wrong_view"
    NO_SELECTION = "no_selection"
    READ_ONLY = "read_only"
    MISSING_CAPABILITY = "missing_capability"
    PANE_CLOSED = "pane_closed"
    UNSUPPORTED_RESOURCE = "unsupported_resource"
    PROTECTED_UI = "protected_ui"
    TRANSITION = "transition"


@dataclass(frozen=True, slots=True)
class UnavailableReason:
    code: AvailabilityCode
    message: str
    severity: Severity = "warning"


@dataclass(frozen=True, slots=True)
class ActionAvailability:
    binding_enabled: bool
    reason: UnavailableReason | None = None

    @property
    def invocable(self) -> bool:
        return self.reason is None

    @classmethod
    def enabled(cls) -> ActionAvailability:
        return cls(binding_enabled=True)
```

- [ ] **Step 4: Share help grouping without replacing `APP_BINDINGS`**

Move `_ACTION_GROUPS`, `_GROUP_ORDER`, `_base_action`, and group lookup from
`help_screen.py` to `app_bindings.py` as:

```python
HELP_GROUP_ORDER = ("Global", "Table", "Helm", "Logs", "Describe", "Agent")
ACTION_HELP_GROUPS: dict[str, tuple[str, ...]] = {
    "quit": ("Global",),
    "help": ("Global",),
    "open_command": ("Global",),
    "open_action_palette": ("Global",),
    "toggle_all_namespaces": ("Global",),
    "favorite_namespace": ("Global",),
    "open_filter": ("Table", "Logs"),
    "describe": ("Table",),
    "relationships": ("Table",),
    "timeline": ("Table",),
    "shell": ("Table",),
    "port_forward": ("Table",),
    "logs": ("Table",),
    "logs_multi": ("Table",),
    "delete_resource": ("Table",),
    "rollout_restart": ("Table",),
    "resize_pod": ("Table",),
    "operator_install": ("Table",),
    "cordon_node": ("Table",),
    "uncordon_node": ("Table",),
    "drain_node": ("Table",),
    "scale_resource": ("Table",),
    "edit_resource": ("Table",),
    "hint_details": ("Table",),
    "transfer": ("Table",),
    "sort_by_age": ("Table",),
    "sort_by_cpu": ("Table",),
    "sort_by_mem": ("Table",),
    "sort_picker": ("Table",),
    "toggle_topbar": ("Global",),
    "log_format": ("Logs",),
    "log_wrap": ("Logs",),
    "log_timestamps": ("Logs",),
    "log_save": ("Logs",),
    "log_previous": ("Logs",),
    "log_search_next": ("Logs",),
    "log_search_prev": ("Logs", "Table"),
    "toggle_agent": ("Agent",),
    "interrupt_agent": ("Agent",),
    "helm_install": ("Helm",),
    "helm_upgrade": ("Helm",),
    "helm_rollback": ("Helm",),
    "helm_history": ("Helm",),
}


def base_action(action: str) -> str:
    return action.partition("(")[0]


def help_groups_for_action(action: str) -> tuple[str, ...]:
    return ACTION_HELP_GROUPS.get(base_action(action), ("Global",))
```

Update `collect_help` to call these helpers. Keep
`APP_HANDLER_KEY_HELP` unchanged and help-only.

- [ ] **Step 5: Add command palette metadata to the typed command catalog**

Extend `command.py`:

```python
@dataclass(frozen=True, slots=True)
class PaletteCommand:
    title: str
    canonical_text: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    aliases: tuple[str, ...]
    help: tuple[HelpRow, ...]
    operation: CommandOperation
    completion: ArgumentCompletion | None = None
    maximum_arguments: int | None = None
    palette: PaletteCommand | None = None
    palette_omit_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.palette is None) == (self.palette_omit_reason is None):
            raise ValueError(
                "command descriptor needs exactly one of palette or palette_omit_reason"
            )
```

Give meaningful bare forms metadata:

```python
PaletteCommand("Open Pulse / Problems", "pulse", ("problems", "warnings"))
PaletteCommand("Choose namespace", "ns", ("namespaces",))
PaletteCommand("Choose Kubernetes context", "ctx", ("context", "cluster"))
PaletteCommand("Configure Agent", "ai", ("agent",))
PaletteCommand("Choose Agent model", "model")
PaletteCommand("Show MCP state", "mcp")
PaletteCommand("Review external proposals", "proposals")
PaletteCommand("List port-forwards", "pf", ("port forward",))
PaletteCommand("Show Telepresence status", "tp", ("telepresence",))
PaletteCommand("Clear table sort", "sort")
```

Set `palette_omit_reason="the bound Quit action is the single palette entry"`
on the quit descriptor.

- [ ] **Step 6: Implement immutable entries and deterministic ranking**

In `action_palette.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class AppActionInvocation:
    action: str


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    canonical_text: str


PaletteInvocation = AppActionInvocation | CommandInvocation


@dataclass(frozen=True, slots=True)
class PaletteEntry:
    id: str
    title: str
    description: str
    category: str
    trigger: str
    aliases: tuple[str, ...]
    declaration_order: int
    availability: ActionAvailability
    invocation: PaletteInvocation
```

`derive_action_entries` must normalize tuple bindings to `Binding`, skip
parameterized action expressions, deduplicate `--alt` IDs, use the configured
override before the default key, and use `key_label` for display.

`rank_entries` must calculate exact, token-prefix, fuzzy-title/alias, then
fuzzy-description tiers with `textual.fuzzy.Matcher`; sort non-empty queries by
negative tier/score, then invocable first, declaration order, and stable ID.
For an empty query sort invocable actions, invocable commands, then unavailable
entries.

- [ ] **Step 7: Run focused tests and static checks**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_palette.py \
  tests/ui/test_command.py \
  tests/ui/test_help_screen.py -q
uv run --frozen ruff check src/korvid/ui/action_palette.py \
  src/korvid/ui/action_policy.py src/korvid/ui/app_bindings.py \
  src/korvid/ui/command.py src/korvid/ui/widgets/help_screen.py \
  tests/ui/test_action_palette.py tests/ui/test_command.py \
  tests/ui/test_help_screen.py
uv run --frozen mypy src/
```

Expected: all pass; existing help text and command parsing stay unchanged.

- [ ] **Step 8: Commit catalog derivation**

```bash
git add src/korvid/ui/action_palette.py src/korvid/ui/action_availability.py \
  src/korvid/ui/action_policy.py \
  src/korvid/ui/app_bindings.py src/korvid/ui/command.py \
  src/korvid/ui/widgets/help_screen.py tests/ui/test_action_palette.py \
  tests/ui/test_command.py tests/ui/test_help_screen.py
git commit -m "feat(ui): derive searchable Action Palette entries (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Expose silent selection and write availability

**Files:**
- Modify: `src/korvid/ui/view_state.py:27-96`
- Modify: `src/korvid/ui/app_surfaces.py:500-545`
- Modify: `src/korvid/ui/write_coordinator.py:292-372`
- Modify: `src/korvid/ui/resource_write_controller.py`
- Modify: `src/korvid/ui/action_policy.py`
- Modify: `src/korvid/__main__.py`
- Test: `tests/ui/test_view_state_seam.py`
- Test: `tests/ui/test_write_coordinator.py`
- Test: `tests/ui/test_write_ops.py`
- Test: `tests/ui/test_action_policy.py`

**Interfaces:**
- Produces: `ViewState.selected_ns_name(*, notify: bool = True)`.
- Produces: `WriteCoordinator.unavailable_reason()`,
  and `ResourceWriteController.unavailable_reason(action)`, each returning
  `UnavailableReason | None`.
- Produces: `ActionPolicy.availability(action: str) -> ActionAvailability`.
- Preserves: `ActionPolicy.binding_enabled` and every existing handler
  notification.

- [ ] **Step 1: Write RED tests for silent selection and write reasons**

Add:

```python
def test_silent_selection_probe_does_not_notify() -> None:
    table = _negative_cursor_table()
    notifications: list[tuple[str, str]] = []
    app = SimpleNamespace(
        _focused_table=lambda: table,
        notify=lambda message, *, severity: notifications.append((message, severity)),
    )
    view = AppViewState(cast("KorvidApp", app))
    assert view.selected_ns_name(notify=False) == (None, None)
    assert notifications == []


def test_write_unavailable_reason_is_side_effect_free(tmp_path: Path) -> None:
    env = make_env(tmp_path, view=FakeView(readonly=True))
    reason = env.coordinator.unavailable_reason()
    assert reason == UnavailableReason(
        AvailabilityCode.READ_ONLY,
        "Read-only mode: cluster writes are disabled",
    )
    assert env.ui.notifications == []
```

Add parametrized policy cases for wrong view, no selection, read-only, missing
audit, synthetic resource, and unsupported scale/restart targets. Assert
`binding_enabled` remains true for no selection and read-only where existing
key handlers explain the refusal.

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_view_state_seam.py \
  tests/ui/test_write_coordinator.py \
  tests/ui/test_action_policy.py -q
```

Expected: failures show missing `notify` parameter, `unavailable_reason`, and
`availability`.

- [ ] **Step 3: Make selection inspection optionally silent**

Change the abstract and concrete signatures:

```python
def selected_ns_name(
    self, *, notify: bool = True
) -> tuple[str | None, str | None]:
```

In `AppViewState`, guard both existing notifications with `if notify:`. Update
the two `ViewState` test fakes in `test_write_coordinator.py` and
`test_session_timeline_controller.py` to accept the keyword. Existing callers
continue using the default and retain their messages.

- [ ] **Step 4: Refactor existing owner guards to return reasons**

Add `WriteCoordinator.unavailable_reason()` and make `write_target()` call it
before resolving the target. The reason method checks, in order: read-only,
missing audit, unknown kind, synthetic kind, and silent selection. The dispatch
method emits the exact existing message/severity and returns `None`; the probe
never notifies.

Use the same side-effect-free pattern in `ResourceWriteController`:

```python
def unavailable_reason(self, action: str) -> UnavailableReason | None:
    reason = self._writes.unavailable_reason()
    if reason is not None:
        return reason
    target = self._writes.write_target(notify=False)
    if target is None:
        return UnavailableReason(
            AvailabilityCode.NO_SELECTION,
            "Select a resource first",
        )
    meta = target.meta
    if action == "rollout_restart" and (meta.group, meta.plural) not in RESTARTABLE:
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE,
            f"Restart does not apply to {gvr_label(meta)}",
        )
    if action == "scale_resource" and (meta.group, meta.plural) not in SCALABLE:
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE,
            f"Scale does not apply to {gvr_label(meta)}",
        )
    return None
```

Use a private `WriteTargetResolution` value if needed so
`WriteCoordinator.write_target(notify=True)` and `unavailable_reason()` share
the same checks without resolving or notifying twice. Preserve the existing
post-await context/UID revalidation.

- [ ] **Step 5: Extend `ActionPolicy` to compose owner reasons**

Inject the owner reason callables in `__main__.py` and implement:

```python
def availability(self, action: str) -> ActionAvailability:
    binding_enabled = self.binding_enabled(action)
    if not binding_enabled:
        return ActionAvailability(
            binding_enabled=False,
            reason=self._wrong_view_reason(action),
        )
    resolver = self._reason_by_action.get(action)
    reason = None if resolver is None else resolver()
    return ActionAvailability(binding_enabled=True, reason=reason)
```

Map generic write actions to `ResourceWriteController.unavailable_reason`.
Titles, keys, and execution remain catalog-derived.

- [ ] **Step 6: Run owner tests and prove no duplicate notifications**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_policy.py \
  tests/ui/test_view_state_seam.py \
  tests/ui/test_write_coordinator.py \
  tests/ui/test_write_ops.py -q
uv run --frozen ruff check src/korvid/ui/ tests/ui/test_action_policy.py \
  tests/ui/test_view_state_seam.py tests/ui/test_write_coordinator.py
uv run --frozen mypy src/
uv run --frozen tach check
```

Expected: all focused tests pass, and tests assert one notification per refused
keyboard action.

- [ ] **Step 7: Commit typed availability**

```bash
git add src/korvid/ui/action_availability.py src/korvid/ui/action_policy.py \
  src/korvid/ui/view_state.py \
  src/korvid/ui/app_surfaces.py src/korvid/ui/write_coordinator.py \
  src/korvid/ui/resource_write_controller.py src/korvid/__main__.py \
  tests/ui/test_action_policy.py tests/ui/test_view_state_seam.py \
  tests/ui/test_write_coordinator.py tests/ui/test_write_ops.py \
git commit -m "feat(ui): expose write action availability (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Add capability-specific availability without side effects

**Files:**
- Modify: `src/korvid/ui/helm_controller.py:176-235`
- Modify: `src/korvid/ui/forward_controller.py:160-220`
- Modify: `src/korvid/ui/transfer.py:140-190`
- Modify: `src/korvid/ui/shell_controller.py:175-235`
- Modify: `src/korvid/ui/operator_controller.py:95-160`
- Modify: `src/korvid/ui/log_controller.py`
- Modify: `src/korvid/ui/action_policy.py`
- Modify: `src/korvid/__main__.py`
- Test: `tests/ui/test_action_policy.py`
- Test: `tests/ui/test_helm_actions.py`
- Test: `tests/ui/test_forward_controller.py`
- Test: `tests/ui/test_port_forward.py`
- Test: `tests/ui/test_transfer_controller.py`
- Test: `tests/ui/test_transfer_picker.py`
- Test: `tests/ui/test_shell.py`
- Test: `tests/ui/test_node_shell.py`
- Test: `tests/ui/test_operator_install.py`
- Test: `tests/ui/test_operator_uninstall.py`
- Test: `tests/ui/test_log_controller.py`
- Test: `tests/ui/test_log_pane.py`

**Interfaces:**
- Produces: `HelmController.unavailable_reason(action: str)`.
- Produces: `ForwardController.unavailable_reason()`.
- Produces: `TransferController.unavailable_reason()`.
- Produces: `ShellController.unavailable_reason()`.
- Produces: `OperatorController.unavailable_reason()`.
- Produces: `LogController.unavailable_reason(action: str)`.
- Extends: the `reason_by_action` mapping injected into `ActionPolicy`.

- [ ] **Step 1: Write one RED reason test at each owner**

Add focused tests using each module's existing fake constructors:

```python
def test_helm_availability_reports_the_missing_executable_without_notifying() -> None:
    controller, ui = make_helm_controller(helm=None)
    reason = controller.unavailable_reason("helm_install")
    assert reason == UnavailableReason(
        AvailabilityCode.MISSING_CAPABILITY,
        "helm CLI not found on PATH - install/upgrade/rollback/uninstall unavailable",
        severity="error",
    )
    assert ui.notifications == []


def test_transfer_availability_reports_an_in_flight_transfer_without_notifying() -> None:
    controller, ui = make_transfer_controller()
    controller._in_flight = True
    reason = controller.unavailable_reason()
    assert reason == UnavailableReason(
        AvailabilityCode.PROTECTED_UI,
        "A transfer is already in progress",
    )
    assert ui.notifications == []


def test_shell_availability_reports_missing_kubectl_without_notifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, ui = make_shell_controller()
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    reason = controller.unavailable_reason()
    assert reason == UnavailableReason(
        AvailabilityCode.MISSING_CAPABILITY,
        "kubectl not found on PATH - shell-in requires kubectl",
        severity="error",
    )
    assert ui.notifications == []
```

Add equivalent assertions for forward registry/`kubectl`, operator write
client/API/manifest source, selected-pod log opening, and closed log pane.

- [ ] **Step 2: Run owner tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_helm_actions.py \
  tests/ui/test_forward_controller.py \
  tests/ui/test_transfer_controller.py \
  tests/ui/test_shell.py \
  tests/ui/test_operator_install.py \
  tests/ui/test_log_controller.py -q
```

Expected: the new tests fail because the reason methods do not exist.

- [ ] **Step 3: Extract each existing synchronous guard into its reason method**

Each method reads only already-owned synchronous state, returns the first
`UnavailableReason`, and never calls `notify`. Existing action methods reuse the
method:

```python
def _report_unavailable(self, reason: UnavailableReason | None) -> bool:
    if reason is None:
        return False
    self._ui.notify(
        reason.message,
        severity=reason.severity,
        markup=False,
    )
    return True
```

Use the exact existing messages and order:

- Helm: wrong view remains policy-owned; controller checks read-only, audit,
  Helm binary, then selection for upgrade/history/rollback.
- Forward: context transition, registry, `kubectl`, then selection.
- Transfer: exec client, in-flight transfer, context transition, then
  selection.
- Shell: context transition, selection, `kubectl`; node view also includes the
  write reason.
- Operator: write client, write reason, Subscription API, and manifest source.
- Logs: selected pod for `logs`/`logs_multi`; visible log or describe pane for
  pane-local search/format actions.

Do not move asynchronous manifest, RBAC, port discovery, or UID/context
revalidation into these methods.

- [ ] **Step 4: Compose capability reasons in `ActionPolicy`**

At the composition root, extend the existing `reason_by_action` mapping:

```python
reason_by_action={
    "delete_resource": functools.partial(resource_writes.unavailable_reason, "delete_resource"),
    "rollout_restart": functools.partial(
        resource_writes.unavailable_reason, "rollout_restart"
    ),
    "edit_resource": functools.partial(resource_writes.unavailable_reason, "edit_resource"),
    "scale_resource": functools.partial(
        resource_writes.unavailable_reason, "scale_resource"
    ),
    "resize_pod": functools.partial(resource_writes.unavailable_reason, "resize_pod"),
    "cordon_node": functools.partial(resource_writes.unavailable_reason, "cordon_node"),
    "uncordon_node": functools.partial(resource_writes.unavailable_reason, "uncordon_node"),
    "drain_node": functools.partial(resource_writes.unavailable_reason, "drain_node"),
    "helm_install": functools.partial(helm_controller.unavailable_reason, "helm_install"),
    "helm_upgrade": functools.partial(helm_controller.unavailable_reason, "helm_upgrade"),
    "helm_history": functools.partial(helm_controller.unavailable_reason, "helm_history"),
    "helm_rollback": functools.partial(helm_controller.unavailable_reason, "helm_rollback"),
    "port_forward": forward_controller.unavailable_reason,
    "transfer": transfer.unavailable_reason,
    "shell": shell.unavailable_reason,
    "operator_install": operators.unavailable_reason,
    "logs": functools.partial(logs.unavailable_reason, "logs"),
    "logs_multi": functools.partial(logs.unavailable_reason, "logs_multi"),
}
```

Add `interrupt_agent` through a small policy-owned resolver that returns
`UnavailableReason(PROTECTED_UI, "No Agent turn is running")` when
`AgentUiController.busy` is false. Do not change its binding visibility.

- [ ] **Step 5: Run all capability and policy tests**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_policy.py \
  tests/ui/test_helm_actions.py \
  tests/ui/test_forward_controller.py \
  tests/ui/test_port_forward.py \
  tests/ui/test_transfer_controller.py \
  tests/ui/test_transfer_picker.py \
  tests/ui/test_shell.py \
  tests/ui/test_node_shell.py \
  tests/ui/test_operator_install.py \
  tests/ui/test_operator_uninstall.py \
  tests/ui/test_log_controller.py \
  tests/ui/test_log_pane.py -q
uv run --frozen ruff check src/korvid/ui/ tests/ui/test_action_policy.py \
  tests/ui/test_helm_actions.py tests/ui/test_forward_controller.py \
  tests/ui/test_transfer_controller.py tests/ui/test_shell.py \
  tests/ui/test_operator_install.py tests/ui/test_log_controller.py
uv run --frozen mypy src/
uv run --frozen tach check
```

Expected: all pass; refused keyboard actions still emit one existing
notification, while availability probes emit none.

- [ ] **Step 6: Commit capability availability**

```bash
git add src/korvid/ui/helm_controller.py src/korvid/ui/forward_controller.py \
  src/korvid/ui/transfer.py src/korvid/ui/shell_controller.py \
  src/korvid/ui/operator_controller.py src/korvid/ui/log_controller.py \
  src/korvid/ui/action_policy.py src/korvid/__main__.py \
  tests/ui/test_action_policy.py tests/ui/test_helm_actions.py \
  tests/ui/test_forward_controller.py tests/ui/test_port_forward.py \
  tests/ui/test_transfer_controller.py tests/ui/test_transfer_picker.py \
  tests/ui/test_shell.py tests/ui/test_node_shell.py \
  tests/ui/test_operator_install.py tests/ui/test_operator_uninstall.py \
  tests/ui/test_log_controller.py tests/ui/test_log_pane.py
git commit -m "feat(ui): expose action capability reasons (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Build the keyboard-first palette modal

**Files:**
- Create: `src/korvid/ui/widgets/action_palette.py`
- Create: `tests/ui/test_action_palette_screen.py`

**Interfaces:**
- Consumes: `Sequence[PaletteEntry]` and `rank_entries`.
- Produces: `ActionPaletteScreen(ModalScreen[str | None])`; dismissal returns a
  stable entry ID only.

- [ ] **Step 1: Write failing modal tests**

Create tests that instantiate the screen inside a minimal test app:

```python
class PaletteHarness(App[None]):
    def __init__(self, palette: ActionPaletteScreen) -> None:
        super().__init__()
        self.palette = palette
        self.results: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Static("workspace")

    def on_mount(self) -> None:
        self.push_screen(self.palette, self.results.append)


def _entries(*, drain_available: bool = True) -> list[PaletteEntry]:
    reason = (
        None
        if drain_available
        else UnavailableReason(
            AvailabilityCode.NO_SELECTION,
            "select a node first",
        )
    )
    return [
        PaletteEntry(
            id="action:drain_node",
            title="Drain node",
            description="Safely evict workloads from a node",
            category="Actions",
            trigger="Shift-D",
            aliases=("drain",),
            declaration_order=0,
            availability=ActionAvailability(True, reason),
            invocation=AppActionInvocation("drain_node"),
        )
    ]


async def test_palette_filters_and_selects_an_available_entry() -> None:
    screen = ActionPaletteScreen(_entries())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("d", "r", "a", "i", "n")
        options = screen.query_one(OptionList)
        assert "Drain node" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        assert app.results == ["action:drain_node"]


async def test_unavailable_entry_is_visible_but_not_selectable() -> None:
    screen = ActionPaletteScreen(_entries(drain_available=False))
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        await pilot.press("d", "r", "a", "i", "n")
        option = screen.query_one(OptionList).get_option_at_index(0)
        assert option.disabled is True
        assert "Unavailable: select a node first" in str(option.prompt)
        await pilot.press("enter")
        assert app.results == []


async def test_palette_renders_in_a_narrow_terminal() -> None:
    app = PaletteHarness(ActionPaletteScreen(_entries()))
    async with app.run_test(size=(36, 16)):
        container = app.screen.query_one("#action-palette")
        assert container.size.width <= 34
        assert app.screen.query_one(Input).has_focus
```

Also cover no results, Escape, `Ctrl-P`, Up/Down, PageUp/PageDown, Home/End,
and category separators.

- [ ] **Step 2: Run modal tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach tests/ui/test_action_palette_screen.py -q
```

Expected: import fails because `ActionPaletteScreen` does not exist.

- [ ] **Step 3: Implement the modal with public Textual widgets**

Implement this structure:

```python
class ActionPaletteScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape,ctrl+p", "cancel", "Close", show=False),
        Binding("down", "move(1)", "Next", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("pagedown", "page(1)", "Next page", show=False),
        Binding("pageup", "page(-1)", "Previous page", show=False),
        Binding("home", "edge(-1)", "First", show=False),
        Binding("end", "edge(1)", "Last", show=False),
    ]

    def __init__(self, entries: Sequence[PaletteEntry]) -> None:
        super().__init__()
        self._entries = tuple(entries)
        self._visible: dict[str, PaletteEntry] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="action-palette"):
            yield Input(placeholder="Search actions and commands", id="action-query")
            yield OptionList(id="action-results")
            yield Static("Enter run · Esc close", id="action-hint")

    @on(Input.Changed)
    def _query_changed(self, event: Input.Changed) -> None:
        self._render_results(event.value)

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        entry = self._visible.get(str(event.option.id))
        if entry is not None and entry.availability.invocable:
            self.dismiss(entry.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

Keep the `Input` focused and have movement actions manipulate the `OptionList`
highlight. Build two-line `Text` prompts with `markup=False` semantics for all
cluster- or configuration-derived text. Use
`Option(prompt, id=entry.id, disabled=not entry.availability.invocable)`.

Use CSS exactly matching the design budgets:

```css
ActionPaletteScreen {
    align: center middle;
}
ActionPaletteScreen #action-palette {
    width: 76;
    max-width: 94%;
    height: auto;
    max-height: 80%;
    border: round $accent;
    padding: 1 2;
    background: $surface;
}
ActionPaletteScreen #action-results {
    height: auto;
    max-height: 18;
}
ActionPaletteScreen #action-hint {
    height: 1;
    color: $text-muted;
}
```

- [ ] **Step 4: Run modal tests and formatting**

Run:

```bash
uv run --frozen pytest -p no:tach tests/ui/test_action_palette_screen.py -q
uv run --frozen ruff check src/korvid/ui/widgets/action_palette.py \
  tests/ui/test_action_palette_screen.py
uv run --frozen ruff format --check src/korvid/ui/widgets/action_palette.py \
  tests/ui/test_action_palette_screen.py
uv run --frozen mypy src/
```

Expected: all pass with no sleeps or timeout changes.

- [ ] **Step 5: Commit the modal**

```bash
git add src/korvid/ui/widgets/action_palette.py \
  tests/ui/test_action_palette_screen.py
git commit -m "feat(ui): add the keyboard-first Action Palette modal (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Wire guarded opening and exact existing-route dispatch

**Files:**
- Modify: `src/korvid/ui/app_bindings.py`
- Modify: `src/korvid/ui/action_policy.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `tests/ui/test_keybindings.py`
- Create: `tests/ui/test_action_palette_workflow.py`
- Modify: `tests/ui/test_app_structure.py`

**Interfaces:**
- Consumes: `ActionPaletteScreen`, catalog derivation, and
  `ActionPolicy.availability`.
- Produces: priority/remappable `open_action_palette` with default `Ctrl-P`.
- Produces: `KorvidApp.action_open_action_palette()` and async selection
  callback using `run_action` or `parse_command`.

- [ ] **Step 1: Write failing workflow and safety-boundary tests**

Create `tests/ui/test_action_palette_workflow.py` with the app fixture from
`tests.ui.test_app` plus the palette helper below:

```python
async def _select_palette_entry(
    pilot: Any,
    query: str,
    expected_id: str,
) -> None:
    await pilot.press("ctrl+p")
    await until(
        pilot,
        lambda: isinstance(pilot.app.screen, ActionPaletteScreen),
        label="palette open",
    )
    for character in query:
        await pilot.press("space" if character == " " else character)
    screen = pilot.app.screen
    assert isinstance(screen, ActionPaletteScreen)
    options = screen.query_one(OptionList)
    await until(
        pilot,
        lambda: (
            options.option_count > 0
            and options.get_option_at_index(0).id == expected_id
        ),
        label=f"{expected_id} ranked first",
    )
    await pilot.press("enter")


async def test_ctrl_p_opens_palette_and_escape_restores_table_focus() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        table.focus()
        await pilot.press("ctrl+p")
        await until(
            pilot,
            lambda: isinstance(app.screen, ActionPaletteScreen),
            label="palette open",
        )
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert app.focused is table


async def test_palette_action_dispatches_once_after_dismissal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("web")])
    calls = 0
    original = app.action_help

    def counted_help() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(app, "action_help", counted_help)
    async with app.run_test() as pilot:
        await _select_palette_entry(pilot, "help", "action:help")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        assert calls == 1


async def test_palette_command_posts_the_typed_pulse_route_once() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _select_palette_entry(pilot, "pulse", "command:pulse")
        await until(
            pilot,
            lambda: isinstance(app.screen, PulseScreen),
            label="pulse open",
        )
        assert len([screen for screen in app.screen_stack if isinstance(screen, PulseScreen)]) == 1
```

Add tests proving palette opening is blocked while command/filter editing,
context switching, and any modal is active.

- [ ] **Step 2: Run workflow tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach tests/ui/test_action_palette_workflow.py -q
```

Expected: `Ctrl-P` still opens Textual's system palette or no Korvid palette
action exists.

- [ ] **Step 3: Replace Textual's implicit palette binding**

In `KorvidApp`, set:

```python
ENABLE_COMMAND_PALETTE: ClassVar[bool] = False
```

Add to `APP_BINDINGS`:

```python
Binding(
    "ctrl+p",
    "open_action_palette",
    "Actions",
    priority=True,
    id="open_action_palette",
)
```

Classify it as Global help and Nav top-bar metadata. Its priority status makes
the existing keybinding planner reject approval-key remaps automatically; add a
test for each protected key.

- [ ] **Step 4: Add protected-surface checks to `ActionPolicy`**

Inject `screen_depth`, `inline_editor_open`, `switching`, and `app_running`
callables. For `open_action_palette`, return `binding_enabled=False` when:

```python
screen_depth() > 1
or inline_editor_open()
or switching()
or not app_running()
```

Expose the same result through `availability`. Keep the direct app action guard
even when `check_action` already refused dispatch.

- [ ] **Step 5: Derive entries and dispatch after modal dismissal**

Implement `action_open_action_palette` and its callback in `app.py`:

```python
def action_open_action_palette(self) -> None:
    if not self._actions.binding_enabled("open_action_palette"):
        return
    self.push_screen(
        ActionPaletteScreen(self._palette_entries()),
        self._on_action_palette_selected,
    )


async def _on_action_palette_selected(self, entry_id: str | None) -> None:
    if entry_id is None:
        return
    entries = {entry.id: entry for entry in self._palette_entries()}
    entry = entries.get(entry_id)
    if entry is None:
        self.notify("That action is no longer available", severity="warning")
        return
    reason = entry.availability.reason
    if reason is not None:
        self.notify(reason.message, severity=reason.severity)
        return
    invocation = entry.invocation
    if isinstance(invocation, AppActionInvocation):
        await self.run_action(invocation.action)
        return
    if isinstance(invocation, CommandInvocation):
        self.post_message(parse_command(invocation.canonical_text, self._command_bar.known))
        return
    assert_never(invocation)
```

`_palette_entries` derives from `self.BINDINGS` and `COMMANDS`, uses
`self._keybinding_overrides`, and asks `self._actions.availability` for every
action. Command availability uses Agent, Telepresence, MCP, and context state
already wired into `ActionPolicy`.

- [ ] **Step 6: Run workflow, remapping, structure, and source-size checks**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_palette_workflow.py \
  tests/ui/test_keybindings.py \
  tests/ui/test_adaptive_footer.py \
  tests/ui/test_app_structure.py -q
uv run --frozen ruff check src/korvid/ui/ tests/ui/test_action_palette_workflow.py \
  tests/ui/test_keybindings.py
uv run --frozen mypy src/
uv run --frozen tach check
uv run --frozen python scripts/check_source_size.py
```

Expected: all pass; `app.py` remains within 1,500 lines.

- [ ] **Step 7: Commit app integration**

```bash
git add src/korvid/ui/app_bindings.py src/korvid/ui/action_policy.py \
  src/korvid/ui/app.py tests/ui/test_action_palette_workflow.py \
  tests/ui/test_keybindings.py tests/ui/test_adaptive_footer.py \
  tests/ui/test_app_structure.py
git commit -m "feat(ui): route Action Palette selections safely (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: Prove approval safety, stale-state refusal, and native terminal delivery

**Files:**
- Modify: `tests/ui/test_action_palette_workflow.py`
- Modify: `tests/windows/native_app.py`
- Modify: `tests/windows/test_native_terminal.py`
- Modify: `docs/keybindings.md`
- Modify: `docs/tui.md`
- Modify: `docs/release-notes/unreleased.md`

**Interfaces:**
- Consumes: final Action Palette workflow.
- Produces: security regression coverage, Windows ConPTY witness, and public
  operator documentation.

- [ ] **Step 1: Write failing approval and stale-state tests**

Extend `tests/ui/test_action_palette_workflow.py` with these imports:

```python
from pathlib import Path

from tests.ui.test_adaptive_footer import (
    _rows_listed,
    make_app as make_navigation_app,
)
from tests.ui.test_write_ops import Recorder, make_app as make_write_app
```

Then add:

```python
async def test_palette_enter_cannot_approve_a_destructive_action(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    recorder = Recorder()
    app = make_write_app(recorder, audit_path)
    async with app.run_test() as pilot:
        await _select_palette_entry(
            pilot,
            "delete resource",
            "action:delete_resource",
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation open",
        )
        assert recorder.calls == []
        assert not audit_path.exists()
        await pilot.press("escape")
        assert recorder.calls == []


async def test_ctrl_p_cannot_cover_an_approval_dialog(tmp_path: Path) -> None:
    recorder = Recorder()
    app = make_write_app(recorder, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation open",
        )
        screen = app.screen
        focused = app.focused
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is screen
        assert app.focused is focused
        assert not any(isinstance(item, ActionPaletteScreen) for item in app.screen_stack)


async def test_stale_palette_selection_is_rechecked_before_dispatch() -> None:
    app = make_navigation_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        await pilot.press("ctrl+p")
        palette = app.screen
        assert isinstance(palette, ActionPaletteScreen)
        await app._workspace_ctl.navigate("nodes", "default")
        palette.dismiss("action:hint_details")
        await until(
            pilot,
            lambda: any("pods view" in n.message for n in app._notifications),
            label="stale action refused",
        )
        assert not isinstance(app.screen, ConfirmScreen)
```

Use condition polling, not added sleeps or timeout increases.

- [ ] **Step 2: Run the safety tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_palette_workflow.py -q
```

Expected: at least one safety assertion fails until all modal and stale-state
guards are complete.

- [ ] **Step 3: Complete safety behavior without new write paths**

Fix only the policy/screen callback defects exposed by Step 2. Do not call
`ResourceWriteController`, `WriteCoordinator`, `WriteOps`, or audit methods from
palette code. The only allowed action route is
`await self.run_action(invocation.action)`.

Add an import-boundary assertion:

```python
def test_palette_modules_do_not_import_write_implementations() -> None:
    ui = Path(__file__).parents[2] / "src" / "korvid" / "ui"
    sources = [
        (ui / "action_palette.py").read_text(encoding="utf-8"),
        (ui / "widgets" / "action_palette.py").read_text(encoding="utf-8"),
    ]
    assert all("korvid.k8s.writes" not in source for source in sources)
    assert all("WriteCoordinator" not in source for source in sources)
```

- [ ] **Step 4: Add a native Windows `Ctrl-P` witness**

In `tests/windows/native_app.py`, import `ActionPaletteScreen`, record
`palette-open` with screen/focus state in `_observe_state`, and record
`palette-closed` after Escape.

In `test_native_terminal.py`, after resources are ready:

```python
session.send(b"\x10")  # Ctrl-P
palette = _phase(witnesses, "palette-open", session, deadline)
assert palette["screen"] == "ActionPaletteScreen", session.diagnostics()
session.send(b"\x1b")
closed = _phase(witnesses, "palette-closed", session, deadline)
assert closed["screen"] != "ActionPaletteScreen", session.diagnostics()
```

Preserve all existing help/filter/suspend/shell phases and the single process
deadline.

- [ ] **Step 5: Document the three complementary discovery surfaces**

Update `docs/keybindings.md` with one row for `Ctrl-P`, explain that unavailable
actions stay searchable with reasons, and state that the key is remappable.

Update `docs/tui.md` with:

- `:` navigates resources and executes typed commands;
- `?` is the exhaustive effective-key reference;
- `Ctrl-P` searches actions by intent and invokes existing routes.

Add one concise `docs/release-notes/unreleased.md` section:

```markdown
## Search actions by intent

Press `Ctrl-P` to search Korvid actions and built-in commands. Results show the
effective remapped key and explain when an action does not apply to the current
view. Selecting a write action still opens the same approval flow and requires a
fresh confirmation keystroke.
```

- [ ] **Step 6: Run focused UI, native structural, docs, and architecture checks**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_palette.py \
  tests/ui/test_action_palette_screen.py \
  tests/ui/test_action_palette_workflow.py \
  tests/ui/test_keybindings.py \
  tests/windows/test_native_terminal.py \
  tests/test_docs_readability.py \
  tests/test_docs_links.py -q
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy src/
uv run --frozen tach check
uv run --frozen python scripts/check_source_size.py
```

Expected: all pass. The true ConPTY scenario remains exercised by the required
Windows CI job; non-Windows runs retain its existing platform skip.

- [ ] **Step 7: Commit safety, platform coverage, and docs**

```bash
git add tests/ui/test_action_palette_workflow.py tests/windows/native_app.py \
  tests/windows/test_native_terminal.py docs/keybindings.md docs/tui.md \
  docs/release-notes/unreleased.md
git commit -m "test(ui): prove Action Palette safety and terminal behavior (#388)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: Run the complete gate and prepare the milestone handoff

**Files:**
- Modify only files required by failures caused by Tasks 1-7.
- Verify: `uv.lock` remains byte-identical to `HEAD`.

**Interfaces:**
- Consumes: all completed Action Palette tasks.
- Produces: a fully verified local branch ready for review and an explicit
  dependency note on #397.

- [ ] **Step 1: Run the complete repository gate once**

Run:

```bash
make check
```

Expected: Ruff, format, strict mypy, pytest, coverage, tach, deptry, source-size,
docs, and repository policy checks pass. If the corporate mirror rewrites
`uv.lock`, restore only `uv.lock`, then rerun the failing command with
`--frozen`; never re-lock or bypass hooks.

- [ ] **Step 2: Re-run the measurable feature contract**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_action_palette.py \
  tests/ui/test_action_palette_screen.py \
  tests/ui/test_action_palette_workflow.py \
  tests/ui/test_command.py \
  tests/ui/test_help_screen.py \
  tests/ui/test_keybindings.py \
  tests/ui/test_adaptive_footer.py -q
git diff --check
git status --short
```

Expected: all targeted tests pass, diff check is clean, and the worktree has
only intentional tracked changes.

- [ ] **Step 3: Request code review on the complete range**

Record `IMPLEMENTATION_BASE=$(git rev-parse HEAD)` immediately before Task 1.
Use `superpowers:requesting-code-review` with that exact base and the current
head. Address every credible correctness, security, architecture, and test
finding through a new RED→GREEN commit; do not amend prior commits.

- [ ] **Step 4: Record completion without opening a PR automatically**

Update #388 with:

- the tested branch and head SHA;
- exact focused and full-gate results;
- confirmation that #397 is a prerequisite;
- confirmation that no new write route exists.

Do not push or open a pull request until the maintainer explicitly requests it.
