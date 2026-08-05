# korvid v0.1.0 release runbook

This runbook is intentionally narrow: it covers the **first** public release,
`v0.1.0`. It is honest about what the workflow proves, what is irreversible,
and which recovery paths are safe to retry.

## One-time repository and publisher bindings

Before anyone publishes `v0.1.0`, confirm these external trust boundaries:

- GitHub tag protection covers `refs/tags/v*` with an immutable rule: only
  trusted release maintainers may create tags, and tag update/deletion is
  prohibited.
- The protected GitHub Actions environment is named `release`. Its deployment
  policy must allow protected tags only, and its protection rules must require
  approval from a designated release maintainer.
- PyPI Trusted Publishing is bound exactly to:
  - repository: `hellices/korvid`
  - workflow file: `.github/workflows/release.yml`
  - environment: `release`

Those bindings are required because the in-workflow checks are defense in depth,
not a substitute for the external trust boundary.

## Irreversible boundaries

- annotated tag publication is irreversible in practice once pushed for a real
  release. Do not plan on moving or deleting a published release tag.
- PyPI publication is irreversible. A published version number cannot be reused
  for different artifacts.
- Build-provenance **attestation is irreversible**: `actions/attest-build-provenance`
  signs through the public Sigstore infrastructure and records the entry in the
  public Rekor transparency log. Those entries cannot be withdrawn, so the
  `attest` job is gated to tag pushes and never runs for a dry run.
- Deleting or moving a published tag/version is not rollback. Treat it as an
  incident that needs diagnosis and a new version if the published artifacts are
  wrong.

## Dry run on `main` before tagging

Manual dry runs are non-publishing and only supported from `main`.
`workflow_dispatch` is offered by GitHub only after this workflow file exists on
the repository's **default branch**, so the first dry run is possible only once
the release workflow has landed on `main`.

