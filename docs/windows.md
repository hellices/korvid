# Windows contributor notes

Native Windows development is supported for dependency sync and the full test
suite:

```sh
uv sync --dev --all-extras
uv run pytest -q
```

PRs that touch shared/runtime behavior must also keep the required
`windows-test` CI job green. The proving run for issue #173 was
`30936032385`: **3376 passed / 37 skipped / 0 failures**.

## Expected skips on Windows

- **21 opt-in contract-suite skips** when `KORVID_CONTRACT_RUN_ID` is unset.
- **16 capability skips**:
  - **3 newly classified capability skips**: 2 `~user` POSIX account-lookup
    cases (`tests/core/test_transfer.py`, `tests/ui/test_transfer_picker.py`)
    and 1 POSIX directory-fsync failure case.
  - **13 pre-existing platform skips** for POSIX-only permission semantics:
    7 local transfer permission-bit cases, 3 audit-log mode cases,
    2 transfer-stream late-permission-loss cases, and 1 unreadable CA bundle
    permission case.

## Current Windows limits

- Symlink tests depend on Windows **Developer Mode** (or an elevated shell):
  native `Path.symlink_to()` can fail before korvid logic runs if symlink
  creation is not allowed. The shared helper turns that into a capability skip
  only when Windows symlink privilege is absent; the final hosted runner had privilege, so the count remained 37.
- Interactive shell attach can hit Textual's `SuspendNotSupported` path on
  Windows/non-suspending drivers. That refusal is expected; render-only tests
  pin `legacy_windows=False` for deterministic Rich output.
- NTFS uses ACLs, not POSIX mode bits. Tests verify atomic create/replace, the requested `0o600` mode, and durability semantics, but they do not claim ACL confidentiality.

## Windows-specific fixes covered by the green run

- Log export opens files with `newline=""`, preserving exact LF bytes instead
  of writing CRLF.
- Terminal/status text now stays ASCII-safe on cp1252-style consoles; the
  literal replacements are `->` and `--`:
  `korvid shell -> ...`, `korvid node shell -> ...`, and
  `korvid crashed -- restart? [Y/n]`.
- The write gate now takes an `op_factory`, so blocked or cancelled writes
  never create eager mutation coroutines; cancellation safety is
  cross-platform.
