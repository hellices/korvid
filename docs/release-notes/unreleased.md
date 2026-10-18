# Unreleased (main)

This page tracks unreleased changes on `main` for the next release after `v0.6.0`.

The [latest published release](https://github.com/hellices/korvid/releases/latest),
v0.6.0, is the stable install; see the [v0.6.0 release notes](v0.6.0.md). The
[current release milestone, v0.7.0](https://github.com/hellices/korvid/milestone/9)
tracks the next release scope.

## Practice on a throwaway cluster

- Getting Started now walks through a disposable [kind](https://kind.sigs.k8s.io/)
  cluster with three small workloads, one healthy and two deliberately broken,
  to try the problems view, logs, relationships, an approved write, and the
  agent without risking a real cluster.
- The manifest is applied only to the `kind-korvid-playground` context, needs
  no host access or API token, and the whole cluster is deleted with one
  command.

See [Practice on a throwaway cluster](../getting-started.md#practice-on-a-throwaway-cluster).

## Current configuration and extension contracts

Existing migration links continue in the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).
