## Summary

<!-- What changed and why. Link the issue it closes, e.g. "Closes #123". -->

## Verification

<!-- How you checked it: tests added, commands run, screenshots for UI changes. -->

## Checklist

- [ ] Tests cover the change, written to fail first where behavior changed.
- [ ] `make check` passes locally.
- [ ] User-facing docs and the README match the change, or nothing user-facing changed.
- [ ] No security invariant is weakened: writes still need a keystroke approval
      and a fail-closed audit entry, and sensitive reads are still masked.
