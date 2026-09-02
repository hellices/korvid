# Pod Transfer UID Revalidation Design

**Issue:** #334

**Status:** Approved for implementation

**Scope:** Local file transfer and the shared pre-execution Pod identity guard

## Problem

File transfer captures a Pod UID when the dialog opens and re-reads the Pod
before starting the name-based exec. Initial summaries can contain no UID, and
the manifest lookup helper deliberately returns `None` for timeouts and
infrastructure errors. Missing captured UIDs bypassed the shared
`pod_uid_unchanged` guard, while missing final UIDs were treated as success, so
an operation could execute by name without proving it still targeted the
approved Pod incarnation.

The guard must distinguish three outcomes:

1. the Pod no longer exists;
2. the Pod exists with a different UID;
3. the current UID cannot be retrieved.

Only a retrieved UID equal to the captured UID permits execution.

## Approaches Considered

### 1. Make the shared Pod UID guard fail closed

Treat `None` from the injected target UID lookup as a retryable verification
failure in `ResourceInspectController.pod_uid_unchanged`. Keep the existing
messages for deletion and replacement.

This is the recommended approach. It is the smallest change, keeps identity
interpretation at the existing boundary, and also protects `kubectl debug`,
which uses the same name-based execution and the same guard.

### 2. Add a transfer-only guard

Duplicate the UID lookup and outcome handling in the transfer path while
leaving the shared helper unchanged.

This limits the immediate behavior change but duplicates a security invariant
and leaves the same unavailable-lookup risk in `kubectl debug`.

### 3. Return a typed revalidation result

Replace the Boolean callback with an enum describing matched, missing,
replaced, and unavailable outcomes, then let each controller render messages.

This gives richer typing but adds interface and fake churn without a current
consumer that needs policy beyond permit or refuse.

## Design

`ResourceInspectController.pod_uid_unchanged` remains the single pre-execution
identity boundary for transfer and debug:

- `ApiStatusError` from a confirmed missing Pod returns `False` and reports
  that the Pod no longer exists.
- A retrieved UID different from the captured UID returns `False` and reports
  that the Pod was replaced.
- A missing captured UID or a final lookup returning `None` returns `False`
  and reports that the Pod identity could not be verified and the user should
  retry.
- Only an exact non-`None` UID match returns `True`.

The final lookup stays bounded by the existing `_UID_LOOKUP_TIMEOUT`.
Infrastructure exceptions and timeouts continue to be normalized by
`_target_manifest`; the identity guard gives that normalized result a stricter
meaning for every name-based transfer or debug operation. The shared guard is
called unconditionally and requires both sides of the comparison to be
non-`None`.

Remote directory listing uses the exec API too, so its dialog-time UID guard
also rejects a missing captured UID rather than listing a potentially different
same-named Pod.

No changes are made to general write-target lookup behavior. Other writes
continue using their existing server-side UID preconditions and fail-open
lookup policy.

## Execution and Audit Ordering

`TransferController` keeps its current ordering:

1. require the exec opener and audit log;
2. revalidate the captured Pod UID;
3. append the intent audit entry;
4. start the exec stream;
5. append the outcome audit entry.

A missing captured UID or an unavailable, missing, or replaced Pod therefore
causes no exec call and no intent audit entry because no transfer was attempted.
Audit-log absence still blocks before identity lookup, and audit append failure
still blocks before exec. This preserves the existing fail-closed audit
guarantees.

## User Experience

The unavailable-lookup notification is distinct and retryable:

> Transfer cancelled - pod `<name>` could not be verified. Retry when the
> cluster is reachable.

The action prefix remains parameterized, so the same guard produces an
equivalent `kubectl debug cancelled` message for debug execution.

Deletion and replacement retain their existing messages so users can
differentiate target lifecycle changes from transient control-plane failure.

## Testing

TDD regressions cover both upload and download final revalidation:

- a missing captured UID blocks transfer, debug, and remote listing before
  exec;
- a timeout-normalized lookup result blocks before audit and exec;
- an infrastructure-error-normalized lookup result blocks before audit and
  exec;
- the notification contains retry guidance and does not claim deletion or
  replacement;
- an exact UID match still proceeds;
- confirmed deletion and UID replacement remain distinct.

The shared helper behavior is also pinned directly so debug cannot regress to
fail-open if transfer wiring changes later.

Targeted validation covers transfer UI, transfer controller, transfer picker,
debug identity tests, Ruff, formatting, mypy for changed source, and Tach.

## Non-goals

- Retrying automatically inside the transfer worker.
- Extending or removing the UID lookup timeout.
- Changing precondition behavior for unrelated Kubernetes writes.
- Auditing a transfer intent when identity revalidation prevents any attempt.
