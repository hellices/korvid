# Helm Release Incarnation Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent approved Helm upgrade, rollback, and uninstall operations from mutating a release that was removed and reinstalled under the same namespace/name.

**Architecture:** Preserve the synthetic release UID used by the UI, while attaching a concrete latest-Secret identity (`metadata.uid` plus revision) to release rows. Extend the single `WriteGate` implementation with an optional asynchronous precondition that runs inside the write reservation after approval and before intent audit, then have each existing-release Helm mutation re-fetch and compare the authoritative identity there.

**Tech Stack:** Python 3.11+, asyncio, Kubernetes HTTP client boundary, Textual, pytest, Ruff, mypy strict, Tach.

## Global Constraints

- `HelmReleaseSummary.uid` remains the stable synthetic navigation UID.
- A valid concrete identity requires a non-empty Secret `metadata.uid` and a positive integer Helm `version` label.
- Exact concrete identity equality is the only state that permits mutation.
- Missing captured identity, 404, timeout, API failure, malformed current identity, and identity mismatch all fail closed.
- A failed precondition produces no intent audit and does not construct the mutation coroutine.
- Helm install remains outside this change.
- Do not re-lock or modify `uv.lock`; use the root checkout's existing virtualenv for local checks.
- Keep all wiring in `src/korvid/__main__.py`; do not inject `KubeClient` into the UI layer.

---

### Task 1: Model and Fetch Concrete Helm Release Identity

**Files:**
- Modify: `src/korvid/k8s/helm.py`
- Modify: `src/korvid/k8s/client.py`
- Test: `tests/k8s/test_helm.py`

**Interfaces:**
- Produces: `HelmReleaseIdentity(secret_uid: str, revision: int)`.
- Produces: `release_identity_from_secret(secret: dict[str, Any]) -> HelmReleaseIdentity | None`.
- Produces: `HelmReleaseSummary.identity: HelmReleaseIdentity | None`.
- Produces: `KubeClient.get_helm_release_identity(namespace: str, name: str) -> HelmReleaseIdentity | None`.
- Preserves: `release_uid(namespace: str, name: str) -> str` and all existing store/hierarchy keys.

- [ ] **Step 1: Write failing parser tests**

Add assertions to `tests/k8s/test_helm.py` proving the concrete identity is
independent of the synthetic UID:

```python
from korvid.k8s.helm import HelmReleaseIdentity, release_identity_from_secret


def test_release_identity_uses_concrete_secret_uid_and_revision() -> None:
    secret = _secret("web", 3)

    assert release_identity_from_secret(secret) == HelmReleaseIdentity(
        secret_uid="secret-uid-web-3",
        revision=3,
    )
    release = release_from_secret(secret)
    assert release.uid == release_uid("default", "web")
    assert release.identity == HelmReleaseIdentity("secret-uid-web-3", 3)


@pytest.mark.parametrize(
    ("uid", "version"),
    [
        ("", "3"),
        (None, "3"),
        ("secret-uid", ""),
        ("secret-uid", "0"),
        ("secret-uid", "not-an-int"),
    ],
)
def test_release_identity_rejects_missing_or_invalid_facts(
    uid: str | None, version: str
) -> None:
    secret = _secret("web", 3)
    secret["metadata"]["uid"] = uid
    secret["metadata"]["labels"]["version"] = version

    assert release_identity_from_secret(secret) is None
    assert release_from_secret(secret).identity is None
```

- [ ] **Step 2: Run parser tests to verify RED**

Run:

```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/security-335-helm-release-identity
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/k8s/test_helm.py \
  -k 'release_identity' -q
```

Expected: collection or assertion failure because the identity type, parser,
and summary field do not exist.

- [ ] **Step 3: Implement the identity parser and summary field**

In `src/korvid/k8s/helm.py`, add the immutable value and parser near the Helm
summary models:

