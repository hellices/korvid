# Repository Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a bounded `KorvidApp`, make source-size regression impossible
to miss, turn Windows documentation-harness hangs into useful evidence, and
harden pull-request execution and repository policy before later product work.

**Architecture:** Keep `KorvidApp` as the Textual shell, move static metadata and
app-backed ports into acyclic UI modules, keep only typed runtime records in
`app_runtime.py`, and construct the controller graph in the sole composition
root, `__main__.py`. Enforce those boundaries with AST contracts and a
standard-library size ratchet, improve the file-backed Node harness runner, and
make untrusted CI use GitHub-hosted runners. Repository settings and stale
issues are changed only after source verification and are read back from
GitHub.

**Tech Stack:** Python 3.11+, Textual, pytest, Ruff, mypy strict, tach, Node.js
CommonJS/ESM harnesses, GitHub Actions YAML, GitHub REST API, pre-commit.

## Global Constraints

- Work only in branch `chore/repository-stabilization-20260911` at
  `/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911`.
- Preserve the public `KorvidApp` keyword signature and all approval,
  context/UID revalidation, redaction, and fail-closed audit behavior.
- `src/korvid/ui/app.py` must be at most 1,500 physical lines and its
  `KorvidApp.__init__` at most 160 physical lines.
- New Python modules default to at most 1,200 physical lines; existing oversized
  modules may shrink but may not exceed their 2026-09-11 baseline.
- Keep the Node harness deadline at 10 seconds; do not add retries, skips, or a
  longer deadline.
- Pull-request source must not execute on `korvid-runners`; trusted push and
  schedule executions may retain that pool.
- Add no dependency and keep `uv.lock` byte-identical to `origin/main`.
- Update only the already-authorized PR #385; do not merge, enable auto-merge,
  approve the agent's own work, push `main`, bypass hooks, change existing
  stashes, or remove unrelated worktrees.
- Use the worktree's proxy-resolved `.venv` with `UV_NO_SYNC=1 uv run ...` for
  local checks. Never run `uv lock`; keep `uv.lock` byte-identical to
  `origin/main`.

---

### Task 1: Extract static bindings and app-backed controller surfaces

**Files:**
- Create: `src/korvid/ui/app_bindings.py`
- Create: `src/korvid/ui/app_surfaces.py`
- Create: `tests/ui/test_app_structure.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/__main__.py`
- Modify: `tests/evals/operation_app.py`
- Modify: `tests/ui/test_agent_follow.py`
- Modify: `tests/ui/test_app_agent_screens.py`
- Modify: `tests/ui/test_approval_timeout.py`
- Modify: `tests/ui/test_mcp_stdio_safety.py`
- Modify: `tests/ui/test_mcp_ui_context.py`
- Modify: `tests/ui/test_view_state_seam.py`

**Interfaces:**
- Consumes: the existing `KorvidApp.BINDINGS`, `HANDLER_KEY_HELP`, `DEFAULT_CSS`,
  `_RelationshipLister`, `_displayed_resource_context`, and all `App*` adapter
  implementations without behavior changes.
- Produces: `APP_BINDINGS`, `APP_HANDLER_KEY_HELP`, `APP_CSS` from
  `app_bindings.py`; `_RelationshipLister` and the existing adapter class names
  from `app_surfaces.py`. `app_surfaces.py` imports `KorvidApp` only inside an
  `if TYPE_CHECKING:` block.

- [ ] **Step 1: Write the failing structure test**

  Create `tests/ui/test_app_structure.py` with a top-level-import check and
  responsibility check:

  ```python
  from __future__ import annotations

  import ast
  from pathlib import Path

  UI = Path(__file__).parents[2] / "src" / "korvid" / "ui"


  def _tree(name: str) -> ast.Module:
      return ast.parse((UI / name).read_text(encoding="utf-8"), filename=name)


  def test_app_support_modules_do_not_import_the_app_at_runtime() -> None:
      for name in ("app_bindings.py", "app_surfaces.py"):
          imports = [
              node.module
              for node in _tree(name).body
              if isinstance(node, ast.ImportFrom)
          ]
          assert "korvid.ui.app" not in imports


  def test_app_module_retains_only_the_textual_shell_responsibilities() -> None:
      source = (UI / "app.py").read_text(encoding="utf-8")
      assert "class AppUIBridge" not in source
      assert "class AppWorkspaceSurface" not in source
      assert 'Binding("q", "quit"' not in source
      assert "ContextSwitchCoordinator(" in source  # removed in Task 2
  ```

- [ ] **Step 2: Run the test and verify RED**

  Run:

  ```bash
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -p no:tach tests/ui/test_app_structure.py -q
  ```

  Expected: failure because `app_bindings.py` and `app_surfaces.py` do not yet
  exist.

- [ ] **Step 3: Move declarative metadata without changing values**

  Relocate the complete current `KorvidApp.BINDINGS` list, in order, to the
  typed `APP_BINDINGS` module constant. Relocate the complete current
  `HANDLER_KEY_HELP` tuple to `APP_HANDLER_KEY_HELP`, and the complete current
  `DEFAULT_CSS` string to `APP_CSS`. Bind the Textual class variables exactly
  as follows:

  ```python
  # app.py / KorvidApp
  BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = (
      APP_BINDINGS
  )
  HANDLER_KEY_HELP: ClassVar[tuple[tuple[str, str, str, str], ...]] = (
      APP_HANDLER_KEY_HELP
  )
  DEFAULT_CSS = APP_CSS
  ```

- [ ] **Step 4: Move every app-backed port as one behavior-preserving unit**

  Move `_RelationshipLister`, `_displayed_resource_context`, and these exact
  classes from `app.py` into `app_surfaces.py`: `AppUIBridge`, `AppAgentPanel`,
  `AppAgentScreens`, `AppProposalScreens`, `AppInspectSurface`,
  `AppTransferScreens`, `AppReviewTasks`, `AppProposalEvents`, `AppViewState`,
  `AppUiSurface`, `AppContextSurface`, `AppSessionConfiguration`, and
  `AppWorkspaceSurface`. Use this import shape and do not import `app.py` at
  runtime:

  ```python
  from typing import TYPE_CHECKING

  if TYPE_CHECKING:
      from korvid.ui.app import KorvidApp
  ```

  Preserve the class bodies byte-for-byte apart from formatter-driven wrapping
  and required imports.

