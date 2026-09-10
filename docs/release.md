# korvid release runbook

This runbook covers feature releases. `v0.1.2` is the first public PyPI
release. The current release version and supported upgrade source are read from
`pyproject.toml` by `scripts/release/release_config.py`. `v0.1.0` remains immutable,
unpublished audit history after its protected tag workflow failed before build,
attestation, staging, PyPI publication, or GitHub Release creation. `v0.1.1`
is unpublished audit history for a different reason: it built and staged, then
stopped at `publish-pypi` because no PyPI Trusted Publisher had been
registered. Its draft GitHub Release was never finalized.
This runbook is honest about what the workflow proves, what is irreversible,
and which recovery paths are safe to retry.

## One-time repository and publisher bindings

Before anyone publishes a feature release, confirm these external trust boundaries:

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

### Registering the Homebrew tap App

The source repository's release workflow needs a one-time GitHub App binding so
it can open the trusted Homebrew handoff PR without using a personal access
token:

- repository variable: `HOMEBREW_APP_ID`
- repository secret: `HOMEBREW_APP_PRIVATE_KEY`
- installation target: `hellices/homebrew-korvid`
- installation permissions: repository contents **write** and pull requests
  **write**

The workflow uploads `korvid.rb` to the source release **before** it checks
those values. If either binding is missing, the release keeps the formula asset
and then fails visibly at the handoff boundary, which is safe to fix and rerun.

### Registering the PyPI Trusted Publisher

This is the step `v0.1.1` died on, and it cannot be repaired from CI: it needs
an authenticated session on pypi.org. Before `v0.1.2`, the project was
registered through a **pending** publisher; it is now an active publisher
binding on the existing `korvid` project. Verify that binding before each
release. The original registration values are retained below for recovery.

1. Sign in at <https://pypi.org/account/login/>. Two-factor authentication must
   be enabled on the account; PyPI requires it for anyone who can publish.
2. Open <https://pypi.org/manage/project/korvid/settings/publishing/>.
3. Verify the active GitHub publisher has these exact values:

   | field | value |
   | --- | --- |
   | PyPI Project Name | `korvid` |
   | Owner | `hellices` |
   | Repository name | `korvid` |
   | Workflow name | `release.yml` |
   | Environment name | `release` |

4. If the active publisher is absent or differs, add a GitHub publisher on that
   project page with the values above. Do not create a second project or a
   long-lived API token.

Every field is matched exactly against the OIDC token GitHub mints for the job.
`release.yml` is the file name, not a path and not the workflow's display name;
`release` is the environment the `publish-pypi` job declares. A mismatch in any
field fails the upload with `invalid-publisher`, which is a rejection by PyPI
rather than a bug in this repository — re-check the five values before
changing anything here.

No API token is created, and none should be: the workflow authenticates by
short-lived OIDC exchange, so there is no long-lived credential to leak.

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
set -eu
git fetch origin main
COMMIT=$(git rev-parse origin/main)
test -n "$COMMIT"
LOCAL_HEAD=$(git rev-parse HEAD)
if [ -z "$LOCAL_HEAD" ] || [ "$LOCAL_HEAD" != "$COMMIT" ]; then
  echo "checked-out HEAD $LOCAL_HEAD does not match reviewed origin/main commit $COMMIT; check out the reviewed commit before reading release metadata" >&2
  exit 1
fi
git update-index -q --refresh
if ! git diff --quiet --ignore-submodules --; then
  echo "tracked working tree has local modifications; commit, stash, or discard them before reading release metadata" >&2
  exit 1
fi
if ! git diff --cached --quiet --ignore-submodules --; then
  echo "index has staged tracked changes; commit, stash, or discard them before reading release metadata" >&2
  exit 1
