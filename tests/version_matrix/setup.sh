#!/usr/bin/env bash
# Install the version-matrix fixtures into a disposable cluster and add one
# kubeconfig context per principal the scenarios read as (#409, slice S5).
#
# Usage: setup.sh ADMIN_CONTEXT
#
# Only ever run against a cluster created for the run: it installs CRDs, an
# APIService and RBAC, and writes contexts into the current kubeconfig.
set -euo pipefail

admin=${1:?usage: setup.sh ADMIN_CONTEXT}
here=$(cd "$(dirname "$0")" && pwd)
kube=(kubectl --context "$admin")
cluster=$("${kube[@]}" config view -o jsonpath="{.contexts[?(@.name=='$admin')].context.cluster}")

"${kube[@]}" apply -f "$here/fixtures.yaml"
"${kube[@]}" wait --for=condition=Established --timeout=120s \
  crd/widgets.matrix.korvid.dev crd/gadgets.future.korvid.dev

"${kube[@]}" apply -f "$here/apiservice.yaml"
"${kube[@]}" wait --for=condition=Available=false --timeout=120s \
  apiservice/v1.unavailable.korvid.dev

for account in events-core events-group; do
  token=$("${kube[@]}" -n korvid-matrix create token "$account" --duration=1h)
  kubectl config set-credentials "matrix-$account" --token="$token" >/dev/null
  kubectl config set-context "matrix-$account" --cluster="$cluster" \
    --user="matrix-$account" --namespace=korvid-matrix >/dev/null
done

# A principal with no credentials: the API server treats it as anonymous,
# which may read no discovery at all. set-credentials merges into an
# existing user, so remove any earlier one to be sure it carries nothing.
kubectl config delete-user matrix-anonymous >/dev/null 2>&1 || true
kubectl config set-credentials matrix-anonymous >/dev/null
kubectl config set-context matrix-anonymous --cluster="$cluster" \
  --user=matrix-anonymous >/dev/null
