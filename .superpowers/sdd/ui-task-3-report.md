# UI Wait Cleanup Task 3 Report

## Baseline

- Focused baseline command: `uv run --no-sync pytest -p no:tach tests/ui/test_write_ops.py tests/ui/test_agent_write.py tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py tests/ui/test_describe.py tests/ui/test_resize_flow.py tests/ui/test_node_shell.py -q`
- Baseline result: `190 passed in 136.66s (0:02:16)`
- Pre-edit pause metrics: `200 pilot.pause calls`, `24.45s` encoded numeric duration, `9` no-arg pauses

## Review findings resolved

- `test_explicit_navigation_clears_the_hierarchy_return` now waits for the *rendered* empty pods target (`NAME/READY/STATUS…NODE` headers + zero rows) before Escape, so the test cannot act on a stale deployments table after `:pods`.
- `test_escape_after_goto_reopens_the_tree_on_the_release_view` now also asserts the app is still on `helmreleases` after the final Escape + idle tick, so the retained no-arg wait is followed by a concrete view-state check.
- `test_explicit_navigation_clears_the_hierarchy_return` now also asserts the app is still on `pods` after Escape + idle tick, so the retained no-arg wait is followed by a concrete view-state check.
- The same empty-pods readiness now guards `test_tree_does_not_open_when_view_changed_during_fetch`, the other `_navigate(... "pods" ...)` call that immediately triggers a dependent action.
- `test_ctrl_d_cancelled_does_nothing` now captures the original base screen object, waits for that exact screen to return after `n`, then restores the original `0.2s` delayed no-write window before asserting no recorder call or audit file.
- `test_unwritable_audit_blocks_write` now restores the original `0.3s` delayed no-dispatch window after the audit-blocked warning before asserting no write ran.
- Replaced every one-word/generic wait label left in scope (`dialog`, `closed`, `popped`, `jumped`, `split`, `drilled`) with action/state-specific labels, and re-audited the seven Task 3 files for remaining equivalents.

## Wait inventory and classification (pre-edit line numbers)

### `tests/ui/test_write_ops.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `_to_view` | `L197 (0.1)` | action-settling wait; converted to view-kind + selected-resource identity polling | converted |
| `test_ctrl_d_delete_confirmed_executes_and_audits` | `L205 (0.1)` | action-settling wait; converted to pod-row readiness before opening the approval screen | converted |
| `test_ctrl_d_cancelled_does_nothing` | `L224 (0.1)`, `L226 (default)`, `L228 (0.2)` | startup/open waits converted to pod-row readiness plus exact base-screen restoration after cancel; retained the original `0.2s` post-cancel absence window because the invariant is delayed non-dispatch (`no write`, `no audit`) | retained |
| `test_readonly_blocks_delete` | `L237 (0.1)`, `L239 (default)` | action-settling waits; converted to pod-row readiness plus explicit read-only notification | converted |
| `test_cluster_scoped_delete_requires_typed_name` | `L248 (0.1)`, `L251 (default)`, `L254 (0.2)` | action-settling waits; converted to nodes-view selection, confirmation identity, and typed-input value predicates so the test still proves `y` alone does not dispatch | converted |
| `test_rollout_restart_on_deployment` | `L268 (0.1)` | action-settling wait; converted to pod-row readiness before navigating to deployments | converted |
| `test_rollout_restart_rejected_on_pods` | `L281 (0.1)`, `L283 (default)` | startup wait converted to pod-row readiness; retained the post-key idle tick because the behaviour under test is absence (`r` on pods stays inert: no confirm, no dispatch) | retained |
| `test_scale_flow_prompts_then_confirms` | `L292 (0.1)` | action-settling wait; converted to pod-row readiness before the replicas prompt | converted |
| `test_failed_write_audits_error` | `L309 (0.1)` | action-settling wait; converted to pod-row readiness before the failing delete flow | converted |
| `test_permission_denied_blocks_delete` | `L323 (0.1)`, `L325 (0.2)` | action-settling waits; converted to pod-row readiness plus explicit permission-denial notification | converted |
| `test_permission_allowed_proceeds` | `L334 (0.1)` | action-settling wait; converted to pod-row readiness | converted |
| `test_unwritable_audit_blocks_write` | `L350 (0.1)`, `L354 (0.3)` | startup wait converted to pod-row readiness; retained the original `0.3s` post-warning absence window because fail-closed audit blocking is a delayed non-dispatch contract | retained |
| `test_scale_prompt_prefills_current_replicas` | `L364 (0.1)` | action-settling wait; converted to pod-row readiness before deployment navigation | converted |
| `test_dialog_opened_during_permission_check_aborts_write` | `L391 (0.1)`, `L393 (0.1)`, `L397 (0.3)` | startup waits converted to pod-row readiness and a positive “permission check in flight” event; retained the final `0.3s` absence window because the assertion is specifically that no confirm appears over the blocker after release | retained |
| `test_y_queued_during_stalled_check_cannot_approve` | `L422 (0.1)`, `L429 (0.2)` | startup wait converted to pod-row readiness; retained the `0.2s` absence window because the test input is a stale pre-dialog `y` and the invariant is that no write dispatch follows | retained |
| `test_delete_binds_selected_row_uid` | `L448 (0.1)` | action-settling wait; converted to pod-row readiness | converted |
| `test_scale_binds_selected_row_uid` | `L460 (0.1)` | action-settling wait; converted to pod-row readiness | converted |
| `test_conflict_reports_target_changed_since_approval` | `L478 (0.1)` | action-settling wait; converted to pod-row readiness | converted |
| `test_e_edit_confirmed_replaces_with_uid` through `test_external_editor_suspend_not_supported_cancels` | `L533..L1061` startup waits (`26` calls, all `0.1`) | action-settling waits; converted to pod-row readiness plus precise editor/confirm/notification/audit predicates | converted |

