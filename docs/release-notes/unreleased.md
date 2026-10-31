# Unreleased (main)

This page tracks changes on `main` after the v0.6.1 release candidate.

The [latest published release](https://github.com/hellices/korvid/releases/latest)
remains the stable install until v0.6.1's publication is verified. See the
[v0.6.1 release notes](v0.6.1.md) for the candidate summary. The
[current release milestone, v0.7.0](https://github.com/hellices/korvid/milestone/9)
tracks the next release scope.

## Configuration propagation

- `:consumers` (or **Explain configuration propagation** in the Action
  Palette) shows which Pods use the selected ConfigMap or Secret: through
  environment variables, a mounted volume, a `subPath` mount, an unmounted
  volume or an image pull secret, and what a change means for each. It reads
  only the object's metadata and the namespace's Pods, and says when the set
  of consumers is not established. See
  [Configuration propagation](../tui.md#configuration-propagation).
- Reopening `:consumers` after the object's resource version moved shows
  whether the change followed each environment variable or `subPath`
  consumer's container start, judged with a one-minute allowance for clock
  skew. Only changes seen between two openings in the same session count.

## Workload creation failures

- `:blocked` (or **Explain workload creation failures** in the Action
  Palette) shows why a Deployment, ReplicaSet, StatefulSet, DaemonSet, Job or
  CronJob has no Pods: the `FailedCreate` rejections recorded for it, next to
  its namespace's ResourceQuotas and LimitRanges, with quantities compared
  exactly and absence said only from complete lists. A **Possible
  constraints** section checks what the next Pod, and a StatefulSet's new
  claims, ask for against those policies as now observed. Each row is a
  possibility, not a cause. See
  [Workload creation failures](../tui.md#workload-creation-failures).

## Kinds served during a session

- A CustomResourceDefinition installed while korvid runs opens through `:`
  without a restart. An unknown `:` command reads the API resources again
  before it is reported, at most once every 30 seconds. `:rediscover` (or
  **Refresh API resources** in the Action Palette) reads them now and says
  which kinds were added, removed or moved to another version. Group versions
  that cannot be read keep the kinds korvid already knew. See
  [Read the cockpit](../tui.md#read-the-cockpit).

## MCP tool hints

- `tools/list` now gives every MCP tool a title and annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
  derived from what the tool touches, so a host can allow read-only tools
  without asking. Approval does not rely on them: write proposals still wait
  for a keystroke in the TUI. See [Tool hints](../mcp.md#tool-hints).

## Current configuration and extension contracts

Existing migration links continue in the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).