```python
@dataclass(frozen=True)
class HelmReleaseIdentity:
    """Concrete identity of the latest Secret backing a Helm release."""

    secret_uid: str
    revision: int


def release_identity_from_secret(secret: dict[str, Any]) -> HelmReleaseIdentity | None:
    """Validated concrete Secret identity, or None when facts are incomplete."""
    metadata = _mapping(secret.get("metadata"))
    labels = _mapping(metadata.get("labels"))
    secret_uid = metadata.get("uid")
    try:
        revision = int(labels.get("version") or 0)
    except (TypeError, ValueError):
        return None
    if not isinstance(secret_uid, str) or not secret_uid or revision <= 0:
        return None
    return HelmReleaseIdentity(secret_uid=secret_uid, revision=revision)
```

Add the optional field without changing `GenericSummary.uid`:

```python
@dataclass(frozen=True)
class HelmReleaseSummary(GenericSummary):
    revision: int = 0
    status: str = ""
    chart: str = "-"
    app_version: str = "-"
    identity: HelmReleaseIdentity | None = None
```

Set `identity=release_identity_from_secret(secret)` in
`release_from_secret()`.

- [ ] **Step 4: Run parser tests to verify GREEN**

Run the command from Step 2.

Expected: all selected tests pass.

- [ ] **Step 5: Write failing authoritative client tests**

In `tests/k8s/test_helm.py`, follow the existing mocked `_request_json` client
pattern:

```python
async def test_get_helm_release_identity_selects_latest_revision() -> None:
    client = KubeClient()
    response = {"items": [_secret("web", 1), _secret("web", 3), _secret("web", 2)]}
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=response)),
    ):
        identity = await client.get_helm_release_identity("default", "web")

    assert identity == HelmReleaseIdentity("secret-uid-web-3", 3)


async def test_get_helm_release_identity_returns_none_for_invalid_latest_secret() -> None:
    latest = _secret("web", 3)
    latest["metadata"]["uid"] = ""
    client = KubeClient()
    response = {"items": [_secret("web", 2), latest]}
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=response)),
    ):
        identity = await client.get_helm_release_identity("default", "web")

    assert identity is None


async def test_get_helm_release_identity_preserves_missing_release_404() -> None:
    client = KubeClient()
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value={"items": []})),
        pytest.raises(ApiStatusError, match="helm release .* not found"),
    ):
        await client.get_helm_release_identity("default", "ghost")
```

- [ ] **Step 6: Run client tests to verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/k8s/test_helm.py \
  -k 'get_helm_release_identity' -q
```

Expected: failure because `KubeClient.get_helm_release_identity` is absent.

- [ ] **Step 7: Implement the authoritative identity reader**

In `src/korvid/k8s/client.py`, reuse the existing private latest-Secret
selection:

```python
async def get_helm_release_identity(
    self, namespace: str, name: str
) -> HelmReleaseIdentity | None:
    """Concrete identity of the latest Secret backing a Helm release."""
    secret = await self._helm_release_secret(namespace, name)
    return release_identity_from_secret(secret)
```

Import `HelmReleaseIdentity` and `release_identity_from_secret` from
`korvid.k8s.helm`. Do not duplicate label parsing in the client.

- [ ] **Step 8: Run Task 1 tests and quality checks**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/k8s/test_helm.py -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/k8s/helm.py src/korvid/k8s/client.py tests/k8s/test_helm.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/k8s/helm.py src/korvid/k8s/client.py tests/k8s/test_helm.py
```

Expected: tests and both Ruff commands pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/korvid/k8s/helm.py src/korvid/k8s/client.py tests/k8s/test_helm.py
git commit -m "security: expose concrete Helm release identity" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Add an Async Precondition Before Intent Audit

**Files:**
- Modify: `src/korvid/ui/write_gate.py`
- Modify: `src/korvid/ui/write_coordinator.py`
- Modify: `tests/ui/test_write_coordinator.py`
- Modify: `tests/ui/test_resource_write_controller.py`

**Interfaces:**
- Produces: `WriteGate.confirm(..., precondition: Callable[[], Awaitable[bool]] | None = None)`.
- Produces: `WriteGate.run(..., *, precondition: Callable[[], Awaitable[bool]] | None = None)`.
- Contract: the precondition runs after approval, inside reservation, before intent audit and mutation construction.
- Contract: `False` yields `blocked: precondition failed`; an exception yields `blocked: precondition error`.

- [ ] **Step 1: Write failing coordinator ordering tests**