fi
VERSION=$(python scripts/release/release_config.py version)
UPGRADE_SOURCE=$(python scripts/release/release_config.py upgrade-source)
TAG="v$VERSION"
: "${VERSION:?release version is required}"
: "${UPGRADE_SOURCE:?release upgrade source is required}"
: "${TAG:?release tag is required}"
gh workflow run Release --ref main
RUN_ID=$(gh run list --workflow Release --limit 1 --json databaseId --jq '.[0].databaseId // empty')
test -n "$RUN_ID"
gh run watch "$RUN_ID" --exit-status
```

The gh run list command retrieves the run that was just queued. Confirm it is the
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
green dry run therefore **reduces but does not eliminate** publication risk.
The irreversible path succeeded for `v0.1.2`, but the new tag and candidate
artifacts are still revalidated at every publication boundary.

The dry run's source policy compares the checked-out `HEAD` against the live
`origin/main`, which the workflow re-fetches explicitly after checkout. A stale
dispatch SHA is rejected.

## Required cross-version upgrade gate

Download the exact wheel produced by the confirmed exact-main dry run; do not
substitute a local build or an artifact from another run:

```sh
: "${RUN_ID:?set RUN_ID to the confirmed dry-run workflow ID}"
: "${COMMIT:?set COMMIT to the reviewed origin/main SHA}"
: "${VERSION:?set VERSION via scripts/release/release_config.py version}"
: "${UPGRADE_SOURCE:?set UPGRADE_SOURCE via scripts/release/release_config.py upgrade-source}"
set -eu
DRY_RUN_COMMIT=$(gh run view "$RUN_ID" --json headSha --jq '.headSha') || exit 1
if [ -z "$DRY_RUN_COMMIT" ] || [ "$DRY_RUN_COMMIT" != "$COMMIT" ]; then
  echo "dry-run commit $DRY_RUN_COMMIT does not match reviewed commit $COMMIT" >&2
  exit 1
fi
candidate_dir="dist/dry-run-$RUN_ID"
gh run download "$RUN_ID" --name dist --dir "$candidate_dir"
CANDIDATE="$PWD/$candidate_dir/korvid-${VERSION}-py3-none-any.whl"
test -f "$CANDIDATE"
upgrade_root=$(mktemp -d)
uv venv --python 3.12 "$upgrade_root/venv"
upgrade_python="$upgrade_root/venv/bin/python"
upgrade_korvid="$upgrade_root/venv/bin/korvid"
uv pip install --python "$upgrade_python" "korvid[all]==${UPGRADE_SOURCE}"
"$upgrade_korvid" --version | grep -Fx "korvid ${UPGRADE_SOURCE}"
candidate_url=$("$upgrade_python" -c \
  'import pathlib, sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$CANDIDATE")
uv pip install --python "$upgrade_python" --upgrade \
  "korvid[all] @ $candidate_url"
"$upgrade_korvid" --version | grep -Fx "korvid ${VERSION}"
"$upgrade_korvid" --help >/dev/null
"$upgrade_python" -c \
  'import korvid.mcp.server, korvid.obs.prometheus, korvid.providers.litellm_factory'
runtime_root="$upgrade_root/runtime"
env HOME="$runtime_root/home" \
  XDG_CONFIG_HOME="$runtime_root/config" \
  XDG_DATA_HOME="$runtime_root/data" \
  XDG_STATE_HOME="$runtime_root/state" \
  "$upgrade_korvid" --version >/dev/null
test ! -e "$runtime_root"
```

Record the run ID, exact commit, and command result with the release evidence.
Do not create or push the release tag until this gate passes.

## Publish `$TAG`

Create the annotated tag from the reviewed commit, then push only that tag:

```sh
: "${COMMIT:?set COMMIT to the reviewed origin/main SHA}"
: "${VERSION:?set VERSION via scripts/release/release_config.py version}"
: "${TAG:?set TAG to v$VERSION}"
set -eu
if [ "$TAG" != "v$VERSION" ]; then
  echo "TAG $TAG does not match expected release tag v$VERSION; refusing to publish" >&2
  exit 1
fi
if git rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null; then
  echo "local tag $TAG already exists; refusing to push it" >&2
  exit 1
fi
if ! REVIEWED_VERSION="$(
  set -eu
  metadata=$(mktemp)
  trap 'rm -f "$metadata"' EXIT
  if ! git show "$COMMIT:pyproject.toml" >"$metadata" 2>/dev/null; then
    echo "could not read pyproject.toml from reviewed commit $COMMIT" >&2
    exit 1
  fi
  python scripts/release/release_config.py version --pyproject "$metadata"
)"; then
  exit 1
fi
if [ -z "$REVIEWED_VERSION" ] || [ "$REVIEWED_VERSION" != "$VERSION" ]; then
  echo "reviewed commit $COMMIT declares version $REVIEWED_VERSION, not requested VERSION $VERSION; refusing to publish" >&2
  exit 1
fi
git tag -a "$TAG" "$COMMIT" -m "korvid $TAG"
test "$(git rev-list -n 1 "refs/tags/$TAG")" = "$COMMIT"
git push origin "refs/tags/$TAG"
TAG_RUN_ID=
attempt=0
while [ -z "$TAG_RUN_ID" ] && [ "$attempt" -lt 30 ]; do
  TAG_RUN_ID=$(gh run list --workflow Release --event push \
    --branch "$TAG" --commit "$COMMIT" --limit 1 \
    --json databaseId --jq '.[0].databaseId // empty') || exit 1
  attempt=$((attempt + 1))
  [ -n "$TAG_RUN_ID" ] || sleep 2