- [ ] **Step 5: Point composition and direct adapter tests at the new module**

  Import `AppUIBridge`, `AppAgentScreens`, `AppUiSurface`, and `AppViewState`
  from `korvid.ui.app_surfaces` in the files listed above. Continue importing
  `KorvidApp` from `korvid.ui.app`; do not leave compatibility re-exports in the
  shell module.

- [ ] **Step 6: Run targeted extraction checks and verify GREEN**

  Run:

  ```bash
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -p no:tach tests/ui/test_app_structure.py \
    tests/ui/test_app_agent_screens.py tests/ui/test_view_state_seam.py \
    tests/ui/test_keybindings.py tests/ui/test_help_screen.py \
    tests/ui/test_approval_timeout.py tests/ui/test_mcp_ui_context.py -q
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/ruff check \
    src/korvid/ui/app.py src/korvid/ui/app_bindings.py \
    src/korvid/ui/app_surfaces.py tests/ui/test_app_structure.py
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/ruff format --check \
    src/korvid/ui/app.py src/korvid/ui/app_bindings.py \
    src/korvid/ui/app_surfaces.py tests/ui/test_app_structure.py
  ```

  Expected: all selected tests and Ruff checks pass.

- [ ] **Step 7: Commit the surface extraction**

  ```bash
  git add src/korvid/ui/app.py src/korvid/ui/app_bindings.py \
    src/korvid/ui/app_surfaces.py src/korvid/__main__.py tests/
  git commit -m "refactor: extract app bindings and UI surfaces"
  ```

### Task 2: Extract typed controller-runtime assembly

> **PR-review correction:** Steps 4-5 recorded the implementation that first
> landed, but they conflict with the repository's sole-composition-root rule.
> Task 9 supersedes only the location and invocation of that construction graph;
> the typed records and behavior-preserving extraction remain valid.

**Files:**
- Create: `src/korvid/ui/app_runtime.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `tests/ui/test_app_structure.py`
- Test: `tests/ui/test_app.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `KorvidApp` methods and the adapters from Task 1; all external
  collaborators currently passed to `KorvidApp.__init__`.
- Produces: `AppRuntimeInputs`, `AppRuntime`, and
  `build_app_runtime(app: KorvidApp, inputs: AppRuntimeInputs) -> AppRuntime`.
  A private `_LateReference[T]` resolves construction cycles and raises
  `RuntimeError("runtime reference read before binding")` before binding.

- [ ] **Step 1: Extend the structure test to require runtime extraction**

  Replace the temporary final assertion from Task 1 and add constructor checks:

  ```python
  def test_app_module_retains_only_the_textual_shell_responsibilities() -> None:
      source = (UI / "app.py").read_text(encoding="utf-8")
      assert "class AppUIBridge" not in source
      assert "class AppWorkspaceSurface" not in source
      assert 'Binding("q", "quit"' not in source
      assert "ContextSwitchCoordinator(" not in source
      assert "AgentUiController(" not in source


  def test_app_runtime_does_not_import_the_app_at_runtime() -> None:
      imports = [
          node.module
          for node in _tree("app_runtime.py").body
          if isinstance(node, ast.ImportFrom)
      ]
      assert "korvid.ui.app" not in imports
  ```

- [ ] **Step 2: Run the structure test and verify RED**

  Run the Task 1 structure-test command. Expected: failure because
  `app_runtime.py` is absent and constructors remain in `app.py`.

- [ ] **Step 3: Define cycle-safe runtime interfaces**

  Add these concrete shapes in `app_runtime.py`:

  ```python
  _T = TypeVar("_T")
  _UNBOUND = object()


  class _LateReference(Generic[_T]):
      def __init__(self) -> None:
          self._value: _T | object = _UNBOUND

      def bind(self, value: _T) -> None:
          if self._value is not _UNBOUND:
              raise RuntimeError("runtime reference already bound")
          self._value = value

      def get(self) -> _T:
          if self._value is _UNBOUND:
              raise RuntimeError("runtime reference read before binding")
          return cast(_T, self._value)


  @dataclass(frozen=True)
  class AppRuntimeInputs:
      config: KorvidConfig
      store: ResourceStore
      watch_manager: WatchManager
      list_namespaces: Callable[[], Awaitable[list[str]]] | None
      aliases: dict[str, ResourceMeta] | None
      get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None
      get_helm_components: Callable[[str, str], Awaitable[list[ComponentRef]]] | None
      get_helm_release_identity: (
          Callable[[str, str], Awaitable[HelmReleaseIdentity | None]] | None
      )
      get_events: EventsFetcher | None
      stream_logs: Callable[..., AsyncIterator[LogLine]] | None
      agent_session: AgentSession | None
      agent_model_name: str | None
      agent_catalog: ModelCatalog | None
      agent_save_profiles: ModelConnectionsWriter | None
      rebuild_agent: Callable[[ModelConnectionConfig, str | None], AgentSession | None] | None
      disconnect_agent: Callable[[], None] | None
      agent_available: bool
      write_ops: WriteOps | None
      audit: AuditLog | None
      check_permission: (
          Callable[[str, str, str, str | None, str, str], Awaitable[bool]] | None
      )
      mcp: MCPControllerBase | None
      edit_text: Callable[[str], Awaitable[str | None]] | None
      metrics: MetricsPoller | None
      pod_resize_supported: bool
      forwards: ForwardRegistry | None
      provider_hint: str | None
      protected_context: str | None
      open_pod_exec: Callable[..., contextlib.AbstractAsyncContextManager[Any]] | None
      list_contexts: Callable[[], tuple[list[str], str | None]] | None
      probe_context: Callable[[str], Awaitable[None]] | None
      switch_context: Callable[[str | None], Awaitable[ContextSwitchResult]] | None
      helm: HelmCLI | None
      proposal_store: ProposalStore | None
      save_topbar: Callable[[bool], None] | None
      telepresence: TelepresenceCLI | None
      probe_traffic_manager: Callable[[], Awaitable[bool]] | None
      agent_follow_bridge: UIBridge | None
      list_relationship_objects: (
          Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]] | None
      )
      session_timeline: SessionTimeline | None
      watch_warning_events: Callable[[str | None], AsyncIterator[dict[str, Any]]] | None
      approval_timeout_seconds: float | None


  @dataclass(frozen=True)
  class AppRuntime:
      view: AppViewState
      relationship_loader: RelationshipSnapshotLoader | None
      context: ContextSwitchCoordinator
      timeline: SessionTimelineController
      writes: WriteCoordinator
      bridge_dispatch: AppContextDispatch
      inspect_surface: AppInspectSurface
      inspect: ResourceInspectController
      shell: ShellController
      forward: ForwardController
      transfer: TransferController
      operators: OperatorController
      helm: HelmController
      debug: DebugController
      drain: DrainController
      resource_writes: ResourceWriteController
      workspace: WorkspaceState
      hints: HintController
      logs: LogController
      workspace_controller: WorkspaceController
      proposals: ProposalController
      integrations: IntegrationController
      agent_ui: AgentUiController
      commands: CommandRouter
  ```

  Keep `AgentSession` behind `if TYPE_CHECKING:` exactly as it is in the
  original shell so importing the base TUI still does not require the optional
  agent runtime.

