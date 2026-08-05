# korvid v0.1.0 release runbook

This runbook is intentionally narrow: it covers the **first** public release,
`v0.1.0`. It is honest about what the workflow proves, what is irreversible,
and which recovery paths are safe to retry.

## One-time repository and publisher bindings

Before anyone publishes `v0.1.0`, confirm these external trust boundaries:

- GitHub tag protection covers `refs/tags/v*` with an immutable rule: only
  trusted release maintainers may create tags, and tag update/deletion is
  prohibited.
- The protected GitHub Actions environment is named `release`.
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
- Deleting or moving a published tag/version is not rollback. Treat it as an
  incident that needs diagnosis and a new version if the published artifacts are
  wrong.

## Dry run on `main` before tagging

Manual dry runs are non-publishing and only supported from `main`.

```sh
gh workflow run Release --ref main
gh run watch RUN_ID --exit-status
```

Replace `RUN_ID` with the workflow run you just started. Do not tag anything
until that dry run succeeds and you have recorded the exact reviewed commit you
intend to publish as `COMMIT`.

## Publish `v0.1.0`

Create the annotated tag from the reviewed commit, then push only that tag:

```sh
git tag -a v0.1.0 COMMIT -m "korvid v0.1.0"
git push origin refs/tags/v0.1.0
gh run watch RUN_ID --exit-status
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
```

If you also downloaded `SHA256SUMS`, verify that file locally before
redistributing artifacts.

## Install, reinstall, and uninstall from PyPI

The simplest first-release install is the full feature set:

```sh
python -m pip install 'korvid[all]==0.1.0'
```

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

## Retained local state after uninstall

Uninstalling the package does **not** delete operator data. By default, korvid
retains these paths:

- `~/.config/korvid/config.yaml`
- `~/.config/korvid/credentials.json` (fallback only when the OS keyring was
  unavailable or broken)
- `~/.local/state/korvid/audit.jsonl` (plus rotated `audit.jsonl.1`-`.3`
  backups when rotation has occurred)
- `~/.local/share/korvid/logs`
- `~/.local/share/korvid/agent-payloads`

If you set `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, or `XDG_DATA_HOME`, replace the
leading `~/.config`, `~/.local/state`, or `~/.local/share` prefixes
accordingly.

## opt-in cleanup

This opt-in cleanup is explicit. Only remove the retained paths if you deliberately want to discard local state:

```sh
rm -f ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json
rm -f ~/.local/state/korvid/audit.jsonl ~/.local/state/korvid/audit.jsonl.1 \
  ~/.local/state/korvid/audit.jsonl.2 ~/.local/state/korvid/audit.jsonl.3
rm -rf ~/.local/share/korvid/logs ~/.local/share/korvid/agent-payloads
```

The package uninstall command does not run that cleanup for you.

## First-release limitation

v0.1.0 cannot prove a cross-version PyPI upgrade because there is no earlier
PyPI release to upgrade from. The release workflow proves fresh installs of the
base, `agent`, `mcp`, and `all` variants plus package uninstall; it does not
yet prove upgrading an older published wheel in place. The next release must
validate upgrading from `0.1.0` before claiming a cross-version PyPI upgrade
path.
