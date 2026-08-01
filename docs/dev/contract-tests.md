# Live-cluster contract tests (issue #109)

`tests/contract/` is a live-cluster contract suite that proves korvid's k8s
layer against a real Kubernetes API server — dry-run previews cause no
persistent mutation, execute paths mutate exactly once, and RBAC behaves as
the permission probes assume. It exists because unit tests with faked
transports cannot catch bugs like #103 (a preview reaching the real mutation
path) or a client that silently swallows HTTP errors.

## How it runs

- **Fast PR gate is unchanged.** The suite is gated on the
  `KORVID_CONTRACT_RUN_ID` environment variable: without it every
  `contract`-marked test skips at collection time.
- **Post-merge gate.** `.github/workflows/k8s-contract.yml` runs on pushes to
  `main` (and protected manual dispatch) inside the `aks-contract-test`
  GitHub environment, which only `main` may use. PR and fork workflows can
  never obtain the Azure identity.
- Run ID `${run_id}-${run_attempt}` labels every fixture
  (`app.kubernetes.io/managed-by=korvid-contract`,
  `korvid.dev/contract-run=<id>`) so cleanup is idempotent; a janitor
  (`python -m tests.contract.janitor`) sweeps leftovers from interrupted runs
  and uncordons any node a crashed run left unschedulable.

## Test-only infrastructure

Everything below is **Korvid test-only infrastructure** — no production
workload runs there, and every resource is disposable:

| Resource | Name |
|---|---|
| Resource group | `rg-korvid-contract-test` (koreacentral) |
| AKS cluster | `aks-korvid-contract-test` (stopped at rest; started/stopped by the workflow; NAP disabled) |
| Managed identity | `id-korvid-contract-test` (OIDC-federated to the `aks-contract-test` environment; no stored kubeconfig, secret, or admin cert) |

Node pools:

- `system` — tainted `CriticalAddonsOnly=true:NoSchedule`, labelled
  `korvid.dev/pool=system`. Never targeted by tests.
- `workload` — labelled `korvid.dev/pool=workload` and
  `korvid.dev/disposable=true`. The **only** pool test pods schedule onto and
  the only node the node-operation tests (cordon/drain/evict) will touch;
  tests fail rather than fall back to a system node.

The cluster has AAD + Azure RBAC enabled with local accounts disabled; the
workflow identity holds a minimal custom role (read/start/stop/list-user-
credentials) plus AKS RBAC Cluster Admin, both scoped to the one cluster.
The provisioning Bicep and deployment record are attached to issue #109 —
the infrastructure code deliberately lives outside this repository.

## Running locally

You need data-plane access to the cluster (AKS RBAC role on it),
`kubelogin`, and Helm **3.15+** (`dry_run_install`/`dry_run_upgrade` pass
`--hide-secret` — older helm degrades to an error-only fallback render —
and `test_helm_release_lifecycle` skips silently when no
`helm` binary is on `PATH`):

```bash
az aks start -g rg-korvid-contract-test -n aks-korvid-contract-test
az aks get-credentials -g rg-korvid-contract-test -n aks-korvid-contract-test \
  -f /tmp/contract-kubeconfig --overwrite-existing
kubelogin convert-kubeconfig -l azurecli --kubeconfig /tmp/contract-kubeconfig

export KUBECONFIG=/tmp/contract-kubeconfig
uv run python -m tests.contract.janitor
KORVID_CONTRACT_RUN_ID=local-$USER uv run pytest -p no:randomly -m contract tests/contract/

az aks stop -g rg-korvid-contract-test -n aks-korvid-contract-test  # always
```

## Writing contract tests

The contract pattern every test follows:

1. **Preview** (`preview_*`, `dry_run_*`): call it, then **read state back
   from the API server** and prove nothing persisted (uid, resourceVersion,
   generation unchanged; no `deletionTimestamp`).
2. **Execute**: call the real write, read back, and prove the mutation
   happened **exactly once** (one generation bump, 404/409 on replay).
3. Previews answer `None` on any failure by design (they never block the
   approval flow), and they are pinned to a GET snapshot's resourceVersion —
   retry via `conftest.preview_until_settled` when a controller may be
   bumping the object concurrently.
4. RBAC scenarios mint short-lived TokenRequest tokens for throwaway
   ServiceAccounts; never legacy token Secrets.
5. Label every created object with `conftest.run_labels()` and create it in
   the per-test `namespace` fixture so teardown and the janitor can find it.
