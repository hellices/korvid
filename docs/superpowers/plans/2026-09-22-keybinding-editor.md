# Keybinding Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver #404's keyboard-only, staged keybinding editor through a reviewed PR without changing the other v0.6.0 feature scopes.

**Architecture:** Share pure context-aware validation between startup and a reversible edit session. A constructor-injected UI controller derives its catalog from real bindings, persists only the keybinding section, and installs a complete keymap on explicit confirmation.

**Tech Stack:** Python 3.11+, Textual 8.2.8 from the existing lock, pytest/Pilot, PyYAML, Ruff, mypy, Tach.

## Global Constraints

- Follow [the feature design](../specs/2026-09-22-keybinding-editor-design.md).
- No dependency or lock changes, extra action registry, new key chord grammar, or public extension contract.
- Preserve approval, fail-closed audit, namespace slot reservations, independently optional extras, and unrelated settings.
- All constructor wiring remains in `src/korvid/__main__.py`; keep existing source-size gates unchanged.
- Use targeted RED/GREEN checks while iterating and the complete gate before committing review fixes.
- Open a PR and complete the repository's review loop; do not merge, tag, or publish.

---

### Task 1: Shared validation and static overlap

**Files:** Modify `src/korvid/core/keybindings.py`; add `tests/core/test_keybinding_contexts.py`.

**Interfaces:** Keep `plan_keybindings(raw, actions, priority_actions, reserved_keys, priority_reserved_keys)` backward compatible; add optional `action_contexts` mapping action names to frozen sets of `(group, plural)` resource identities. Both startup and editing consume this API.

- [x] RED: prove disjoint views can share a remapped key while an overlapping action cannot.

```python
actions = {"restart": ("r",), "rollback": ("b",)}
scopes = {"restart": frozenset({("apps", "deployments")}),
          "rollback": frozenset({("", "helmrevisions")})}
plan = plan_keybindings({"rollback": "r"}, actions, action_contexts=scopes)
assert plan.overrides == {"rollback": "r"}
assert not plan.warnings
```

- [x] Run `uv run --frozen pytest -p no:tach tests/core/test_keybinding_contexts.py -x -q`; confirm the missing behavior fails.
- [x] GREEN: apply the same overlap predicate to explicit duplicate checks and default-collision fixpoint validation. Canonicalize punctuation/shift aliases before either path.
- [x] Verify core context cases and `tests/core/test_keybindings.py`; cover globally overlapping and pane-capable actions without consulting a live view.

### Task 2: Reversible conflict planner

**Files:** Add `src/korvid/core/keymap_edit.py` and `tests/core/test_keymap_edit.py`.

**Interfaces:** `KeymapRules` holds the existing action/default/priority/reserved/context metadata and exposes `plan(overrides)`. `KeymapEdit(rules, overrides)` exposes a copied `overrides` proposal, `assign`, `reset`, `reset_all`, `undo`, `conflicts`, `suggestions`, and an optional valid conflict swap. It never accesses a file or UI.

- [x] RED: stage a three-action rotation, verify the intermediate conflict remains visible, then verify the final plan preserves all three changes.

```python
edit.assign("logs", "d")
assert edit.conflicts()
edit.assign("describe", "g")
edit.assign("relationships", "l")
assert not edit.conflicts()
assert edit.overrides == {"logs": "d", "describe": "g", "relationships": "l"}
assert edit.undo()
assert edit.conflicts()
```

- [x] Run `uv run --frozen pytest -p no:tach tests/core/test_keymap_edit.py -x -q` for RED.
- [x] GREEN: retain complete pending overrides and immutable history snapshots; validate a swap/suggestion through `KeymapRules.plan`, not an independent heuristic.
- [x] Verify occupied-default reset, reset-all, duplicate aliases, deterministic suggestion limits, backtracking, and invalid/protected edits.

### Task 3: Narrow atomic persistence

**Files:** Add `src/korvid/core/keybinding_config.py` and `tests/core/test_keybinding_config.py`.