- [ ] **Step 4: Move the existing construction graph into one builder**

  Implement `build_app_runtime` by moving the existing controller creation in
  its current order. Replace only forward references with private typed refs,
  for example:

  ```python
  workspace_ref = _LateReference[WorkspaceController]()
  agent_ref = _LateReference[AgentUiController]()
  logs_ref = _LateReference[LogController]()
  hints_ref = _LateReference[HintController]()
  timeline_ref = _LateReference[SessionTimelineController]()
  proposals_ref = _LateReference[ProposalController]()
  forward_ref = _LateReference[ForwardController]()
  writes_ref = _LateReference[WriteCoordinator]()

  context = ContextSwitchCoordinator(
      ui=AppUiSurface(app),
      surface=AppContextSurface(app),
      view=view,
      session=AppSessionConfiguration(app),
      store=inputs.store,
      watches=inputs.watch_manager,
      workspace=workspace_ref.get,
      logs=logs_ref.get,
      hints=hints_ref.get,
      timeline=timeline_ref.get,
      proposals=proposals_ref.get,
      forwards=forward_ref.get,
      registry=lambda: app._forwards,
      writes=writes_ref.get,
      agent=agent_ref.get,
      mcp=lambda: app._mcp,
      audit=lambda: app._audit,
      list_contexts=inputs.list_contexts,
      probe_context=inputs.probe_context,
      switch_context=inputs.switch_context,
  )
  # Construct each existing controller with its current arguments. Bind each
  # reference immediately after that concrete controller is constructed.
  workspace_ref.bind(workspace_controller)
  agent_ref.bind(agent_ui)
  return AppRuntime(
      view=view,
      relationship_loader=relationship_loader,
      context=context,
      timeline=timeline,
      writes=writes,
      bridge_dispatch=bridge_dispatch,
      inspect_surface=inspect_surface,
      inspect=inspect_controller,
      shell=shell,
      forward=forward,
      transfer=transfer,
      operators=operators,
      helm=helm_controller,
      debug=debug,
      drain=drain,
      resource_writes=resource_writes,
      workspace=workspace,
      hints=hints,
      logs=logs,
      workspace_controller=workspace_controller,
      proposals=proposals,
      integrations=integrations,
      agent_ui=agent_ui,
      commands=commands,
  )
  ```

  Every callback that deliberately reads a patchable app field remains a lambda
  over `app` rather than being frozen to the initial input value.

- [ ] **Step 5: Reduce `KorvidApp.__init__` to input packing and binding**

  Keep the exact existing signature. Validate `approval_timeout_seconds`, assign
  the raw shell-owned collaborators and state, build `AppRuntimeInputs`, call
  `build_app_runtime`, and bind every returned field to its existing private
  name (`self._ctx`, `self._writes`, `self._agent_ui`, and so on). No action or
  lifecycle method changes in this step.

- [ ] **Step 6: Run the focused wiring and architecture checks**

  Run:

  ```bash
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -p no:tach tests/ui/test_app_structure.py tests/ui/test_app.py \
    tests/ui/test_agent_wiring.py tests/ui/test_ctx_switch.py \
    tests/ui/test_impact_security.py tests/test_main_wiring.py -q
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m mypy src/korvid/ui/app.py src/korvid/ui/app_bindings.py \
    src/korvid/ui/app_surfaces.py src/korvid/ui/app_runtime.py
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m tach check
  ```

  Expected: tests, strict typing, and layer checks pass; `wc -l` reports
  `app.py <= 1500`, `app_bindings.py <= 1200`, `app_surfaces.py <= 1200`, and
  `app_runtime.py <= 1200`.

- [ ] **Step 7: Commit the runtime extraction**

  ```bash
  git add src/korvid/ui/app.py src/korvid/ui/app_runtime.py \
    tests/ui/test_app_structure.py
  git commit -m "refactor: extract KorvidApp runtime assembly"
  ```

### Task 3: Add a fail-closed source-size ratchet

**Files:**
- Create: `scripts/check_source_size.py`
- Create: `tests/test_source_size.py`
- Modify: `.pre-commit-config.yaml`
- Modify: `Makefile`
- Modify: `docs/dev/quality-gates.md`

**Interfaces:**
- Consumes: tracked `src/korvid/**/*.py` files from `git ls-files`.
- Produces: `check_repository(root: Path) -> list[str]` and a CLI returning zero
  only when all module and constructor limits pass.

