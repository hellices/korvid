# Bounded read-only observability connectors (issue #193)

## Problem

korvid reads Kubernetes resources, events, pod logs and metrics-server data.
Many production failures are invisible from those sources: error rates and
latency move before a pod fails, saturation lives at service level rather than
in one pod, and centralized logs outlive the pod that wrote them.

korvid has no typed, least-privilege contract for asking an external
observability backend about the resource the user is looking at.

## Shape of the answer

A new leaf package, `korvid.obs`, defines a connector boundary and ships two
implementations: Prometheus and Loki. Two new agent/MCP tools, `query_metrics`
and `search_logs`, dispatch to it through a new registry effect,
`external_read`.

The boundary is deliberately narrow. korvid does not become an observability
client: it asks a fixed catalogue of resource-scoped questions and returns a
bounded, cited answer.

### Layering

```
korvid.obs   -> korvid.core          (config, redaction)
korvid.tools -> korvid.obs           (dispatch)
```

`korvid.obs` imports no Textual and no Kubernetes client. The ABC and result
types are stdlib-only; the two HTTP implementations import `httpx` and ship in
a new `observability` extra, so a base install is unaffected.

### Why a fixed catalogue, not free-form PromQL/LogQL

An LLM writing arbitrary PromQL against a shared Prometheus can produce a query
that costs the backend more than the incident does. Worse, a query language is
a place to smuggle intent: `{namespace="x"} or {namespace="kube-system"}` is
still "one query". The catalogue makes the blast radius of a tool call a
property of korvid's code rather than of the model's output.

Each catalogue entry names a signal (`cpu`, `memory`, `restarts`,
`request_rate`, `error_rate`, `latency_p95`) and renders a template whose only
variable parts are label *values*, each escaped for the target query language.

For logs, the model may pass a plain substring filter. It is escaped and
embedded as a LogQL line filter, never as a selector — a substring cannot widen
the label scope.

### Limits

Every query passes the same gate before a request leaves the process:

| limit | default | enforced |
|---|---|---|
| time window | 60 min, max 360 | rejected above the configured max |
| result series | 50 | truncated, and the truncation is reported |
| log lines | 200 | truncated, and the truncation is reported |
| response bytes | 1 MiB | streamed read aborts past the cap |
| request timeout | 10 s | httpx timeout |
| concurrency | 2 per connector | `asyncio.Semaphore` |

Truncation is never silent: the rendered result carries a `truncated:` line, so
a model cannot mistake a capped answer for a complete one.

### Credentials and TLS

Auth is configured as a *source*, never a value: `token_env` names an
environment variable, `token_file` names a file. The token is read at call time
and lives only in the request headers. It is never rendered into a result,
never included in an error message, and never written to the audit log.

TLS verification cannot be disabled. There is no `insecure` option; a config
that tries to set one fails loudly rather than being ignored, because a user who
believes they disabled verification and did not is worse off than one who is
told no. Corporate CAs use the existing `network.ca_bundle` setting.

### Errors

Config, auth, permission, network, timeout and limit failures stay
distinguishable. Each maps to a single actionable sentence naming the endpoint
host (never the credential) and what to change.

### Evidence

`external_read` results participate in the agent's evidence ledger exactly like
cluster reads: they carry source, endpoint host, query scope, time window and
truncation status, so a claim citing them can be checked. On the MCP surface
they are surfaced as an activity note (there is no screen to mirror them to),
and their failure verdict trusts the connector's own error bit rather than the
`ERROR:` prefix, because a log line may legitimately start with `ERROR:`.

## Out of scope

Hosting any of these backends; writing alerts, dashboards or recording rules;
unbounded queries; tracing and cloud-provider connectors.
