# Task 1 report

## What changed

- Added `src/korvid/agent/interaction.py` with frozen/slotted interaction contracts:
  - `ResourceIdentity`
  - `PaneContext`
  - `ClusterFacts`
  - `InteractionContext`
  - `Navigate`
  - `SetFilter`
  - `SelectResource`
  - `FocusPane`
  - `OpenLogs`
  - `OpenDescribe`
  - `DrillDown`
  - `OpenEvidence`
  - `UiAction`
  - `UiActionResult`
  - `AgentUiBridge`
- Exported the new contracts from `src/korvid/agent/__init__.py`.
- Added `tests/agent/test_interaction.py` covering immutability, validation, and bridge behavior.

## RED

Command:

```bash
uv run pytest -p no:tach tests/agent/test_interaction.py -q
```

Output:

```text
ERROR collecting tests/agent/test_interaction.py
E   ModuleNotFoundError: No module named 'korvid.agent.interaction'
```

Why it failed: the interaction module did not exist yet, so test collection could not import the contract types.

## GREEN

Commands and results:

```bash
uv run pytest -p no:tach tests/agent/test_interaction.py -q
```

```text
10 passed in 0.04s
```

```bash
uv run ruff check src/korvid/agent/interaction.py tests/agent/test_interaction.py
```

```text
All checks passed!
```

```bash
uv run mypy src/korvid/agent/interaction.py
```

```text
Success: no issues found in 1 source file
```

Formatting:

```bash
uv run ruff format src/korvid/agent/interaction.py src/korvid/agent/__init__.py tests/agent/test_interaction.py
```

```text
1 file reformatted, 2 files left unchanged
```

## Self-review

- Contracts are pure Python only; no Textual or application-service imports.
- All contract dataclasses are frozen and slotted.
- `AgentUiBridge` is an ABC with the required `snapshot()` and `apply()` methods.
- Validation is limited to the contract fields the brief called out.

## Concerns

- `OpenLogs`, `OpenDescribe`, `DrillDown`, and `OpenEvidence` are intentionally parameterless; later work can resolve the live selection from `InteractionContext`.
- `SelectResource.namespace` and `SelectResource.uid` remain permissive so cluster-scoped and not-yet-known identities can still be represented.

## Commit

- `e23c141` — `feat: define agent interaction contracts`

## Fix review findings

- Corrected the interaction contract shapes in `src/korvid/agent/interaction.py`:
  - `ResourceIdentity(kind, namespace, name, uid)` now uses `namespace: str | None` and `uid: str | None`.
  - `Navigate(view, namespace=None)` now defaults `namespace` to `None`.
  - `SelectResource(kind, name, namespace=None, uid=None)` now requires `kind` and `name`.
  - `OpenLogs(pod, namespace, container=None)`, `OpenDescribe(kind, name, namespace=None)`,
    `DrillDown(name)`, and `OpenEvidence(ref)` now carry their exact targets/ref and validate
    required nonblank fields.
  - Removed the redundant module-level `__all__` from `interaction.py`.
- Added focused tests in `tests/agent/test_interaction.py` for:
  - exact constructor signatures and defaults,
  - optional `None` values,
  - required-field validation,
  - explicit clear behavior for `SetFilter(None)`,
  - package-level export behavior, and
  - exact action payload values.

### RED

```bash
cd /private/tmp/korvid-pr314-main-sync && uv run pytest -p no:tach tests/agent/test_interaction.py -q
```

```text
12 failed, 11 passed in 0.07s
```

### GREEN

```bash
cd /private/tmp/korvid-pr314-main-sync && uv run pytest -p no:tach tests/agent/test_interaction.py -q
```

```text
23 passed in 0.04s
```

```bash
cd /private/tmp/korvid-pr314-main-sync && uv run ruff check src/korvid/agent/interaction.py tests/agent/test_interaction.py
```

```text
All checks passed!
```

```bash
cd /private/tmp/korvid-pr314-main-sync && uv run mypy src/korvid/agent/interaction.py
```

```text
Success: no issues found in 1 source file
```

### Files

- `src/korvid/agent/interaction.py`
- `tests/agent/test_interaction.py`

### Commit

- `a81f882` — `fix: align agent interaction contracts`

### Self-review

- Contract defaults now match the brief exactly.
- Typed actions carry explicit targets/ref instead of resolving live selection.
- No unrelated files changed.
