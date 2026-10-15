# Unreleased (main)

This page tracks unreleased changes on `main` for the next release after `v0.5.0`.

The [latest published release](https://github.com/hellices/korvid/releases/latest)
remains the stable install. The
[current release milestone, v0.6.0](https://github.com/hellices/korvid/milestone/8)
tracks the next release scope, with cross-feature qualification in
[release tracker #421](https://github.com/hellices/korvid/issues/421).
See the [v0.5.0 notes](v0.5.0.md) for the published release.

## Keybinding editing

- Open `:keys` / `:keybindings`, or **Edit keybindings** in the Action Palette,
  to stage remaps, resolve conflict chains, swap keys, undo, and reset.
- Review the complete proposal, then explicitly Apply. Saving must succeed
  before live bindings change; cancellation or a failed save preserves the
  previous map and configuration.
- Startup and editing share context-aware validation, preserving legitimate
  key reuse in mutually exclusive views. Help, the top bar, and the Action
  Palette update with dispatch and remain consistent after restart.
- Reset affects only keybindings. Favorites and separately saved namespace
  state are preserved, numeric slots `1`–`9` remain reserved, and `0` remains
  remappable. Approval and fixed modal keys retain their protections.

See the [keybinding guide](../keybindings.md#remap-an-app-action). This implements
[#404](https://github.com/hellices/korvid/issues/404). Namespace assignment and
Deployment observation are documented separately below; Pod comparison remains
separate milestone work.

## Deployment operation outcomes

- An approved direct-TUI Deployment scale or rollout restart now starts bounded,
  read-only convergence observation only after the mutation and its success
  audit both complete.
- API acceptance and controller convergence are shown separately. Exact
  generation, replica, restart-marker, UID, condition, ReplicaSet, and Pod
  evidence classify completed, stalled, superseded, replaced, stopped, or
  incomplete outcomes; missing or denied evidence never becomes success.
- Open the latest retained result with `:outcomes`,
  `:deployment-outcomes`, or **Deployment operation result** in the Action
  Palette. Up to three results are retained, and active observation is bounded
  to roughly five minutes.
- Result-screen Pod actions revalidate the exact UID. Context switching stops
  old-cluster trackers before replacing the Kubernetes client, and app
  shutdown reaps the tracker workers.

This implements [#405](https://github.com/hellices/korvid/issues/405).

## Stable namespace shortcuts

- Keys `1`–`9` keep your `favorite_namespaces` pinned first. Each namespace
  you switch to with `:ns`, `:<view> <ns>` or the picker takes the next free
  slot, so the keys follow the namespaces you actually use.
- An automatic slot keeps its number across refreshes and restarts. The
  assignment is saved per cluster under `$XDG_STATE_HOME/korvid`, separately
  from `config.yaml` and keybindings.
- A namespace that leaves the listing keeps its slot as unavailable and the
  key is refused rather than reused. A failed or denied listing keeps the last
  known map and never probes namespaces individually.
- `?` and the `:ns` picker show the same slots the keys dispatch. `:slots` /
  `:ns-slots` previews a repack that reclaims unavailable slots and saves it
  only after you confirm.

See [namespace shortcuts](../tui.md#namespace-shortcuts). This implements
[#406](https://github.com/hellices/korvid/issues/406).

## Current configuration and extension contracts

Existing migration links now continue at the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).