- [ ] **Step 1: Write policy boundary tests**

  Build temporary git repositories in `tests/test_source_size.py` and assert:
  default-limit acceptance at exactly 1,200 lines, rejection at 1,201, a
  grandfathered module accepted at its cap and rejected one line above, missing
  and duplicate policy paths rejected, empty rationales rejected, invalid UTF-8
  rejected, `KorvidApp.__init__` accepted at 160 lines and rejected at 161, and
  the real repository returns `[]`.

  Every fixture initializes git and adds its source file so untracked files do
  not accidentally enter the policy.

- [ ] **Step 2: Run source-size tests and verify RED**

  ```bash
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -p no:tach tests/test_source_size.py -q
  ```

  Expected: import failure because `scripts/check_source_size.py` is absent.

- [ ] **Step 3: Implement the standard-library checker**

  Use an ordered tuple so duplicate paths remain detectable:

  ```python
  @dataclass(frozen=True)
  class ModuleLimit:
      path: str
      max_lines: int
      rationale: str


  DEFAULT_MAX_LINES = 1_200
  MODULE_LIMITS = (
      ModuleLimit("src/korvid/ui/app.py", 1_500, "Textual application shell"),
      ModuleLimit("src/korvid/__main__.py", 1_773, "composition root baseline"),
      ModuleLimit("src/korvid/core/config.py", 1_684, "configuration baseline"),
      ModuleLimit("src/korvid/k8s/client.py", 1_806, "client boundary baseline"),
      ModuleLimit("src/korvid/tools/executor.py", 2_059, "executor baseline"),
      ModuleLimit("src/korvid/tools/registry.py", 1_420, "registry baseline"),
      ModuleLimit(
          "src/korvid/ui/agent_ui_controller.py", 2_450,
          "agent UI controller baseline",
      ),
      ModuleLimit(
          "src/korvid/ui/workspace_controller.py", 1_640,
          "workspace controller baseline",
      ),
      ModuleLimit(
          "src/korvid/ui/widgets/resource_table.py", 1_446,
          "resource table baseline",
      ),
  )
  CONSTRUCTOR_LIMITS = (
      ConstructorLimit("src/korvid/ui/app.py", "KorvidApp", "__init__", 160),
  )
  ```

  Validate tuple entry types, positive caps, normalized repository-relative
  `.py` paths, non-empty rationale, duplicate paths, and existence among tracked
  files before reading. Decode UTF-8 strictly, count `splitlines()`, parse AST
  for constructor spans, collect every violation, print each to stderr, and exit
  1 on any violation. A failed git command is itself a violation.

- [ ] **Step 4: Wire the checker into both local gates**

  Add a local pre-commit hook with:

  ```yaml
  - id: source-size
    name: Source size ratchet
    entry: uv run --frozen python scripts/check_source_size.py
    language: system
    pass_filenames: false
    always_run: true
  ```

  Add a phony `source-size` Make target and make `check` depend on it before
  lint/typecheck/test. Document the default, exceptions, constructor cap, and
  the allowed response (split, shrink, or explicitly justify a reviewed
  exception; never bypass).

- [ ] **Step 5: Verify the checker and gate wiring**

  Run the Task 3 pytest command, then:

  ```bash
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    scripts/check_source_size.py
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/pre-commit \
    run source-size --all-files
  ```

  Expected: all tests pass and both commands exit zero.

- [ ] **Step 6: Commit the ratchet**

  ```bash
  git add scripts/check_source_size.py tests/test_source_size.py \
    .pre-commit-config.yaml Makefile docs/dev/quality-gates.md
  git commit -m "build: enforce source size limits"
  ```

### Task 4: Add bounded Windows Node-process evidence

> **PR-review correction:** Step 5 correctly requires an unbounded final wait
> after a successful `kill()`, but did not distinguish a `kill()` call that
> itself raises. Task 8 adds the missing bounded failure branch.

**Files:**
- Create: `tests/js/harness_preload.cjs`
- Modify: `tests/js/harness_lifecycle.mjs`
- Modify: `tests/test_docs_landing_behavior.py`
- Modify: `docs/dev/quality-gates.md`

**Interfaces:**
- Consumes: `_run_harness(name: str)` and the existing file-backed stdout and
  stderr captures.
- Produces: a `Popen`-based runner which re-raises its original
  `TimeoutExpired`, and value-bounded diagnostics containing PID, poll state,
  monotonic elapsed time, process status/CPU/thread/memory, and lifecycle
  milestones.

- [ ] **Step 1: Rewrite the timeout unit test for the intended `Popen` contract**

  Replace the first-call `subprocess.run` fake with a fake `Popen` object whose
  first `wait(timeout=10)` writes oversized capture data and raises one retained
  `TimeoutExpired`; assert the same exception object is raised, the snapshot is
  taken before `terminate`, the child is terminated and confirmed reaped, and
  the three startup probes remain bounded `subprocess.run` calls.

  Assert the note includes only these process fields:
  `pid`, `poll`, `elapsed-ms`, `status`, `cpu-user-seconds`,
  `cpu-system-seconds`, `threads`, `rss-bytes`, and `vms-bytes`; assert a fake
  command line, environment value, open file, and endpoint are absent.

- [ ] **Step 2: Add failing escalation and lifecycle tests**

  Add a fake process whose terminate wait also times out and assert call order
  `wait`, `snapshot`, `terminate`, `wait`, `kill`, `wait`. Extend real harness
  assertions so stderr begins with a synchronous `stage=node-started` marker and
  every lifecycle marker contains a non-negative integer `elapsed-ms=` value.

- [ ] **Step 3: Run the focused tests and verify RED**

  ```bash
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -p no:tach tests/test_docs_landing_behavior.py -q
  ```

  Expected: failures because `_run_harness` still calls `subprocess.run` and no
  preload or elapsed milestones exist.