Add direct tests in `tests/ui/test_write_coordinator.py` using the existing
`make_env`/audit helpers:

```python
async def test_confirm_runs_async_precondition_before_intent_audit(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    order: list[str] = []

    async def precondition() -> bool:
        order.append("precondition")
        assert _audit_entries(env.audit_path) == []
        return True

    async def operation() -> None:
        order.append("operation")

    await env.writes.confirm(
        "Upgrade web?",
        "HELM UPGRADE web",
        action="helm-upgrade",
        meta=_HELM_META,
        namespace="default",
        name="web",
        op_factory=operation,
        precondition=precondition,
    )
    env.ui.answer(True)
    await env.ui.settle()

    assert order == ["precondition", "operation"]
    assert [entry["outcome"] for entry in _audit_entries(env.audit_path)] == [
        "intent",
        "success",
    ]


async def test_false_precondition_creates_no_audit_or_mutation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    mutated = False

    async def precondition() -> bool:
        return False

    async def operation() -> None:
        nonlocal mutated
        mutated = True

    await env.writes.confirm(
        "Upgrade web?",
        "HELM UPGRADE web",
        action="helm-upgrade",
        meta=_HELM_META,
        namespace="default",
        name="web",
        op_factory=operation,
        precondition=precondition,
    )
    env.ui.answer(True)
    await env.ui.settle()

    assert mutated is False
    assert _audit_entries(env.audit_path) == []


async def test_raised_precondition_creates_no_audit_or_mutation(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    async def precondition() -> bool:
        raise RuntimeError("identity backend failed")

    await env.writes.confirm(
        "Upgrade web?",
        "HELM UPGRADE web",
        action="helm-upgrade",
        meta=_HELM_META,
        namespace="default",
        name="web",
        op_factory=lambda: _unexpected_operation(),
        precondition=precondition,
    )
    env.ui.answer(True)
    await env.ui.settle()

    assert _audit_entries(env.audit_path) == []
    assert any("precondition" in message for message in env.ui.messages())
```

Adapt only helper names to the existing file; preserve the assertions and
ordering.

- [ ] **Step 2: Run the new coordinator tests to verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/ui/test_write_coordinator.py \
  -k 'precondition' -q
```

Expected: `TypeError` because `confirm` does not accept `precondition`.

- [ ] **Step 3: Extend the checked interface**

In `src/korvid/ui/write_gate.py`, add the optional keyword to `confirm` and
`run`:

```python
precondition: Callable[[], Awaitable[bool]] | None = None,
```

Document that it is a refusal-only callback: it runs only after approval and
cannot create a path around the gate.

Update the explicit `confirm` signature in
`tests/ui/test_resource_write_controller.py` with the same default. The
`StubGate` in `tests/ui/test_forward_controller.py` already accepts
`**kwargs` and needs no edit.

- [ ] **Step 4: Implement reserved precondition execution**

Thread `precondition` from `WriteCoordinator.confirm()` to `run()`, then into
`_run_write_inner()`. At the start of `_run_write_inner()`, before
`audit_write(..., "intent")`, add:

```python
if precondition is not None:
    try:
        permitted = await precondition()
    except Exception as exc:
        logger.exception("write precondition failed: %s", exc)
        self._ui.notify(
            f"{action} {kind}/{name} blocked: precondition could not be verified",
            severity="error",
        )
        return "blocked: precondition error"
    if not permitted:
        return "blocked: precondition failed"
```

Keep `self.reserved(...)` around the complete `_run_write` call so the
precondition executes while context switching is blocked. Do not call
`op_factory()` or `audit_write()` on either blocked path.

- [ ] **Step 5: Run coordinator and controller tests**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/ui/test_write_coordinator.py \
  tests/ui/test_resource_write_controller.py \
  tests/ui/test_forward_controller.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Run Task 2 type and style checks**

Run:

```bash
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/ui/write_gate.py src/korvid/ui/write_coordinator.py \
  tests/ui/test_write_coordinator.py tests/ui/test_resource_write_controller.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/ui/write_gate.py src/korvid/ui/write_coordinator.py \
  tests/ui/test_write_coordinator.py tests/ui/test_resource_write_controller.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy \
  src/korvid/ui/write_gate.py src/korvid/ui/write_coordinator.py
