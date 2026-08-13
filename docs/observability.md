# Observability connectors

korvid can ask a Prometheus and a Loki backend a **fixed set of
resource-scoped questions** about the workload you are looking at. It does
not become an observability client: there is no free-form query surface,
every call is bounded before it leaves the process, and both backends are
read-only.

Configure neither and nothing changes — the two tools are not offered to
the agent or to MCP hosts at all.

## Why

Kubernetes state does not explain every incident. Error rate and latency
move before a pod fails; saturation lives at service level rather than in
one pod; and centralized logs outlive the pod that wrote them, which is
exactly the pod you want to read after a restart.

## Install

```bash
pip install 'korvid[observability]'
```

The connector boundary itself is stdlib. The extra provides the HTTP
client, and it is already present if you installed `korvid[agent]` or
`korvid[all]`.

## Configure

```yaml
observability:
  prometheus:
    url: https://prometheus.internal/prometheus
    token_env: PROM_TOKEN          # or: token_file: /var/run/secrets/prom
    timeout_seconds: 10
    default_window_minutes: 60
    max_window_minutes: 360
    max_series: 50
    max_response_bytes: 1048576
    max_concurrency: 2

  loki:
    url: https://loki.internal
    token_file: /var/run/secrets/loki
    tenant: team-a                 # X-Scope-OrgID, for multi-tenant Loki
    max_lines: 200
    label_mappings:                # how your log shipper labels things
      namespace: namespace
      pod: pod
      workload: app
```

The two backends are independent: configure either, both, or neither.

Every key is optional except `url`. A backend whose `url` is missing or is
not `http(s)://` is **disabled with a startup warning** rather than being
half-configured.

## Tools

### `query_metrics`

Reads one signal for a workload or pod, aggregated over a window.

| signal | unit | metric |
|---|---|---|
| `cpu` | cores | `container_cpu_usage_seconds_total` (rate) |
| `memory` | bytes | `container_memory_working_set_bytes` (max over window) |
| `restarts` | count | `kube_pod_container_status_restarts_total` (increase) |
| `request_rate` | requests/s | `http_requests_total` (rate) |
| `error_rate` | requests/s | `http_requests_total{code=~"5.."}` (rate) |
| `latency_p95` | seconds | `http_request_duration_seconds_bucket` (p95) |

These are the cAdvisor / kube-state-metrics / Prometheus-client naming
conventions. A cluster whose metrics are named differently gets an empty
result, not a wrong one — the result says `no series matched`.

A `workload` matches its pods by **name prefix**, which is the only
relationship visible in cAdvisor labels without a second lookup. A `pod`
matches exactly.

### `search_logs`

Searches centralized logs for a workload or pod over a window. `contains`
is a **plain substring**, not a regular expression and not LogQL. It
becomes a line filter, so it can narrow the result but can never widen the
label scope.

## What is bounded, and what happens at the boundary

| limit | default | behaviour at the limit |
|---|---|---|
| time window | 60 min (max 360) | **refused**, not shortened |
| result series | 50 | truncated, and the result says so |
| log lines | 200 | truncated, and the result says so |
| response bytes | 1 MiB | request aborted |
| request timeout | 10 s | reported as a timeout |
| concurrent requests | 2 per backend | queued |

An over-long window is refused rather than clamped because silently
shrinking it answers a different question from the one that was asked, and
nothing downstream could tell.

Truncation is never silent. Every result carries `truncated: yes|no`, the
window it covers, the endpoint that answered, and the query that ran, so a
claim resting on it can be checked. These results participate in the
agent's evidence citations like any cluster read.

## Credentials

A token is **named** in config, never stored there:

- `token_env` — the name of an environment variable
- `token_file` — a path

Exactly one of the two. Setting both disables the backend rather than
guessing which credential to send. Setting an inline `token:`,
`password:`, `api_key:` or similar also disables it — `config.yaml` is not
a secret store, and the warning does not echo the value.

The token is read **at call time**, used in one `Authorization` header, and
dropped. It never appears in a tool result, an error message, an audit
record or a log line. A rotated token takes effect without restarting
korvid.

If your URL contains userinfo (`https://user:pass@host`), only the host is
ever reported.

## TLS

TLS verification cannot be disabled. There is no `insecure` option, and a
config that sets one disables the backend with a warning pointing at
`network.ca_bundle` — a user who believes they turned verification off and
did not is worse off than one who is told no.

Corporate CAs use the same `network.ca_bundle` setting the agent providers
use; see [air-gapped operation](airgap.md). The connectors get their HTTP
client from the composition root, built with that one trust builder, so
they cannot disagree with the rest of korvid about trust.

## Errors

Failures stay distinguishable, because they need different responses:

| kind | meaning |
|---|---|
| `config` | the URL, the credential source, or the signal name is wrong |
| `auth` | the backend rejected the credential (HTTP 401) |
| `permission` | the credential lacks access to this scope (HTTP 403) |
| `network` | the endpoint is unreachable |
| `timeout` | the backend did not answer in time |
| `limit` | the request or the answer exceeded a configured bound |
| `backend` | the backend refused or malformed the query |

The kind travels with the message: `ERROR: [permission] loki.internal
refused the query …`.

## In the TUI

External reads appear as activity notes rather than screen navigation:
there is no Prometheus view to mirror a query to, and picking some resource
screen would claim the query was about that object.

## What this is not

korvid does not host Prometheus, Loki or an OpenTelemetry collector, does
not write alerts, dashboards or recording rules, and does not accept
free-form queries. Tracing and cloud-provider connectors are not part of
this surface.