```sh
gh workflow run Release --ref main
RUN_ID=$(gh run list --workflow Release --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

The second command retrieves the run that was just queued. Confirm it is the
run you started (`gh run view "$RUN_ID"`) before relying on its result. Do not
tag anything until that dry run succeeds and you have recorded the exact
reviewed commit you intend to publish as `COMMIT`.

### What the dry run proves — and what it cannot

The dry run executes `verify`, `build`, `smoke`, `sbom`, `offline`, and
`collect`. It builds the same artifacts, runs the same 36-cell install matrix,
and assembles the same release file set.

It deliberately stops there. The dry run **does not exercise attestation**,
`stage-github-release`, PyPI publication, `finalize-github-release`, the
compare-assets recovery path (`scripts/release/compare_assets.py`), or the
pre-publication **tag revalidation** performed by
`check_source.py --expected-commit`. Those jobs require a tag push,
a protected environment approval, and irreversible external side effects. A
green dry run therefore **reduces but does not eliminate** first-publication
risk: the staging, publication, and finalization path is first exercised for
real during `v0.1.0`.

The dry run's source policy compares the checked-out `HEAD` against the live
`origin/main`, which the workflow re-fetches explicitly after checkout. A stale
dispatch SHA is rejected.

## Publish `v0.1.0`

Create the annotated tag from the reviewed commit, then push only that tag:

```sh
git tag -a v0.1.0 COMMIT -m "korvid v0.1.0"
git push origin refs/tags/v0.1.0
RUN_ID=$(gh run list --workflow Release --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

The push starts `.github/workflows/release.yml`, which revalidates the tag
before staging the draft GitHub Release, before PyPI publication, and before
publishing the final GitHub Release.

## Safe recovery boundaries

The workflow is intentionally idempotent only inside a narrow boundary:

- If the staged draft release already exists **and** the rerun proves the staged assets are byte-identical, it is safe to resume the idempotent workflow only when the staged assets match.
- If PyPI already has `0.1.0` but the matching draft release is missing, or if
  the staged assets differ, stop and diagnose.
- Do **not** attempt recovery by deleting or moving a published tag/version.

## Verify the published artifacts

After the workflow succeeds, download the release artifacts and verify the wheel
attestation from GitHub:

```sh
gh release download v0.1.0 --dir dist/v0.1.0
gh attestation verify dist/v0.1.0/korvid-0.1.0-py3-none-any.whl --repo hellices/korvid
gh attestation verify dist/v0.1.0/SHA256SUMS --repo hellices/korvid
(cd dist/v0.1.0 && shasum --algorithm 256 --check SHA256SUMS)
```

The attestation check establishes the provenance of `SHA256SUMS`; the final
command then verifies every downloaded release asset against that manifest.

## Install, reinstall, and uninstall from PyPI

The simplest first-release install is the full feature set:

```sh
python -m pip install 'korvid[all]==0.1.0'
```

During the brief window between this workflow landing on `main` and `v0.1.0`
appearing on PyPI, install from source instead:

```sh
python -m pip install 'korvid[all] @ git+https://github.com/hellices/korvid'
```

Once `v0.1.0` is published, PyPI is the release path and the source install is
only a fallback for unreleased code.

If you already installed `korvid`, `korvid[agent]`, or `korvid[mcp]`, rerun your package manager with the full desired extra set rather than assuming it will expand extras in place. With pip, the explicit reinstall/extra-expansion command is:

```sh
python -m pip install --upgrade 'korvid[all]==0.1.0'
```

With other installers, use their reinstall/upgrade equivalent or uninstall
first, then install the exact requirement you want.

To remove the package itself:

```sh
python -m pip uninstall -y korvid
```

## What the smoke matrix proves

For each of the 36 runner/Python/variant cells, the release workflow performs a
**fresh install of each variant**: one direct install of that variant's own
requirement (for example `korvid[mcp]`) from the single wheel built by this run,
into a brand-new virtual environment. It then asserts the reported version, that
the variant's feature packages import, that feature packages from *other* extras
are absent, that the `korvid` launcher answers `--help` and `--version`, that
uninstall removes the package and the launcher, and that a noninteractive run
leaves no user state behind.

Every non-base variant additionally gets a
**separate base-to-extra expansion check** in its own clean virtual
environment: install base `korvid`, then run the documented
`--upgrade 'korvid[extra]'` command and re-assert the same contract. The two
checks are independent, so a passing fresh install is never inferred from an
expansion (or vice versa).

`entra` is deliberately outside this matrix; it stays covered by the standard
test suite rather than the release smoke.

## Retained local state after uninstall

Uninstalling the package does **not** delete operator data. By default, korvid
retains these paths:

- `~/.config/korvid/config.yaml`
- `~/.config/korvid/credentials.json` (fallback only when the OS keyring was
  unavailable or broken)
- The OS keyring credential stored under service `korvid`, account
  `github-oauth` (when the keyring was available during GitHub Copilot login)
- `~/.local/state/korvid/audit.jsonl`, its sibling lock file
  `~/.local/state/korvid/audit.jsonl.lock`, and rotated `audit.jsonl.1`-`.3`
  backups when rotation has occurred
- `~/.local/state/korvid/mcp-endpoint.json` (MCP discovery registry) and its
  sibling lock file `~/.local/state/korvid/mcp-endpoint.json.lock`
- `~/.local/share/korvid/logs`
- `~/.local/share/korvid/agent-payloads`

Environment overrides are **not** uniform:

- `XDG_STATE_HOME` relocates the `~/.local/state/korvid` paths (audit log, MCP
  endpoint registry).
- `XDG_DATA_HOME` relocates the `~/.local/share/korvid` paths (log exports,
  agent payloads).
- `XDG_CONFIG_HOME` is not honored. `config.yaml` and the `credentials.json`
  fallback are always under `~/.config/korvid`, resolved from the user's home
  directory. Do not rewrite those two paths when cleaning up, or you will leave
  credentials behind.

## opt-in cleanup

This opt-in cleanup is explicit. Only remove the retained paths if you deliberately want to discard local state:

Remove the OS-keyring credential before uninstalling korvid and before deleting
the fallback file. Run this while the `keyring` dependency from `korvid[agent]`
is still installed. A missing entry is harmless; any other backend error remains
visible and must be resolved before continuing.

```sh
python - <<'PY'
import keyring

credential = keyring.get_password("korvid", "github-oauth")
if credential is not None:
    keyring.delete_password("korvid", "github-oauth")
PY
```

Then remove the retained files:

```sh
# config and credentials are always under ~/.config/korvid
rm -f ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json
# state honors XDG_STATE_HOME; substitute "${XDG_STATE_HOME:-$HOME/.local/state}"
rm -f ~/.local/state/korvid/audit.jsonl ~/.local/state/korvid/audit.jsonl.1 \
  ~/.local/state/korvid/audit.jsonl.2 ~/.local/state/korvid/audit.jsonl.3 \
  ~/.local/state/korvid/audit.jsonl.lock
rm -f ~/.local/state/korvid/mcp-endpoint.json \
  ~/.local/state/korvid/mcp-endpoint.json.lock
# data honors XDG_DATA_HOME; substitute "${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf ~/.local/share/korvid/logs ~/.local/share/korvid/agent-payloads
```

Stop korvid (including any `korvid --mcp` server) before removing
`mcp-endpoint.json`; deleting a live registry entry out from under a running
server leaves external MCP hosts without a discovery record.

The package uninstall command does not run that cleanup for you.

## First-release limitation

v0.1.0 cannot prove a cross-version PyPI upgrade because there is no earlier
PyPI release to upgrade from. The release workflow proves fresh installs of the
base, `agent`, `mcp`, and `all` variants plus package uninstall; it does not
yet prove upgrading an older published wheel in place. The next release must
validate upgrading from `0.1.0` before claiming a cross-version PyPI upgrade
path.

Nor can any dry run prove the publication path itself. Attestation, staging,
PyPI upload, finalization, compare-assets recovery, and pre-publication tag
revalidation are exercised for the first time during the real `v0.1.0` push.
Plan the first release as a supervised operation with a maintainer watching the
run, not as a rehearsed one.