```

Expected: Ruff and mypy pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/korvid/ui/write_gate.py src/korvid/ui/write_coordinator.py \
  tests/ui/test_write_coordinator.py tests/ui/test_resource_write_controller.py
git commit -m "security: check write preconditions before audit" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Revalidate Helm Identity in Every Existing-Release Mutation

**Files:**
- Modify: `src/korvid/__main__.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/ui/helm_controller.py`
- Modify: `tests/test_main_wiring.py`
- Modify: `tests/ui/test_helm_actions.py`

**Interfaces:**
- Consumes: `HelmReleaseIdentity` and `KubeClient.get_helm_release_identity`.
- Consumes: `WriteGate.confirm(..., precondition=...)`.
- Produces: `KorvidApp(..., get_helm_release_identity: Callable[[str, str], Awaitable[HelmReleaseIdentity | None]] | None = None)`.
- Produces: `HelmController` identity reader dependency and
  `_release_identity_unchanged(...) -> bool`.

- [ ] **Step 1: Upgrade helper rows and app factory**

In `tests/ui/test_helm_actions.py`, make release rows concrete:

```python
def _release_row(
    name: str,
    chart: str = "nginx-18.1.0",
    *,
    secret_uid: str | None = None,
    revision: int = 3,
) -> HelmReleaseSummary:
    identity = (
        None
        if secret_uid == ""
        else HelmReleaseIdentity(secret_uid or f"secret-uid-{name}-{revision}", revision)
    )
    return HelmReleaseSummary(
        name=name,
        namespace="default",
        kind="HelmRelease",
        created="2026-07-26T10:00:00Z",
        uid=release_uid("default", name),
        revision=revision,
        status="deployed",
        chart=chart,
        app_version="1.27.0",
        identity=identity,
    )
```

Extend `make_app()` with an identity reader. Omitted input returns the
identity of the matching default release row; explicit callables allow each
test to return a replacement, `None`, 404, timeout, or runtime error:

```python
get_helm_release_identity: (
    Callable[[str, str], Awaitable[HelmReleaseIdentity | None]] | None
) = None,
```

Pass it to `KorvidApp`.

- [ ] **Step 2: Write failing upgrade replacement test**

Drive the existing upgrade wizard through approval, then make the
authoritative reader return another concrete UID:

```python
async def test_upgrade_rejects_reinstalled_release_after_approval(
    tmp_path: Path,
) -> None:
    helm = FakeHelm()

    async def current_identity(
        namespace: str, name: str
    ) -> HelmReleaseIdentity | None:
        return HelmReleaseIdentity("replacement-secret-uid", 1)

    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        helm=helm,
        audit_path=audit_path,
        get_helm_release_identity=current_identity,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("changed since it was approved" in n.message for n in app._notifications),
            label="replacement blocked",
        )

    assert not any(call[0] == "upgrade" for call in helm.calls)
    assert _audit_entries(audit_path) == []
```

- [ ] **Step 3: Write failing rollback and uninstall replacement tests**

Add parallel full-flow tests:

```python
async def test_rollback_rejects_reinstalled_release_after_approval(
    tmp_path: Path,
) -> None:
    helm = FakeHelm()

    async def current_identity(
        namespace: str, name: str
    ) -> HelmReleaseIdentity | None:
        return HelmReleaseIdentity("replacement-secret-uid", 1)

    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        helm=helm,
        audit_path=audit_path,
        get_helm_release_identity=current_identity,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("changed since it was approved" in n.message for n in app._notifications),
            label="replacement blocked",
        )

    assert ("rollback", "web", 2, "default") not in helm.calls
    assert _audit_entries(audit_path) == []


async def test_uninstall_rejects_reinstalled_release_after_approval(
    tmp_path: Path,
) -> None:
    helm = FakeHelm()

    async def current_identity(
        namespace: str, name: str
    ) -> HelmReleaseIdentity | None:
        return HelmReleaseIdentity("replacement-secret-uid", 1)

    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        helm=helm,
        audit_path=audit_path,
        get_helm_release_identity=current_identity,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        for ch in "web":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("changed since it was approved" in n.message for n in app._notifications),
            label="replacement blocked",
        )

    assert ("uninstall", "web", "default", False) not in helm.calls
    assert _audit_entries(audit_path) == []