done
test -n "$TAG_RUN_ID"
TAG_RUN_COMMIT=$(gh run view "$TAG_RUN_ID" --json headSha --jq '.headSha')
test "$TAG_RUN_COMMIT" = "$COMMIT"
gh run watch "$TAG_RUN_ID" --exit-status
```

This guard rereads only the reviewed commit's `pyproject.toml` to confirm the
operator's frozen `COMMIT` still declares the requested `VERSION`; it does not
replace the exact-main dry-run review or the cross-version upgrade gate above.

The push starts `.github/workflows/release.yml`, which revalidates the tag
before staging the draft GitHub Release, before PyPI publication, and before
publishing the final GitHub Release.

## Safe recovery boundaries

The workflow is intentionally idempotent only inside a narrow boundary:

- If the staged draft release already exists **and** the rerun proves the staged assets are byte-identical, it is safe to resume the idempotent workflow only when the staged assets match.
- If PyPI already has the selected release version but the matching draft release is missing, or if
  the staged assets differ, stop and diagnose.
- Do **not** attempt recovery by deleting or moving a published tag/version.

## Verify the published artifacts

After the workflow succeeds, download the release artifacts and verify the wheel
attestation from GitHub:

```sh
: "${VERSION:?set VERSION via scripts/release/release_config.py version}"
: "${TAG:?set TAG to v$VERSION}"
set -eu
gh release download "$TAG" --dir "dist/$TAG"
gh attestation verify "dist/$TAG/korvid-${VERSION}-py3-none-any.whl" --repo hellices/korvid
gh attestation verify "dist/$TAG/SHA256SUMS" --repo hellices/korvid
(cd "dist/$TAG" && shasum --algorithm 256 --check SHA256SUMS)
```

The attestation check establishes the provenance of `SHA256SUMS`; the final
command then verifies every built artifact listed in that manifest. The
separately generated `korvid.rb` formula is uploaded after collection and is
not covered by that manifest or its attestation.

## Publish and verify the Homebrew tap

The release workflow attaches `korvid.rb` to the GitHub Release with the source
repository `GITHUB_TOKEN`. It then requires `HOMEBREW_APP_ID` and
`HOMEBREW_APP_PRIVATE_KEY`, mints a short-lived GitHub App token scoped to
`hellices/homebrew-korvid`, and runs `scripts/release/update_homebrew_tap.py`.
The tap repository uses the same App identity for its trusted classifier and
stores:

- repository variable: `HOMEBREW_APP_ID`
- repository secret: `HOMEBREW_APP_PRIVATE_KEY`
- repository variable: `HOMEBREW_APP_SLUG`

The tap's trusted classifier uses `HOMEBREW_APP_SLUG`; the source workflow does
not store that slug locally. When you run the source-side verification snippet
below, export the tap repository's `HOMEBREW_APP_SLUG` value first. The source
workflow itself can keep using `actions/create-github-app-token`'s `app-slug`
output for the bot login it passes into `update_homebrew_tap.py`.

That handoff is safe to retry:

- if tap `main` already carries the same version, the source workflow exits
  cleanly and leaves any bottle-enriched formula alone;
- if tap `main` is newer, the handoff fails visibly rather than downgrade it;
- if the trusted `bump-korvid-${VERSION}` branch already exists and still
  changes only `Formula/korvid.rb`, the workflow reuses it;
- if the branch carries unrelated content or the App binding is missing, the
  job fails after preserving the formula release asset.

The tap's **default-branch validator** is what finishes trusted post-release
delivery there: it validates the PR, adds bottles, confirms the branch still
matches the reviewed head commit, and then from a trusted `workflow_run` gate
performs a one-shot `gh pr merge --squash --match-head-commit`. That avoids
approval carrying across later branch changes. Ordinary human-authored pull
requests remain manual in both repositories, and GitHub's persistent PR merge
enrollment can stay disabled.

After publication, find the trusted tap PR and wait for its checks:

```sh
: "${VERSION:?set VERSION via scripts/release/release_config.py version}"
: "${HOMEBREW_APP_SLUG:?set to the GitHub App slug bound to the tap}"
set -eu
case "$HOMEBREW_APP_SLUG" in
  *'[bot]') EXPECTED_BOT_LOGIN="$HOMEBREW_APP_SLUG" ;;
  *) EXPECTED_BOT_LOGIN="${HOMEBREW_APP_SLUG}[bot]" ;;
