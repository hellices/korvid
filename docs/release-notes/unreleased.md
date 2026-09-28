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
[#404](https://github.com/hellices/korvid/issues/404); automatic namespace
assignment, post-action Deployment observation, and Pod comparison remain
separate milestone work, not features delivered by this change.

## Current configuration and extension contracts

Existing migration links now continue at the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).