```

- [ ] **Step 4: Run replacement tests to verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/ui/test_helm_actions.py \
  -k 'reinstalled_release_after_approval' -q
```

Expected: failures because identity wiring and revalidation do not exist.

- [ ] **Step 5: Wire the authoritative reader**

In `src/korvid/ui/app.py`:

```python
get_helm_release_identity: (
    Callable[[str, str], Awaitable[HelmReleaseIdentity | None]] | None
) = None,
```

Store it and pass a late-bound accessor to `HelmController`. In
`src/korvid/__main__.py`, wire:

```python
get_helm_release_identity=kube.get_helm_release_identity,
```

Update `tests/test_main_wiring.py` so its fake client exposes the same async
method and assert that the app receives the bound reader.

- [ ] **Step 6: Capture identity before approval**

In `HelmController`:

```python
_HELM_IDENTITY_TIMEOUT = 10.0
```

Accept the narrow reader accessor in the constructor. For upgrade and
uninstall, reject a selected `HelmReleaseSummary` whose `identity is None`.
For rollback, resolve the latest release row by `row.release` and reject when
that row or its identity is absent.

Thread the captured `HelmReleaseIdentity` through `_confirm_change`,
`rollback`, and `uninstall`; install continues to pass no identity.

- [ ] **Step 7: Implement the post-approval comparison**

Add a controller helper:

```python
async def _release_identity_unchanged(
    self,
    namespace: str,
    release: str,
    approved: HelmReleaseIdentity,
    *,
    action: str,
) -> bool:
    reader = self._get_release_identity()
    if reader is None:
        self._ui.notify(
            f"{action} cancelled - Helm release identity could not be verified",
            severity="warning",
        )
        return False
    try:
        current = await asyncio.wait_for(
            reader(namespace, release),
            timeout=_HELM_IDENTITY_TIMEOUT,
        )
    except ApiStatusError as exc:
        if exc.status == 404:
            self._ui.notify(
                f"{action} cancelled - release {release} no longer exists",
                severity="warning",
            )
            return False
        logger.warning("Helm release identity lookup failed", exc_info=True)
        current = None
    except TimeoutError:
        logger.warning("Helm release identity lookup timed out", exc_info=True)
        current = None
    if current is None:
        self._ui.notify(
            f"{action} cancelled - Helm release identity could not be verified; "
            "refresh and retry",
            severity="warning",
        )
        return False
    if current != approved:
        self._ui.notify(
            f"{action} cancelled - release {release} changed since it was approved; "
            "refresh and retry",
            severity="warning",
        )
        return False
    return True
```

Import `ApiStatusError`. Let unexpected non-Kubernetes exceptions reach the
coordinator's explicit precondition error boundary rather than silently
converting them.

Pass a `precondition` closure to `WriteGate.confirm` for upgrade, rollback,
and uninstall:

```python
precondition=lambda: self._release_identity_unchanged(
    namespace,
    release,
    approved_identity,
    action="Helm upgrade",
),
```

- [ ] **Step 8: Add fail-closed and success tests**

Define the expected lookup failures explicitly:

```python
async def _missing_identity(
    namespace: str, name: str
) -> HelmReleaseIdentity | None:
    return None


async def _deleted_release(
    namespace: str, name: str
) -> HelmReleaseIdentity | None:
    raise ApiStatusError(404, "Not Found")


async def _timed_out_identity(
    namespace: str, name: str
) -> HelmReleaseIdentity | None:
    raise TimeoutError


async def _failed_identity_lookup(
    namespace: str, name: str
) -> HelmReleaseIdentity | None:
    raise ApiStatusError(500, "Internal Server Error")


@pytest.mark.parametrize(
    "reader",
    [
        _missing_identity,
        _deleted_release,
        _timed_out_identity,
        _failed_identity_lookup,
    ],
)
async def test_upgrade_blocks_when_identity_cannot_be_verified(
    tmp_path: Path,
    reader: Callable[[str, str], Awaitable[HelmReleaseIdentity | None]],
) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        helm=helm,
        audit_path=audit_path,
        get_helm_release_identity=reader,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="identity failure blocked",
        )

    assert not any(call[0] == "upgrade" for call in helm.calls)
    assert _audit_entries(audit_path) == []
```

