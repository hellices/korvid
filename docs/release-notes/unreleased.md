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
- The walkthrough runs in its own kubeconfig file, so its `kubectl` and korvid
  commands cannot reach your other clusters. The workloads need no host access
  or API token, and the whole cluster is deleted with one command.

See [Practice on a throwaway cluster](../getting-started.md#practice-on-a-throwaway-cluster).

## Kubernetes support

- A new [Kubernetes support](../kubernetes-support.md) page defines which
  Kubernetes versions korvid supports and what "verified" means. The oldest
  supported version is 1.33.
- A version matrix verifies 1.33, 1.34, 1.35 and 1.36 on throwaway kind
  clusters, with read-only scenarios for API version resolution, unavailable
  APIs, denied principals, and ConfigMap and Secret metadata that must never
  carry their data.

## Current configuration and extension contracts

Existing migration links continue in the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).
