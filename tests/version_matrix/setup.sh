#!/usr/bin/env bash
# Install the version-matrix fixtures into a disposable cluster and add one
# kubeconfig context per principal the scenarios read as (#409, slice S5).
#
# Usage: setup.sh ADMIN_CONTEXT
#
# Only ever run against a cluster created for the run: it installs CRDs, an
# APIService and RBAC, and writes contexts into the current kubeconfig. It
# refuses a context unless it names a kind cluster that exists and points at
# that cluster's API server, since a context's name alone proves nothing.
set -euo pipefail

refuse() {
  echo "setup.sh: refusing $admin: $1" >&2
  exit 2
}

admin=${1:?usage: setup.sh ADMIN_CONTEXT}
case "$admin" in
  kind-*) name=${admin#kind-} ;;
  *) refuse "it only seeds a kind cluster (kind-NAME)" ;;
esac
kind get clusters 2>/dev/null | grep -qxF "$name" || refuse "no kind cluster named $name"

here=$(cd "$(dirname "$0")" && pwd)
kube=(kubectl --context "$admin")
cluster=$("${kube[@]}" config view -o jsonpath="{.contexts[?(@.name=='$admin')].context.cluster}")
server=$(kubectl config view -o jsonpath="{.clusters[?(@.name=='$cluster')].cluster.server}")
own=$(mktemp)
trap 'rm -f "$own"' EXIT
kind get kubeconfig --name "$name" >"$own"
expected=$(kubectl config view --kubeconfig "$own" -o jsonpath='{.clusters[0].cluster.server}')
[ -n "$server" ] && [ "$server" = "$expected" ] ||
  refuse "its server ${server:-(none)} is not kind cluster $name's ($expected)"

"${kube[@]}" apply -f "$here/fixtures.yaml"
"${kube[@]}" wait --for=condition=Established --timeout=120s \
  crd/widgets.matrix.korvid.dev crd/gadgets.future.korvid.dev

"${kube[@]}" apply -f "$here/apiservice.yaml"
"${kube[@]}" wait --for=condition=Available=false --timeout=120s \
  apiservice/v1.unavailable.korvid.dev

for account in events-core events-group; do
  token=$("${kube[@]}" -n korvid-matrix create token "$account" --duration=1h)
  # set-credentials merges into an existing user; start from an empty one.
  kubectl config delete-user "matrix-$account" >/dev/null 2>&1 || true
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