Repeat the same reader parameter matrix for rollback and uninstall using the
complete modal sequences from Step 3. Keep at least one full-flow exact-match
success assertion for upgrade, rollback, and uninstall. Assert:

- the expected Helm call occurs exactly once on match;
- no Helm mutation call occurs on failure;
- failure paths create no intent audit;
- success paths retain intent and success audit records;
- install does not call the identity reader.

- [ ] **Step 9: Run the complete Helm UI suite**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/ui/test_helm_actions.py tests/test_main_wiring.py -q
```

Expected: all tests pass.

- [ ] **Step 10: Run Task 3 style, type, and architecture checks**

Run:

```bash
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/__main__.py src/korvid/ui/app.py src/korvid/ui/helm_controller.py \
  tests/test_main_wiring.py tests/ui/test_helm_actions.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/__main__.py src/korvid/ui/app.py src/korvid/ui/helm_controller.py \
  tests/test_main_wiring.py tests/ui/test_helm_actions.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src
/Users/hwang-inhwan/workspace/kube/.venv/bin/tach check
```

Expected: all commands pass.

- [ ] **Step 11: Commit Task 3**

```bash
git add src/korvid/__main__.py src/korvid/ui/app.py \
  src/korvid/ui/helm_controller.py tests/test_main_wiring.py \
  tests/ui/test_helm_actions.py
git commit -m "security: revalidate Helm releases before mutation" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Full Verification, Review, and Pull Request

**Files:**
- Verify all changed files from Tasks 1-3.
- Update the design or plan only if implementation revealed a factual mismatch.

**Interfaces:**
- Consumes: all prior task deliverables.
- Produces: a clean, reviewed branch and a merge-ready pull request closing #335.

- [ ] **Step 1: Format touched Python files**

Run `ruff format` only on the Python files changed by this plan, then inspect
the diff. Do not format unrelated files.

- [ ] **Step 2: Run the full repository gate**

From the issue worktree, use the root virtualenv:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check src tests
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check src tests
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/tach check
git diff --check
test "$(git hash-object uv.lock)" = "$(git rev-parse HEAD:uv.lock)"
```

Expected: every command exits zero, the full pytest summary has zero failures,
and both `uv.lock` hashes match.

- [ ] **Step 3: Request an independent code review**

Invoke the repository review workflow against the complete branch diff. Fix
only credible correctness, security, architecture, or required-check findings.
For each fix, add a failing regression first, rerun the targeted test, and
create a separate non-amended commit.

- [ ] **Step 4: Confirm branch integrity**

Run:

```bash
git status --short
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
```

Expected: clean worktree, the design/implementation commits are present, and
the diff contains only #335 files.

- [ ] **Step 5: Push and open the pull request**

Push `security/335-helm-release-identity` normally, without force, and create a
PR titled:

```text
security: revalidate Helm releases before mutation
```

The body must summarize the concrete Secret identity, the approval-to-audit
ordering, replacement-release regressions, full gate results, and include
`Closes #335`.

- [ ] **Step 6: Complete the review loop**

Read every review comment, including suppressed low-confidence findings.
Evaluate each against the codebase before changing code. For every credible
finding:

1. add a failing regression;
2. implement the focused fix;
3. run targeted checks and the full `make check` gate;
4. create a new commit without amend or force-push;
5. reply in the inline thread naming the commit and test;
6. resolve the thread;
7. re-request Copilot review.

Stop speculative changes after two consecutive low-confidence-only rounds.

- [ ] **Step 7: Verify remote merge readiness**

Run:

```bash
gh pr view <PR_NUMBER> --json statusCheckRollup,reviewDecision,mergeStateStatus
```

Expected: every required check is `SUCCESS`, all credible threads are
resolved, and the latest reviewer result has no blocking finding. Do not merge
the PR automatically; report merge readiness to the user with a concise
in-chat development report.