### `tests/ui/test_agent_write.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `test_agent_delete_approved_by_user_key` through `test_agent_write_dialog_shows_the_ownership_banner` | all `L121..L642 (0.1)` startup waits except the rows below | action-settling startup waits; converted to base-screen readiness because these agent flows do not depend on a populated table row | converted |
| `test_agent_write_pending_while_panel_collapsed` | `L342 (0.1)`, `L346 (0.3)` | startup wait converted to base-screen readiness; retained `0.3s` because the behaviour under test is that a collapsed panel keeps the approval pending with no surprise modal | retained |
| `test_agent_write_waits_for_user_modal_to_close` | `L380 (0.1)`, `L386 (0.3)` | startup wait converted to base-screen readiness; retained `0.3s` because the test input is “another dialog stays open” and the invariant is absence of stacked approval | retained |
| `test_agent_write_expired_budget_never_grants_extra_window` | `L525 (0.1)`, `L529 (0.1)` | startup wait converted to base-screen readiness; retained the final `0.1s` post-result absence window because the contract under test is “no lingering approval window after expiry” | retained |
| `test_agent_write_missing_target_errors_before_dialog` | `L568 (0.1)`, `L575 (0.1)` | action-settling waits; converted to base-screen readiness and immediate post-result assertion because `agent_request_write(...)` has already finished the whole flow | converted |

### `tests/ui/test_hierarchy_nav.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `test_enter_on_release_opens_hierarchy_tree` through `test_alias_discovery_refreshes_open_tree` | all `0.1` startup waits except the rows below | action-settling waits; converted to base-screen readiness before `:view` navigation, plus existing row/screen predicates | converted |
| `test_escape_after_goto_reopens_the_tree_on_the_release_view` | `L240 (0.1)`, `L267 (default)` | startup wait converted to base-screen readiness; retained the final idle tick because the assertion is that the one-shot hierarchy return stays gone after a second Escape | retained |
| `test_explicit_navigation_clears_the_hierarchy_return` | `L276 (0.1)`, `L287 (default)` | startup wait converted to base-screen readiness; added empty-pods render readiness before Escape (headers + zero rows) so the test cannot leave the stale deployments table in place; retained the final idle tick because the assertion is continued absence of a latent tree return | retained |
| `test_hierarchy_return_is_scoped_to_the_initiating_pane` | `L321 (0.1)`, `L333 (default)` | startup wait converted to base-screen readiness; retained the final idle tick because the assertion is that pressing Escape in the other pane does *not* reopen the tree | retained |
| `test_return_is_refused_when_a_ctx_switch_starts_during_the_navigate` | `L403 (0.1)`, `L423 (0.5)` | both waits were action-settling; converted to base-screen readiness plus an explicit `app._ctx_switching and not HierarchyScreen` predicate | converted |

### `tests/ui/test_node_ops.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `test_c_cordons_node_after_approval` through `test_persistently_throttled_eviction_counts_as_failed` | all startup waits plus post-action waits except the rows below | action-settling waits; converted to initial-row readiness, confirmation identity, audit/result state, status-bar progress, precheck calls, or typed-input predicates | converted |
| `test_cordon_does_not_apply_to_pods` | `L220 (0.1)`, `L222 (0.2)` | startup wait converted to initial-row readiness; retained `0.2s` because the tested behaviour is pure absence on the wrong view (no confirm, no node mutation) | retained |
| `test_cancelled_in_flight_eviction_that_lands_is_counted` | `L721 (0.1)`, `L728 (0.05)` | startup wait converted to initial-row readiness; retained `0.05s` because it is the ordering input that lets cancellation arrive before the in-flight eviction is released | retained |

