# Kubernetes support

korvid supports the Kubernetes versions its version matrix verifies, and it
claims nothing for any other version. This page says what "verified" means,
which versions are verified, and how korvid copes with APIs that a cluster
does or does not serve.

## Supported versions

The oldest supported version is **Kubernetes 1.33**.

| Minor version | Status | Verified by |
| --- | --- | --- |
| 1.36 | Verified | the version matrix |
| 1.35 | Verified | the version matrix and the AKS contract cluster |
| 1.34 | Verified | the version matrix |
| 1.33 | Verified | the version matrix |

Each release's notes list the versions verified for that release.

## What "verified" means

- **Verified:** korvid's integration scenarios ran against a live API server
  of that minor version, and passed. The server is a
  [kind](https://kind.sigs.k8s.io/) cluster created by the version matrix, or
  the AKS cluster the write-path contract tests run against.
- **Fixture-compatible:** covered only by unit tests that use constructed API
  responses. Nothing has checked it against a live server.

A version the matrix only *runs* is not a support claim until it passes. No
version is currently listed as only fixture-compatible.

## Newer and missing APIs

korvid never decides what a cluster can do from its version number. It asks
the cluster's API discovery which resources each API group version serves,
and reads only versions korvid knows how to read:

- a resource served in several versions is read in the most preferred one
  korvid knows, even if that is not the cluster's preferred version;
- a resource served only in versions korvid does not know is reported as
  unsupported, never read as if it were a known one;
- an optional API that is not installed is reported as not served;
- when discovery itself fails or is refused, korvid reports the evidence as
  unavailable or denied, never as unsupported.

So a feature that needs a newer API keeps working on any supported cluster
that serves it in a version korvid knows how to read, and says why it cannot run on one that does not.

## How versions are verified

The **Kubernetes version matrix** workflow creates one kind cluster per minor
version on GitHub-hosted runners, so it adds no cloud cost, and runs the same
read-only scenarios against each:

- a resource served in a preferred and in a non-preferred version;
- a resource served only in a version korvid does not know;
- an optional API that is installed, and one that is not;
- an aggregated API that is registered but unavailable;
- metadata-only reads of ConfigMaps and Secrets created with `kubectl apply`,
  which must never return their data or the `last-applied-configuration`
  annotation;
- a principal denied a list, and a principal denied discovery;
- Events read by a principal allowed both API groups, which reads
  `events.k8s.io`; by one allowed only `events.k8s.io`; and by one allowed
  only core `v1`, which falls through to core after `events.k8s.io` is
  denied.

The workflow runs on demand, and whenever a pull request or a push to `main`
changes the modules those scenarios exercise or the project's dependencies. Each passing job records the
API server version it verified in the run's summary.

The matrix runs every minor version from the oldest supported one up. 1.35 is
also the AKS contract cluster's version, and 1.36 is the API generation
korvid's Kubernetes client targets.