**Interfaces:** `save_keybindings(path: Path, overrides: Mapping[str, str]) -> None` updates only the keybinding section using the existing atomic writer.

- [x] RED: write a fixture containing favorites, arbitrary automatic namespace state, and model settings; assert replacing and removing overrides preserves every other value.

```python
save_keybindings(path, {"logs": "ctrl+g"})
saved = yaml.safe_load(path.read_text())
assert saved["favorite_namespaces"] == ["prod", "dev"]
assert saved["keybindings"] == {"logs": "ctrl+g"}
save_keybindings(path, {})
assert "keybindings" not in yaml.safe_load(path.read_text())
```

- [x] Run `uv run --frozen pytest -p no:tach tests/core/test_keybinding_config.py -x -q` for RED.
- [x] GREEN: reread the current file, reject malformed/non-mapping data, preserve unrelated keys, and atomically replace only after serialization succeeds.
- [x] Verify a failed replace/read leaves the original bytes intact and a missing file creates a usable restrictive configuration.

### Task 4: Editor, controller, and complete live keymap

**Files:** Add focused modules `src/korvid/ui/keybinding_catalog.py`, `src/korvid/ui/keybinding_controller.py`, `src/korvid/ui/keybinding_surface.py`, and `src/korvid/ui/widgets/keybinding_editor.py`; add corresponding focused UI tests. Modify `app.py`, `app_runtime.py`, `action_policy.py`, `command.py`, `command_router.py`, `messages.py`, and `__main__.py` only at integration points.

**Interfaces:** Catalog derives `KeymapRules` from app bindings and dispatch view metadata. `KeybindingController.load()` handles startup; `open_editor()` opens a staged session; `apply(overrides)` returns a user-facing error or commits after persistence. Its surface builds/installs the complete binding-ID map and updates app effective overrides/config together. Persistence is constructor-injected.

- [x] RED: prove a context-separated remap retains untouched actions on the shared key and reset restores defaults in the running app.
- [x] RED: prove `:keys` and `:keybindings` share one typed zero-argument command, palette row, and guarded owner route.
- [x] RED: exercise keyboard staging, guided owner selection, suggestions/swap, undo, both resets, review invalidation, explicit apply, cancellation, and save failure.
- [x] GREEN: use one bounded modal and pure session; preflight the complete keymap, save, then activate without an asynchronous gap. Do not use a partial Textual overlay that deletes another contextual binding.
- [x] Update every affected command-router fake in the same change and retain constructor injection/source-size caps by extracting relevant keybinding shell logic.
- [x] Run focused core/UI suites plus `uv run --frozen tach check` after changing imports.

### Task 5: User-facing integration and review

**Files:** Update `docs/keybindings.md`, `docs/dev/capabilities.md`, and `docs/release-notes/unreleased.md`; add end-to-end assertions in `tests/ui/test_keybinding_editor_workflow.py`.

- [x] RED: after confirmed apply/reload/reset, assert actual dispatch, Help, top bar, and Action Palette all advertise the same effective keys.
- [x] Verify approval-modal isolation and immutable namespace reservations with a real Pilot workflow, not only planner assertions.
- [x] Document entry points, staged confirmation, reset boundaries, and failure behavior; replace the stale v0.5.0 candidate/milestone references without claiming the remaining v0.6.0 features ship.
- [x] Run touched-file Ruff, the full `make check`, coverage, documentation and configured pre-commit gates. Keep the lock unchanged.

**Post-commit delivery:** Record these steps in the PR rather than changing this plan after each external review round.

- Commit, push the existing session branch, and create a source PR closing #404 and relating #421/#406.
- Read every review body/thread, fix credible findings with RED/GREEN and full gates, reply per finding with commit/test evidence, resolve addressed threads, and request the next review when required.
- Stop after two consecutive suppressed-low-confidence-only rounds with no blocking findings. Verify all required checks on the exact head and report readiness without merging.
