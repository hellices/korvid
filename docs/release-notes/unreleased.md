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
  exactly and absence said only from complete lists. See
  [Workload creation failures](../tui.md#workload-creation-failures).

## Current configuration and extension contracts

Existing migration links continue in the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).