- [ ] **Step 4: Add the synchronous preload milestone**

  `harness_preload.cjs` stores a monotonic `process.hrtime.bigint()` value on a
  `Symbol.for("korvid.harness.started")` global and synchronously writes:

  ```javascript
  korvid-harness stage=node-started elapsed-ms=0
  ```

  Invoke Node as `[node, "--require", preload, harness]`. In
  `harness_lifecycle.mjs`, reuse the stored monotonic origin (or initialize it
  when invoked without the preload) and append a rounded-down non-negative
  `elapsed-ms` to `milestone`, `complete`, `before-exit`, and `exit` output.

- [ ] **Step 5: Implement bounded process inspection and reaping**

  Use `subprocess.Popen` with closed stdin and the existing temporary files.
  On the existing ten-second `wait`, capture `monotonic()`, `process.pid`, and
  `process.poll()`, then call `psutil.Process(pid).as_dict` only for `status`,
  `cpu_times`, `num_threads`, and `memory_info`. Catch `psutil.Error` and
  `OSError` into a type-only diagnostic. Terminate and wait for two seconds;
  if that wait times out, kill and wait without another process-level deadline
  until the child is confirmed reaped. Never inspect cmdline, environment,
  files, connections, parent, username, or executable.

  Read bounded capture head/tail only after reaping, attach the existing probes
  plus the new snapshot to the original timeout, then use bare `raise`.

- [ ] **Step 6: Verify real and synthetic timeout behavior**

  Run the Task 4 test command and repeat the two successful harnesses 25 times:

  ```bash
  for run in $(seq 1 25); do
    PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
      -m pytest -p no:tach \
      tests/test_docs_landing_behavior.py::test_scene_switcher_behavior \
      tests/test_docs_landing_behavior.py::test_scene_fallback_behavior -q || exit 1
  done
  ```

  Expected: the unit and real-harness tests pass; all 25 repetitions exit zero.

- [ ] **Step 7: Document the new evidence and commit**

  Update the Windows diagnostic section without claiming a root cause or fix,
  then commit:

  ```bash
  git add tests/js/harness_preload.cjs tests/js/harness_lifecycle.mjs \
    tests/test_docs_landing_behavior.py docs/dev/quality-gates.md
  git commit -m "test: capture bounded Windows harness process evidence"
  ```

### Task 5: Isolate pull requests and bound CI jobs

**Files:**
- Create: `tests/test_ci_workflow.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/codeql.yml`
- Modify: `tests/test_platforms.py`
- Modify: `docs/dev/quality-gates.md`

**Interfaces:**
- Consumes: the current CI job names and Python/Windows compatibility matrix.
- Produces: event-sensitive GitHub-hosted runner expressions, explicit job
  timeouts, one deterministic Windows seed, and an honest advisory `ty` step.

- [ ] **Step 1: Write workflow-contract tests**

  Parse both workflow YAML files and require these exact job timeouts:
  `changes=10`, `test=45`, `windows-test=45`, `pre-commit=20`, `security=15`,
  `dependency-review=10`, `ty-experimental=15`, `analyze=20`.

  Require `ubuntu-latest` for `changes` and `dependency-review`,
  `windows-latest` for `windows-test`, and this exact runner expression for the
  Linux jobs and CodeQL:

  ```yaml
  runs-on: ${{ github.event_name == 'pull_request' && 'ubuntu-latest' || 'korvid-runners' }}
  ```

  Assert the Windows job prints `${{ github.run_id }}` and passes it once as
  `--randomly-seed=${{ github.run_id }}` to the non-retried full-suite command.
  Assert `ty-experimental` syncs `--locked --dev --all-extras`, then runs
  `uv run --with ty ty check src/`, contains no `|| true`, leaves setup
  failures blocking, and sets `continue-on-error: true` only on the analyzer
  step.

- [ ] **Step 2: Run workflow tests and verify RED**

  ```bash
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -p no:tach tests/test_ci_workflow.py tests/test_platforms.py -q
  ```

  Expected: failures for self-hosted PR jobs, missing timeouts/seed, and the
  current shell-masked `uvx ty check src/` command.

- [ ] **Step 3: Apply runner and timeout policy without reducing coverage**

  Add the exact runners and job-level `timeout-minutes` values from Step 1.
  Keep all Python 3.11/3.12/3.13 legs, the Windows leg, docs-only classifier,
  audit regression, CodeQL queries, dependency review, and security scans.

- [ ] **Step 4: Make Windows seed and `ty` execution observable**

  Add a Windows step that prints `pytest-randomly seed: ${{ github.run_id }}`.
  Add `--randomly-seed=${{ github.run_id }}` only to the full non-native suite
  invocation and update `tests/test_platforms.py`'s exact expected command.
  Replace the experimental command with:

  ```yaml
  - run: uv sync --locked --dev --all-extras
  - run: uv run --with ty ty check src/
  ```

- [ ] **Step 5: Verify workflows and pinning**

  Run the Task 5 pytest command, then:

  ```bash
  uvx zizmor --min-severity medium .github/workflows/
  rg -n '^\s*-?\s*uses:' .github/workflows \
    | rg -v '@[0-9a-f]{40}(\s|$)'
  ```

  Expected: tests and zizmor pass; the final `rg` produces no output.

- [ ] **Step 6: Commit CI hardening**

  ```bash
  git add .github/workflows/ci.yml .github/workflows/codeql.yml \
    tests/test_ci_workflow.py tests/test_platforms.py docs/dev/quality-gates.md
  git commit -m "ci: isolate pull request execution"
  ```

### Task 6: Run the complete local verification gate

**Files:**
- Modify only files required to fix credible failures introduced above.

**Interfaces:**
- Consumes: all source changes from Tasks 1-5.
- Produces: reproducible local evidence and a clean branch; no PR or merge.

- [ ] **Step 1: Format and lint every touched Python file**

  ```bash
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/ruff check --fix \
    src/ tests/ scripts/check_source_size.py
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/ruff format \
    src/ tests/ scripts/check_source_size.py
  ```