### `tests/ui/test_describe.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `test_d_opens_describe_screen_with_pod_name_and_events` through `test_describe_unknown_kind_notifies_error` | `26` calls, all `0.2` | action-settling waits; converted to precise table-row, describe-screen, dismissal, and notification predicates with no retained sleeps | converted |

### `tests/ui/test_resize_flow.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `_to_view` | `L172 (0.1)` | action-settling wait; converted to view-kind + selected-resource identity polling | converted |
| `test_resize_confirmed_executes_and_audits`, `test_resize_confirm_shows_preview`, `test_resize_gated_when_cluster_lacks_subresource`, `test_readonly_blocks_resize`, `test_resize_precheck_asks_patch_on_pods_resize`, `test_resize_prompt_cancel_does_nothing`, `test_agent_resize_approved_by_user_key`, `test_agent_resize_requires_resources`, `test_agent_resize_rejected_for_non_pod_kind`, `test_agent_resize_rejected_when_cluster_lacks_subresource`, `test_resize_banner_reuses_the_prompt_manifest` | remaining `0.1/0.2` waits in those tests | action-settling waits; converted to row readiness, prompt identity, confirm identity, notifications, or precheck call predicates | converted |
| `test_resize_only_applies_to_pods` | `L233 (0.1)`, `L236 (0.2)` | startup/navigation waits converted to row- and view-readiness; retained `0.2s` because the wrong-kind resize path is an absence test (no prompt, no write) with no stronger observable completion signal | retained |

### `tests/ui/test_node_shell.py`

| Test | `pilot.pause` calls | Classification | Result |
| --- | --- | --- | --- |
| `test_s_on_nodes_view_opens_privileged_approval_dialog` through `test_suspend_not_supported_refuses_gracefully_and_cleans_up` | `19` calls (`18 × 0.1`, `1 × 0.1` dismissal wait) | action-settling waits; converted to base-screen readiness, nodes-view selection, confirmation identity, cleanup completion, notification, and audit-outcome predicates with no retained sleeps | converted |

## Before/after pause metrics

| File | Before calls | Before explicit seconds | After calls | After explicit seconds |
| --- | ---: | ---: | ---: | ---: |
| `tests/ui/test_write_ops.py` | 51 | 5.50 | 5 | 1.00 |
| `tests/ui/test_agent_write.py` | 29 | 3.30 | 3 | 0.70 |
| `tests/ui/test_hierarchy_nav.py` | 29 | 3.00 | 3 | 0.00 |
| `tests/ui/test_node_ops.py` | 28 | 3.25 | 2 | 0.25 |
| `tests/ui/test_describe.py` | 26 | 5.20 | 0 | 0.00 |
| `tests/ui/test_resize_flow.py` | 18 | 2.30 | 1 | 0.20 |
| `tests/ui/test_node_shell.py` | 19 | 1.90 | 0 | 0.00 |
| **Total** | **200** | **24.45** | **14** | **2.15** |

## Retained-wait rationale

- `test_rollout_restart_rejected_on_pods`: retained one idle tick because the contract is “no confirm / no dispatch” on the wrong kind.
- `test_ctrl_d_cancelled_does_nothing`: restored `0.2s` after the base screen returns because cancel is defined by *continued* absence of both write dispatch and audit output.
- `test_unwritable_audit_blocks_write`: restored `0.3s` after the audit-blocked warning because fail-closed intent logging must keep the write suppressed even after the notification lands.
- `test_dialog_opened_during_permission_check_aborts_write`: retained `0.3s` after the blocker appears to prove the confirm screen never stacks on top after the gated check is released.
- `test_y_queued_during_stalled_check_cannot_approve`: retained `0.2s` because the stale pre-dialog `y` must produce *no* dispatch.
- `test_agent_write_pending_while_panel_collapsed`: retained `0.3s` to preserve the “collapsed panel keeps request pending with no modal” timing contract.
- `test_agent_write_waits_for_user_modal_to_close`: retained `0.3s` to preserve the “approval waits behind an existing user modal” timing contract.
- `test_agent_write_expired_budget_never_grants_extra_window`: retained `0.1s` to prove no lingering dialog survives an already-expired approval budget.
- `test_escape_after_goto_reopens_the_tree_on_the_release_view`, `test_explicit_navigation_clears_the_hierarchy_return`, `test_hierarchy_return_is_scoped_to_the_initiating_pane`: retained one idle tick each because the tested behaviour is the continued absence of a reopened hierarchy screen after Escape.
- `test_cordon_does_not_apply_to_pods`: retained `0.2s` because the wrong-view node-op path is defined by the lack of any dialog or write dispatch.
- `test_cancelled_in_flight_eviction_that_lands_is_counted`: retained `0.05s` because it is the deliberate ordering gap that lets cancellation land before the held in-flight eviction is released.
- `test_resize_only_applies_to_pods`: retained `0.2s` because the wrong-kind resize path is an inert absence test with no stronger post-action predicate.
- Current retained-pause totals: `14` calls overall, `2.15s` explicit duration, `4` no-arg idle ticks.

