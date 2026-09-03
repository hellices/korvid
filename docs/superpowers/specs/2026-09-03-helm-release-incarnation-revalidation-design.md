# Helm Release Incarnation Revalidation Design

**Issue:** #335
**Milestone:** v0.4.0

## Context

Korvid ships Helm upgrade, rollback, and uninstall as user-facing TUI
mutations. Each flow captures a namespace and release name before opening its
approval dialog, but Helm addresses the eventual mutation by those names only.
If a release is uninstalled and a different release is installed under the
same name while approval is pending, the approved command can mutate the new
release.

The Helm browser currently gives each release a synthetic
`helm:<namespace>/<name>` UID. That value is intentionally stable across
revisions so hierarchy navigation remains anchored, but it also remains stable
across uninstall and reinstall. It therefore cannot authorize a mutation.

## Scope

This change protects:

- Helm upgrade
- Helm rollback
- Helm uninstall

Helm install is excluded because it creates a release rather than mutating an
approved existing incarnation. Existing context-epoch, view-selection,
approval, write-reservation, and fail-closed audit behavior remains in place.

## Release Identity

Add an immutable `HelmReleaseIdentity` value in the Kubernetes layer:

```python
@dataclass(frozen=True)
class HelmReleaseIdentity:
    secret_uid: str
    revision: int
```

The identity describes the latest concrete Helm release Secret. Both fields
must be present and valid:

- `metadata.uid` must be a non-empty string.
- the Helm `version` label must parse to a positive integer.

`HelmReleaseSummary.uid` remains the stable synthetic navigation UID. A
separate optional `identity` field carries the concrete identity parsed from
the same latest Secret that produced the row. This avoids changing store keys,
hierarchy ownership, or existing release navigation.

`KubeClient.get_helm_release_identity(namespace, name)` performs an
authoritative LIST using the existing Helm Secret selectors, selects the
highest revision with the existing latest-release logic, and returns its
validated concrete identity. A missing release remains an `ApiStatusError(404)`;
malformed or missing identity is reported as unavailable rather than converted
into a synthetic value.

## Approval and Execution Flow

Each mutation captures the latest release identity before approval:

- Upgrade and uninstall use the selected `HelmReleaseSummary.identity`.
- Rollback uses the current `HelmReleaseSummary` for the selected revision's
  release when that row is already cached. If revision history was opened
  directly and no release row is cached, it calls
  `get_helm_release_identity` before approval and uses that authoritative
  identity. The selected historical revision UID is not sufficient because
  the command mutates the current release incarnation.

If the cached pre-approval identity is absent, or the direct authoritative
lookup fails or returns no identity, the flow stops before opening the approval
dialog and reports that release identity could not be verified.

`WriteGate.confirm` gains an optional asynchronous `precondition` callback.
The single `WriteCoordinator` implementation runs it after user approval and
inside the synchronous write reservation, but before the intent audit:

1. user approval;
2. synchronous write reservation;
3. asynchronous release-identity precondition;
4. fail-closed intent audit;
5. Helm mutation;
6. outcome audit and notification.

The reservation prevents a context switch while the authoritative lookup is
in flight. The callback compares the captured identity with
`get_helm_release_identity()` immediately before mutation. Exact equality is
the only success state.

The callback is optional so unrelated write flows retain their existing
behavior. It returns `True` to proceed and `False` after issuing its
domain-specific refusal notification. If it returns `False` or raises, the
coordinator does not append an intent audit and does not construct the
mutation coroutine. Unexpected exceptions are logged and surfaced as a
blocked write rather than swallowed.

## Failure Semantics

The Helm identity check fails closed when:

- the captured identity is missing or malformed;
- the current release no longer exists;
- the Kubernetes lookup times out or fails;
- the current Secret UID differs;
- the current revision differs.

A missing release reports that it no longer exists. Lookup errors and invalid
identity report that it could not be verified and should be retried when the
cluster is reachable. A mismatch reports that the release changed since
approval and requires refresh and retry.

These failures produce no Helm subprocess call and no intent audit. They do
not alter the selected row, retry automatically, or fall back to the synthetic
release UID.

## Wiring

The composition root injects the authoritative identity reader through
`KorvidApp` into `HelmController`. The controller depends on a narrow
`Callable[[str, str], Awaitable[HelmReleaseIdentity | None]]`, not on
`KubeClient` itself. Context switching continues to update the shared
Kubernetes client; the write reservation prevents a switch during
post-approval verification.

Adding the optional `WriteGate.confirm` parameter requires matching signatures
in its ABC, `WriteCoordinator`, and test fakes. It does not introduce a second
write path.

## Testing

Tests cover the boundary at each layer:

- Helm parsing extracts a valid concrete Secret UID and positive revision
  without changing the synthetic release UID.
- Kubernetes client identity lookup selects the latest revision and rejects
  missing or malformed identity.
- `WriteCoordinator` executes an async precondition after approval and before
  intent audit; false and raised outcomes create no audit or mutation.
- Upgrade, rollback, and uninstall proceed only for an exact identity match.
- Each mutation rejects a release removed and reinstalled under the same name.
- Missing captured identity, deleted release, lookup failure, and timeout are
  fail-closed.
- Composition-root and app/controller fakes preserve the typed wiring.

Targeted Helm and coordinator tests run during iteration. The final branch
must pass the full repository quality gate, architecture check, diff check,
and `uv.lock` hash comparison before a pull request is opened.