- [ ] **Step 2: Run the full project gate with the reusable environment**

  Run the equivalent of `make check` explicitly with the reusable interpreter:

  ```bash
  /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    scripts/check_source_size.py
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m ruff check src/ tests/
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m mypy
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m pytest -x -q
  PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.worktrees/main-review/.venv/bin/python \
    -m tach check
  ```

  Expected: every command exits zero. Report this as reused-environment
  evidence, not a successful fresh sync.

- [ ] **Step 3: Verify immutable and structural invariants**

  ```bash
  test "$(git hash-object uv.lock)" = "$(git show origin/main:uv.lock | git hash-object --stdin)"
  git diff --check origin/main...HEAD
  git status --short
  ```

  Expected: lock hashes match, diff check is clean, and no uncommitted files
  remain. If formatting changed files, make a new commit rather than amending.

### Task 7: Apply and read back GitHub issue and policy decisions

> This task predates the maintainer's later authorization of PR #385. Its
> external mutations and read-back checks are complete; the current delivery
> and no-merge contract is Task 10.

**Files:**
- No repository files.

**Interfaces:**
- Consumes: verified branch state and the current GitHub ruleset with ID
  `19630295`.
- Produces: closed issues #195/#307, dismissed CodeQL alert #16, strict required
  checks including security/dependency-review/analyze, a high-or-critical
  CodeQL rule, and enforced Action SHA pinning. #371 remains open.

- [ ] **Step 1: Re-read every external target immediately before mutation**

  Use `gh issue view` for #195, #307, and #371; GET CodeQL alert #16, Actions
  permissions, and ruleset 19630295. Stop if identity or current state differs
  materially from the approved design.

- [ ] **Step 2: Close only the two obsolete issues with explicit comments**

  Close #195 as `not planned`, explaining that its three extension surfaces
  have different readiness and a future plugin issue must start from a concrete
  consumer. Close #307 as `not planned`, naming PR #312 as the delivered
  stateful journey foundation, the `small`/`full` to `low`/`high` replacement,
  and that dynamic phase-specific low-tier tool exposure was not implemented.
  Read both issues back and verify `state=CLOSED` and `stateReason=NOT_PLANNED`.

- [ ] **Step 3: Dismiss the intentional CodeQL test alert**

  PATCH alert #16 to `state=dismissed`, `dismissed_reason=used in tests`, with a
  comment that `tests/providers/test_models_dev.py` creates a deliberate
  world-writable decoy while production uses a private temporary file with mode
  `0600`. Read back the alert and verify the reason, comment, and path.

- [ ] **Step 4: Update the complete default-branch ruleset payload**

  Preserve deletion, non-fast-forward, Copilot review, pull-request parameters,
  zero approving reviews, allowed merge methods, conditions, enforcement, and
  empty bypass actors. Change required-status parameters to strict and add
  unique contexts `security`, `dependency-review`, and `analyze`. Add:

  ```json
  {
    "type": "code_scanning",
    "parameters": {
      "code_scanning_tools": [{
        "tool": "CodeQL",
        "security_alerts_threshold": "high_or_higher",
        "alerts_threshold": "errors"
      }]
    }
  }
  ```

  PUT the complete ruleset, GET it back, and compare all preserved and changed
  fields. Do not modify merge settings or bypass actors.

- [ ] **Step 5: Require immutable Action references and read back**

  PUT Actions permissions with the existing `enabled=true` and
  `allowed_actions=all`, changing only `sha_pinning_required=true`. GET the
  setting back and require the exact three values.

- [ ] **Step 6: Verify external end state**

  Confirm #195/#307 are closed as not planned, #371 is open, alert #16 is
  dismissed as used in tests, the ruleset is active/strict with all eight
  required contexts and the CodeQL high-or-higher rule, and SHA pinning is true.
  Confirm the branch name, commits, local checks, and that Windows recurrence
  evidence remains pending. Do not merge.

### Task 8: Bound the cleanup path when `kill()` itself fails

**Files:**
- Modify: `tests/test_docs_landing_behavior.py`
- Modify: `docs/superpowers/specs/2026-09-11-repository-stabilization-design.md`

**Interfaces:**
- Consumes: `_bounded_process_wait()` and `_terminate_and_reap()`.
- Produces: a cleanup state string that never reports a failed kill as reaped
  unless the final bounded wait actually reaps it.

- [ ] **Step 1: Add the failing kill-error regression**

  Add a fake whose first bounded wait times out, whose `kill()` raises
  `PermissionError`, and whose second bounded wait also times out. Assert the
  exact call order and diagnostic:

  ```python
  def test_harness_timeout_bounds_reap_when_kill_itself_fails() -> None:
      class FakeProcess:
          def __init__(self) -> None:
              self.calls: list[str] = []

          def terminate(self) -> None:
              self.calls.append("terminate")

          def kill(self) -> None:
              self.calls.append("kill")
              raise PermissionError("denied")

          def wait(self, timeout: float | None = None) -> int:
              self.calls.append(f"wait:{timeout}")
              raise subprocess.TimeoutExpired("node", timeout or 0)

      process = FakeProcess()
      result = _terminate_and_reap(cast(Any, process), poll_state=None)

      assert process.calls == ["terminate", "wait:2", "kill", "wait:2"]
      assert result == "kill=error type=PermissionError; reap=timed-out"
  ```

- [ ] **Step 2: Run the regression and verify RED**

  ```bash
  UV_NO_SYNC=1 uv run pytest -p no:tach \
    tests/test_docs_landing_behavior.py::test_harness_timeout_bounds_reap_when_kill_itself_fails -q
  ```

  Expected: failure because the current implementation calls `wait(None)`
  after `kill()` raises.

- [ ] **Step 3: Split successful-kill and failed-kill reaping**

  Preserve the unbounded confirming wait only when `process.kill()` returns.
  When it raises, call `_bounded_process_wait(process)` and append its real
  result:

  ```python
  try:
      process.kill()
  except OSError as error:
      diagnostics.append(f"kill=error type={type(error).__name__}")
      diagnostics.append(f"reap={_bounded_process_wait(process)}")
      return "; ".join(diagnostics)
  process.wait()
  diagnostics.append("kill=reaped")
  return "; ".join(diagnostics)
  ```

