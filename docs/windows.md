# Windows contributor notes

Native Windows development is supported for dependency sync and the full test
suite:

```sh
uv sync --dev --all-extras
uv run pytest -q
```

PRs that touch shared/runtime behavior must also keep the required
`windows-test` CI job green. It is enforced by the repository's default-branch
ruleset alongside the Linux Python matrix and pre-commit. Defining a workflow
job alone does not make it a required merge check.

## Native terminal validation

Before the full suite, the Windows job runs:

```sh
uv run pytest -p no:tach tests/windows/test_native_terminal.py -q
```

This smoke test launches the real TUI in a Windows ConPTY and sends terminal
input rather than using Textual's headless `run_test()` driver. It checks
startup, help/filter input, terminal handoff through the production shell
controller to a fixture `kubectl.exe`, shell input, return to a responsive TUI,
and clean exit. It also checks driver threads and owned process/handle cleanup.
Process-wide handle totals are retained as diagnostic samples, not a leak
budget: UI state and runtime allocations also change those totals. The hard
checks require successful closure of every owned ConPTY/process/job handle,
stopped driver/reader threads, and exit of the launcher, TUI, and shell processes.
Failure blocks the Windows check; missing ConPTY on the Windows runner is not
silently skipped.

The smoke has a 150-second scenario budget and a five-minute CI step limit.
The `windows-native-terminal` CI artifact retains phase witnesses and the final
128 KiB of terminal output for seven days, including failed runs. To retain the
same evidence locally, set `KORVID_WINDOWS_SMOKE_ARTIFACT_DIR` to a writable
directory before running the command above.
The remaining Windows suite excludes this already-tested module, so the native
scenario runs only once, under the dedicated step's timeout and artifact capture.

Cluster data and the child-shell command are isolated fixtures. This is
native Windows terminal validation, **not a live-cluster verification** and
not an automated test of the Windows Terminal GUI. No personal kubeconfig,
credentials, or real-cluster mutations are used.

For a real deployment, also check the installed CLI (`korvid --help` and
`korvid --version`) and run korvid in Windows Terminal against an approved test
cluster. Verify navigation/filter input, logs, shell entry and return, resize,
and exit using that deployment's terminal, kubectl, authentication, and network
configuration. CI fixture success does not replace that check.

## Historical Windows baseline

The proving run for issue #173 was
`30936032385`: **3376 passed / 37 skipped / 0 failures**. The following counts
describe that historical run, not the current growing suite.

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
- The pinned Textual Windows driver supports terminal suspension. Headless or
  other non-suspending drivers can still take the `SuspendNotSupported` path;
  graceful refusal is expected there. Render-only tests pin
  `legacy_windows=False` for deterministic Rich output.
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