## Mutation / regression evidence

- Temporary mutation: scheduled `asyncio.get_running_loop().call_later(0.05, rec.calls.append, ("mutant-delete", "pods", "default", "web-1"))` inside both restored delayed-no-write tests, then removed it before final verification.
- Mutated command: `uv run --no-sync pytest -p no:tach tests/ui/test_write_ops.py::test_ctrl_d_cancelled_does_nothing tests/ui/test_write_ops.py::test_unwritable_audit_blocks_write -q`
- Mutated result: `2 failed in 1.46s` — both tests caught the injected delayed dispatch during the restored `0.2s` / `0.3s` absence windows.
- Post-restore confirmation: reran the focused affected suite below and returned to green.

## Verification

- Focused affected pytest: `uv run --no-sync pytest -p no:tach tests/ui/test_hierarchy_nav.py::test_explicit_navigation_clears_the_hierarchy_return tests/ui/test_hierarchy_nav.py::test_tree_does_not_open_when_view_changed_during_fetch tests/ui/test_write_ops.py::test_ctrl_d_cancelled_does_nothing tests/ui/test_write_ops.py::test_unwritable_audit_blocks_write tests/ui/test_node_shell.py::test_s_on_nodes_view_opens_privileged_approval_dialog -q` → `5 passed in 4.74s`
- Baseline before edits: `uv run --no-sync pytest -p no:tach tests/ui/test_write_ops.py tests/ui/test_agent_write.py tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py tests/ui/test_describe.py tests/ui/test_resize_flow.py tests/ui/test_node_shell.py -q` → `190 passed in 136.66s (0:02:16)`
- Final seven-file pytest: `uv run --no-sync pytest -p no:tach tests/ui/test_write_ops.py tests/ui/test_agent_write.py tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py tests/ui/test_describe.py tests/ui/test_resize_flow.py tests/ui/test_node_shell.py -q` → `185 passed in 134.32s (0:02:14)`
- Final lint: `uv run --no-sync ruff check tests/ui/test_write_ops.py tests/ui/test_agent_write.py tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py tests/ui/test_describe.py tests/ui/test_resize_flow.py tests/ui/test_node_shell.py` → `All checks passed!`
- Final format check: `uv run --no-sync ruff format --check tests/ui/test_write_ops.py tests/ui/test_agent_write.py tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py tests/ui/test_describe.py tests/ui/test_resize_flow.py tests/ui/test_node_shell.py` → `7 files already formatted`

## Final review resolution

- The only remaining no-arg idle ticks in `tests/ui/test_hierarchy_nav.py` are now paired with explicit post-action state assertions:
  - final Escape in `test_escape_after_goto_reopens_the_tree_on_the_release_view` ⇒ `app.current_kind == "helmreleases"`
  - final Escape in `test_explicit_navigation_clears_the_hierarchy_return` ⇒ `app.current_kind == "pods"`
  - pane-return pause in `test_hierarchy_return_is_scoped_to_the_initiating_pane` already asserted the pane stayed on `deployments`
- No product code changed.
- Validation reran clean on the touched hierarchy tests and the full Task 3 suite.

## Commit

- Functional commit: `7f32c3d` — `test: preserve delayed no-write assertions`
- Co-authored-by trailer included as required.

## Self-review

- Replaced every remaining non-semantic startup/navigation sleep in scope with explicit row, screen, notification, worker, subprocess, audit, or empty-target-render predicates.
- Kept only genuine absence/ordering/timing windows where elapsed time is part of the behaviour under test, including the two restored delayed no-write windows requested in review.
- Preserved approval-gate invariants explicitly: no write before confirmation, no write after cancel, and no write if audit intent persistence fails.
- Left product code unchanged.

## Follow-up: hierarchy worker lifecycle contract

- `test_tree_does_not_open_when_view_changed_during_fetch` now snapshots the pre-Enter worker identities, waits for a newly observed hierarchy fetch worker while `slow_components` is parked, captures that worker tuple, navigates away, releases the gate, and waits for the captured worker(s) to finish before asserting the tree stayed closed and `current_kind` remained `pods`.
- Added mutation evidence in `test_tree_fetch_worker_start_wait_rejects_vacuous_empty_capture`: `run_worker` is patched to discard the coroutine without launching the fetch, the new start wait times out, and the empty-capture `all([])` shape still returns `True`.
- Validation rerun in the worktree:
  - focused hierarchy test: passed twice
  - full `tests/ui/test_hierarchy_nav.py`: passed
  - seven-file UI suite: passed
  - `ruff check` and `ruff format --check` on `tests/ui/test_hierarchy_nav.py`: passed