- [ ] **Step 4: Run the focused harness tests and verify GREEN**

  ```bash
  UV_NO_SYNC=1 uv run pytest -p no:tach tests/test_docs_landing_behavior.py -q
  UV_NO_SYNC=1 uv run ruff check tests/test_docs_landing_behavior.py
  UV_NO_SYNC=1 uv run ruff format --check tests/test_docs_landing_behavior.py
  ```

  Expected: all commands exit zero and the successful-kill tests still end in
  `wait:None`/`kill=reaped`.

### Task 9: Restore root-owned UI runtime assembly

**Files:**
- Create: `src/korvid/composition_support.py`
- Create: `tests/app_factory.py`
- Create: `tests/app_factory_typing.py`
- Modify: `src/korvid/__main__.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/ui/app_runtime.py`
- Modify: `tests/ui/test_app_structure.py`
- Modify: every test module that directly constructs `KorvidApp` or one of its
  test subclasses.

**Interfaces:**
- Consumes: the existing `AppRuntimeInputs`, `AppRuntime`, `_LateReference`, and
  current controller constructor arguments without behavior changes.
- Produces: `assemble_app_runtime(app: AppT) -> AppT` in `korvid.__main__`,
  `KorvidApp.runtime_inputs`, one-time `KorvidApp.bind_runtime(runtime)`, and
  a `ParamSpec`-bound `build_test_app` for `KorvidApp` plus the explicit typed
  `build_test_subclass` path.

- [ ] **Step 1: Add failing architecture and construction-path tests**

  In `tests/ui/test_app_structure.py`, collect calls by AST name and require the
  controller graph in the root, not either UI assembly file:

  ```python
  RUNTIME_COMPONENTS = {
      "AgentUiController",
      "AppAgentPanel",
      "AppAgentScreens",
      "AppContextSurface",
      "AppContextDispatch",
      "AppInspectSurface",
      "AppProposalEvents",
      "AppProposalScreens",
      "AppReviewTasks",
      "AppRuntime",
      "AppSessionConfiguration",
      "AppTransferScreens",
      "AppUIBridge",
      "AppUiSurface",
      "AppViewState",
      "AppWorkspaceSurface",
      "CommandRouter",
      "ContextSwitchCoordinator",
      "DebugController",
      "DrainController",
      "ForwardController",
      "HelmController",
      "HintController",
      "IntegrationController",
      "LogController",
      "OperatorController",
      "ProposalController",
      "RelationshipSnapshotLoader",
      "ResourceInspectController",
      "ResourceWriteController",
      "SessionTimelineController",
      "ShellController",
      "TransferController",
      "WorkspaceController",
      "WorkspaceState",
      "WriteCoordinator",
  }


  def _called_names(tree: ast.AST) -> set[str]:
      return {
          node.func.id
          for node in ast.walk(tree)
          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
      }


  def test_only_the_composition_root_constructs_the_app_runtime() -> None:
      ui_calls = set().union(
          *(_called_names(ast.parse(path.read_text(encoding="utf-8"))) for path in UI.rglob("*.py"))
      )
      assert not ui_calls.intersection(RUNTIME_COMPONENTS)
      assert RUNTIME_COMPONENTS <= _called_names(ast.parse(MAIN.read_text(encoding="utf-8")))
  ```

  Add a second AST contract that walks `tests/**/*.py`, excludes
  `tests/app_factory.py`, and reports every direct call whose function name
  ends in `KorvidApp` (including `MeasuredKorvidApp` and
  `_ObservedKorvidApp`). Add a focused behavior test that a second
  `bind_runtime()` raises
  `RuntimeError("app runtime already bound")`.

- [ ] **Step 2: Run the structure tests and verify RED**

  ```bash
  UV_NO_SYNC=1 uv run pytest -p no:tach tests/ui/test_app_structure.py -q
  ```

  Expected: the graph constructors are found in `app_runtime.py`, absent from
  `__main__.py`, and direct test construction paths are reported.

- [ ] **Step 3: Make `app_runtime.py` a data/type module**

  Keep `AppRuntimeInputs`, `AppRuntime`, and `_LateReference`; remove
  `build_app_runtime` and every controller/surface constructor call. Put
  annotation-only controller imports behind `TYPE_CHECKING` where runtime
  lookup is unnecessary.

- [ ] **Step 4: Make the Textual shell explicitly bindable once**

  Preserve the current `KorvidApp` keyword signature and input validation.
  Store the immutable input record, bind only shell-owned fields in
  `__init__`, and add:

  ```python
  @property
  def runtime_inputs(self) -> AppRuntimeInputs:
      return self._runtime_inputs

  def bind_runtime(self, runtime: AppRuntime) -> None:
      if self._runtime_bound:
          raise RuntimeError("app runtime already bound")
      self._runtime_bound = True
      self._runtime = runtime
      self._view = runtime.view
      self._relationship_loader = runtime.relationship_loader
      self._ctx = runtime.context
      self._timeline = runtime.timeline
      self._writes = runtime.writes
      self._bridge_dispatch = runtime.bridge_dispatch
      self._inspect_surface = runtime.inspect_surface
      self._inspect = runtime.inspect
      self._shell = runtime.shell
      self._forward = runtime.forward
      self._transfer = runtime.transfer
      self._olm = runtime.operators
      self._helm_ctl = runtime.helm
      self._debug = runtime.debug
      self._drain = runtime.drain
      self._resource_writes = runtime.resource_writes
      self._workspace = runtime.workspace
      self._hints = runtime.hints
      self._logs = runtime.logs
      self._workspace_ctl = runtime.workspace_controller
      self._proposals = runtime.proposals
      self._integrations = runtime.integrations
      self._agent_ui = runtime.agent_ui
      self._commands = runtime.commands
  ```

  Set `_runtime_bound = False` before any external assembly and do not invoke a
  builder from the UI package.