esac
TAP_PR=$(gh api "repos/hellices/homebrew-korvid/pulls?state=open&head=hellices:bump-korvid-${VERSION}&base=main" \
  --jq "map(select(
    .title == \"korvid ${VERSION}\" and
    .base.ref == \"main\" and
    .head.ref == \"bump-korvid-${VERSION}\" and
    .head.repo.owner.login == \"hellices\"
  )) | if length == 1 then .[0].number else empty end")
if [ -z "$TAP_PR" ]; then
  echo "trusted bump-korvid-${VERSION} tap PR not found" >&2
  exit 1
fi
AUTHOR_LOGIN=$(gh api "repos/hellices/homebrew-korvid/pulls?state=open&head=hellices:bump-korvid-${VERSION}&base=main" \
  --jq "map(select(
    .title == \"korvid ${VERSION}\" and
    .base.ref == \"main\" and
    .head.ref == \"bump-korvid-${VERSION}\" and
    .head.repo.owner.login == \"hellices\"
  )) | if length == 1 then .[0].user.login // empty else empty end")
if [ -z "$AUTHOR_LOGIN" ] || [ "$AUTHOR_LOGIN" != "$EXPECTED_BOT_LOGIN" ]; then
  echo "trusted bump-korvid-${VERSION} tap PR author $AUTHOR_LOGIN does not match expected $EXPECTED_BOT_LOGIN" >&2
  exit 1
fi
gh pr checks "$TAP_PR" --repo hellices/homebrew-korvid --watch || exit 1
gh pr view "$TAP_PR" --repo hellices/homebrew-korvid --json \
  number,title,headRefName,baseRefName,state,isDraft,mergedAt,statusCheckRollup
```

Its trust basis remains the same: the formula is generated from the
tag-revalidated `uv.lock` after publication, but it is not separately attested
or listed in `SHA256SUMS`.

Finally verify the tap, not merely the formula attached to the source release:

```sh
: "${VERSION:?set VERSION via scripts/release/release_config.py version}"
set -eu
brew update
brew upgrade hellices/korvid/korvid || brew install hellices/korvid/korvid
korvid --version | grep -Fx "korvid ${VERSION}"
brew test hellices/korvid/korvid
```

## Install, reinstall, and uninstall from PyPI

These commands use isolated tool-managed environments. Use the installer that
owns the existing korvid environment so reinstall, extra expansion, and
uninstall all target the same application.

The simplest install is the full feature set:

```sh
uv tool install 'korvid[all]'
# or
pipx install 'korvid[all]'
```

For unreleased `main` development, install from source instead:

```sh
uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'
# or
pipx install 'korvid[all] @ git+https://github.com/hellices/korvid'
```

Tagged versions should be installed from PyPI; the source install is only a
fallback for unreleased code.

If you already installed any narrower korvid requirement, reinstall the full
desired extra set:

```sh
uv tool install --force 'korvid[all]'
# or
pipx install --force 'korvid[all]'
```

To remove the package itself:

```sh
uv tool uninstall korvid
# or
pipx uninstall korvid
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
environment: install base `korvid`, then use pip's `--upgrade` inside that
disposable CI virtual environment and re-assert the same contract. The two
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

Stop all korvid processes, including any `korvid --mcp` server, before
removing retained files. Deleting a live lock file can let running processes
coordinate against different files, and deleting a live MCP registry entry
leaves external hosts without a discovery record.

Then remove the retained files:

```sh
# config and credentials are always under ~/.config/korvid
rm -f ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json
state_root="${XDG_STATE_HOME:-$HOME/.local/state}/korvid"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/korvid"
rm -f "$state_root/audit.jsonl" "$state_root/audit.jsonl.1" \
  "$state_root/audit.jsonl.2" "$state_root/audit.jsonl.3" \
  "$state_root/audit.jsonl.lock"
rm -f "$state_root/mcp-endpoint.json" "$state_root/mcp-endpoint.json.lock"
rm -rf "$data_root/logs" "$data_root/agent-payloads"
```

The package uninstall command does not run that cleanup for you.

## Remaining dry-run limits

No dry run can prove the publication path itself. Attestation, staging, PyPI
upload, finalization, compare-assets recovery, and pre-publication tag
revalidation remain tag-only boundaries. `v0.1.2` proved that path once;
each new release must still be supervised by a maintainer because its publication is
irreversible.
