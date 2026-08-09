#!/usr/bin/env bash
# Wait until the AKS cluster has no operation in flight.
#
# Azure flips `powerState.code` as soon as it accepts a start/stop request,
# while `provisioningState` stays Starting/Stopping/Updating until the
# operation actually completes. Any other operation issued in that window is
# rejected with "there's an in-progress ... operation", so both the start and
# the stop paths have to wait for the real thing.
#
# Usage: wait-aks-settled.sh <resource-group> <cluster> [expected-power-state]
#
# Exits 0 once provisioningState is Succeeded (and, when given, powerState
# matches). Exits 1 if that has not happened within WAIT_AKS_TIMEOUT seconds
# (default 600), printing the last observed pair.
#
# Query failures are retried through the whole budget by default, because the
# caller after a stop is a barrier: giving up early would release the
# workflow's concurrency group while the stop is still in flight, which is the
# collision this exists to prevent. Set WAIT_AKS_MAX_QUERY_FAILURES to abort
# after N consecutive failures instead - only appropriate for an advisory
# pre-flight check whose failure does not skip anything.
set -uo pipefail

group=${1:?resource group required}
cluster=${2:?cluster name required}
want_power=${3:-}
max_fails=${WAIT_AKS_MAX_QUERY_FAILURES:-0}

deadline=$(( SECONDS + ${WAIT_AKS_TIMEOUT:-600} ))
power=""
prov=""
fails=0
err_file=$(mktemp)
trap 'rm -f "$err_file"' EXIT

while :; do
  if out=$(az aks show -g "$group" -n "$cluster" \
      --query '[powerState.code,provisioningState]' -o tsv 2>"$err_file"); then
    fails=0
    read -r power prov < <(printf '%s' "$out" | tr '\n' ' ') || true
  else
    # An expired login, a wrong subscription or a deleted cluster is
    # permanent, so every failure is logged rather than hidden. Whether to
    # give up early is the caller's call: a barrier must keep waiting, since
    # returning early would release the concurrency group with an operation
    # still running.
    fails=$(( fails + 1 ))
    power=""
    prov=""
    echo "az aks show failed: $(tr '\n' ' ' <"$err_file")"
    if [ "$max_fails" -gt 0 ] && [ "$fails" -ge "$max_fails" ]; then
      echo "::error title=Cluster query failed::az aks show failed $fails times in a row for $cluster: $(tr '\n' ' ' <"$err_file")"
      exit 1
    fi
  fi
  if [ "${prov:-}" = "Succeeded" ] && { [ -z "$want_power" ] || [ "${power:-}" = "$want_power" ]; }; then
    exit 0
  fi
  # Poll once more *at* the deadline rather than sleeping past it and failing
  # on a stale read: an operation that settles during the final sleep still
  # counts as settled within the advertised budget.
  [ "$SECONDS" -lt "$deadline" ] || break
  echo "waiting for $cluster to settle (${power:-unknown}/${prov:-unknown})"
  sleep 20
done

echo "::error title=Cluster busy::$cluster is ${power:-unknown}/${prov:-unknown} after ${WAIT_AKS_TIMEOUT:-600}s; another operation has not finished."
exit 1