- [ ] **Step 5: Move only support definitions out of the size-bounded root**

  Move behavior-preserving records/adapters and non-construction lifecycle
  helpers from `__main__.py` into `composition_support.py`: at minimum
  `ObservabilityWiring`, `_MCPAppHooks`, `_AgentToolUIBridgeProxy`,
  `AgentWiring`, `_AgentUiBridgeProxy`, `_RunState`,
  `_missing_extra_packages`, `_custom_column_names`, `_shutdown`,
  `_discover_in_background`, `_close_provider_in_background`,
  `_close_agent_in_background`, `_cluster_facts`, `_agent_environment`,
  `_warn_agent_disabled`, `_active_model_name`, `_validate_ca_bundle`,
  `_start_mcp_if_enabled`, `_teardown`, and `_protected_context_name`.
  Re-export the private names from `__main__.py` so existing focused tests and
  monkeypatch seams keep their contracts. Do not move provider, MCP,
  observability, agent-session, or UI-controller constructor calls into the
  support module.

- [ ] **Step 6: Construct and bind the complete graph in `__main__.py`**

  Move the existing `build_app_runtime` body into the root without changing
  constructor arguments or callback late-binding. Define:

  ```python
  AppT = TypeVar("AppT", bound=KorvidApp)


  def assemble_app_runtime(app: AppT) -> AppT:
      runtime = _construct_app_runtime(app, app.runtime_inputs)
      app.bind_runtime(runtime)
      return app
  ```

  `_construct_app_runtime` is the existing `build_app_runtime` implementation
  moved into `__main__.py` and renamed; every constructor call and callback
  argument remains in that function. In `_wire_and_run`, construct the shell
  first and immediately replace it with `app = assemble_app_runtime(app)`.
  Keep the `__main__.py` source-size cap at 1,773; reduce or move only pure
  support definitions if more room is needed.

- [ ] **Step 7: Add and migrate the explicit test factory**

  Create:

  ```python
  from collections.abc import Callable
  from typing import ParamSpec, TypeVar


  AppT = TypeVar("AppT", bound=KorvidApp)
  AppP = ParamSpec("AppP")


  def _bind_test_app_factory(
      app_type: Callable[AppP, AppT],
      /,
  ) -> Callable[AppP, AppT]:
      def build(*args: AppP.args, **kwargs: AppP.kwargs) -> AppT:
          return assemble_app_runtime(app_type(*args, **kwargs))

      return build


  build_test_app = _bind_test_app_factory(KorvidApp)


  def build_test_subclass(
      app_type: Callable[AppP, AppT],
      /,
      *args: AppP.args,
      **kwargs: AppP.kwargs,
  ) -> AppT:
      return assemble_app_runtime(app_type(*args, **kwargs))
  ```

  Replace direct `KorvidApp(...)` test calls with `build_test_app(...)`. For
  `_ObservedKorvidApp` and `MeasuredKorvidApp`, use
  `build_test_subclass(Subclass, ...)`. Add a compile-time negative regression
  that makes mypy's unused-ignore check fail if the base factory accepts an
  unknown `KorvidApp` keyword. Leave subclass `super().__init__(...)` calls intact.
  Update the composition-root fakes so they either capture the runtime bind or
  explicitly stub `assemble_app_runtime`; do not weaken the production path.

- [ ] **Step 8: Run focused architecture, UI, optional-extra, and size checks**

  ```bash
  UV_NO_SYNC=1 uv run pytest -p no:tach tests/ui/test_app_structure.py \
    tests/ui tests/test_main_wiring.py tests/test_main_recovery.py \
    tests/test_optional_extras.py tests/evals tests/performance \
    tests/windows/test_native_terminal.py -q
  UV_NO_SYNC=1 uv run ruff check src/korvid/__main__.py \
    src/korvid/composition_support.py src/korvid/ui/app.py \
    src/korvid/ui/app_runtime.py tests/app_factory.py tests/
  UV_NO_SYNC=1 uv run ruff format --check src/korvid/__main__.py \
    src/korvid/composition_support.py src/korvid/ui/app.py \
    src/korvid/ui/app_runtime.py tests/app_factory.py tests/
  UV_NO_SYNC=1 uv run mypy src/ tests/
  UV_NO_SYNC=1 uv run tach check
  UV_NO_SYNC=1 uv run python scripts/check_source_size.py
  ```

  Expected: every command exits zero; `app.py <= 1500`,
  `__main__.py <= 1773`, and no direct test construction remains.

### Task 10: Verify, update PR #385, and continue the review loop

**Files:**
- Modify only files needed for credible review findings or required-check
  failures.

- [ ] **Step 1: Run the complete local gate and pre-commit**

  ```bash
  UV_NO_SYNC=1 make check
  UV_NO_SYNC=1 uv run pre-commit run --all-files
  git diff --check
  test "$(git hash-object uv.lock)" = \
    "$(git show origin/main:uv.lock | git hash-object --stdin)"
  ```

- [ ] **Step 2: Commit, push, and update the PR description**

  Use a new commit (never amend and never bypass hooks), push the topic branch,
  and add the two review corrections and their regression tests to PR #385's
  body.

- [ ] **Step 3: Reply, resolve, and request another review**

  Reply individually to inline comment `3991377906` with the commit and test,
  resolve its GraphQL thread, and request
  `copilot-pull-request-reviewer[bot]`. Read every new review body, including
  `<details>`/suppressed findings, and every unresolved thread.

- [ ] **Step 4: Repeat only for credible findings**

  Apply RED→GREEN for correctness, security, architecture, data-loss, or
  required-check findings. Count low-confidence-only rounds as specified in
  `AGENTS.md`; after two consecutive such rounds, stop speculative changes and
  do not request another Copilot review.

- [ ] **Step 5: Hand back without merging**

  Require every entry in `gh pr view 385 --json statusCheckRollup` to be
  `SUCCESS`, report the final commit and review state, and stop. Never merge,
  enable auto-merge, or approve the PR.
