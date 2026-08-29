# MCP server

Requires the `[mcp]` extra (see the README's
[installation section](https://github.com/hellices/korvid/blob/main/README.md#installation)).
Start with `korvid --mcp` (or set `mcp: {enabled: true}` in
`~/.config/korvid/config.yaml`) to expose the read and UI-drive tools to
external agents — VS Code Copilot Chat, Claude Code, Cursor, Zed — over a
[Streamable HTTP MCP](https://modelcontextprotocol.io) server bound to
`127.0.0.1:7878` (`mcp: {port: N}` to change). Write tools are **not**
exposed: cluster mutations stay behind the in-TUI confirmation dialog. An
opt-in *proposal* flow lets external agents queue writes for your review
(see [Propose a write](#propose-a-write)) — even then, a proposal never
executes without a fresh user keystroke in the TUI.

## Evidence crosses a tool boundary

**MCP tool calls never go through korvid's embedded-provider boundary.** No
korvid-managed model call is involved, so `OutboundPolicy` — the sanitized
payload, the request caps, the text credential-pattern masking the embedded
[agent](agent.md#inspecting-what-the-agent-sends) applies — does not run on this
surface at all. Disclosure is **tool-specific**, and what an MCP client
receives is **not necessarily the same snapshot** a Direct keybinding or the
embedded Agent would show for the same object:

- **Structured manifests** (`get_resource`) are recursively redacted where
  they are produced, by the same `korvid.core.redaction` primitive the
  embedded agent's boundary uses: `Secret` data, the
  `kubectl.kubernetes.io/last-applied-configuration` annotation, and
  credential-named keys/values are masked **structurally** — by where they
  sit in the parsed document, not by how they look — before the document is
  bounded and returned. An MCP client sees the same redacted document the
  model would.
- **Compound workload diagnoses** (`diagnose_workload`) are a shaped text
  report rather than a parsed document, so the structural pass above never
  runs over them. What they do get is producer-side **credential-pattern
  text redaction**: korvid masks credential-shaped text in every shaped
  section of the report, and in each embedded pod diagnosis, *before* the
  report is compacted to its size cap — a cut taken first would split a
  credential across the boundary and leave the tail unclassifiable. Pattern
  matching is weaker than the structural guarantee above: a secret no
  pattern recognises is not masked here.
- **Service and PVC binding diagnoses** (`diagnose_service`, `diagnose_pvc`)
  are deterministic structured YAML: EndpointSlice or secondary-read RBAC
  denials become explicit `gaps[]` entries rather than an error, so the
  caller can reason about incomplete evidence instead of a hard failure.
- **Logs, events, lists, single-pod diagnoses, and helm status** get only
  their own tool-specific shaping (scoping, formatting, size caps). They are
  **not** credential-pattern masked — a token printed into a pod's log
  reaches the client verbatim.

The external client (and whatever model or data policy it applies) owns its
own AI data boundary past this producer-side redaction; korvid's guarantee
stops at what is exposed, how each tool shapes it, and that writes still
require your keystroke — not what the connected client does with the data.
See [the threat model](threat-model.md) for the full boundary.

```mermaid
flowchart LR
    CLIENT["External MCP client"]
    READ["Bounded Kubernetes read tools"]
    OBS_READ["Bounded observability tools<br/>activity note only"]
    FOLLOW["Optional follow<br/>UI navigation only"]
    PROPOSE["Opt-in write proposal"]
    CONFIRM["In-TUI human confirmation"]
    AUDIT["Fail-closed audit"]
    KUBE[("Kubernetes API")]
    OBS[("Prometheus / Loki")]

    CLIENT --> READ --> KUBE
    CLIENT --> OBS_READ --> OBS
    READ -. successful read .-> FOLLOW
    CLIENT -. disabled by default .-> PROPOSE --> CONFIRM --> AUDIT --> KUBE

    style CONFIRM fill:#f5a623,color:#12151a,stroke:#ffcf6e
    style AUDIT fill:#1b1f26,color:#e7e9ee,stroke:#f5a623
```

Follow never changes a tool result, and a proposal never becomes a mutation
until the TUI receives a fresh user keystroke and the audit append succeeds.
Prometheus and Loki queries never reach the Kubernetes API through this
surface — they route to their own backends and never touch `KUBE` above.

The live endpoint is also published to
`$XDG_STATE_HOME/korvid/mcp-endpoint.json` (defaulting to
`~/.local/state/korvid/mcp-endpoint.json` when `XDG_STATE_HOME` is unset)
while korvid runs — a registry keyed by process id, so multiple korvid
instances can be discovered side by side:

```json
{"servers": {"12345": {"url": "http://127.0.0.1:7878/mcp", "port": 7878, "pid": 12345}}}
```

## Connect a client

| Host | Configuration |
|---|---|
| VS Code | `.vscode/mcp.json`: `{"servers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}` |
| Claude Code | `claude mcp add --transport http korvid http://127.0.0.1:7878/mcp` |
| Cursor | `.cursor/mcp.json`: `{"mcpServers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}` |
| Zed | `settings.json`: `{"context_servers": {"korvid": {"url": "http://127.0.0.1:7878/mcp"}}}` |

Without configuration, korvid still runs the server once `mcp.enabled: true`
is set — no client is required for it to be listening; point any MCP-capable
host at the published endpoint above.

## Read once or follow activity

External hosts overwhelmingly call the *read* tools, which by themselves
move nothing on screen. **Follow mode** mirrors those reads in the TUI so you
can watch the assistant work — off by default, since screen hijacking
mid-task is worse than invisibility. Enable at startup with
`mcp: {follow: true}`, or live with `:mcp follow on|off` (bare `:mcp follow`
toggles; bare `:mcp` reports both server and follow state).

With follow on, a successful **Kubernetes** read navigates the TUI:
`list_resources` selects that view, `get_resource` / `get_events` open the
describe pane, `get_logs` opens the log pane, and the `diagnose_*` tools open
the relevant describe pane too. The mirror is fire-and-forget UI navigation,
not a resumable feed; if it cannot land, korvid falls back to an activity
note.

With follow off, Kubernetes reads surface only as activity notes. Prometheus
and Loki reads always use an activity note instead of navigating anywhere
because no korvid screen can display them.
An activity note does not make the read followable; it only makes the external
read visible.

Mirroring never waits on, or fails with, the MCP response; a failed read is
never mirrored, and a mirror never opens over an approval dialog — approvals
are confirmed only by your keystrokes. With follow **off**, each external
read still surfaces as a transient toast (`client-name: get_logs api-1 (ns
prod)`), so nothing reads your cluster invisibly.

The landing clip of this flow is recorded from this repository alone: a real
MCP SDK client making read-only requests over Streamable HTTP to a loopback
korvid serving a synthetic cluster, with follow mirroring each answer.

## Propose a write

Off by default. With

```yaml
mcp:
  enabled: true
  write_proposals: true
```

the MCP surface adds three **opt-in** tools — `propose_write`,
`get_write_proposal`, `cancel_write_proposal` — that let an external agent
*propose* a cluster write (delete, scale, rollout restart, in-place pod
resize). A proposal never mutates anything by itself:

- `propose_write` runs the same validation as the built-in agent write path
  (kind resolution, read-only mode, RBAC pre-check, target-UID capture,
  dry-run preview) and queues an **immutable** proposal; the caller polls
  `get_write_proposal` for the terminal outcome.
- The TUI shows a persistent `⚑` status-bar indicator naming the caller and
  target. Nothing auto-opens and focus is never stolen.
- `:proposals` reviews pending proposals one at a time in the same
  confirmation dialog as every other write — including the typed-name and
  typed-context gates. Only your keystroke — the same fresh user keystroke
  every write path requires — can approve; MCP tools cannot focus, type, or
  confirm.
- Before executing, korvid rechecks the kube context epoch, RBAC, and the
  captured target UID, then writes through the same fail-closed
  audit-before-mutation path with `source=external_mcp` and the caller
  metadata recorded.
- Deny, dismiss, TTL expiry (10 minutes), and caller cancel are distinct
  outcomes. A context switch, an MCP server restart, or TUI shutdown
  invalidates pending proposals.

Local callers are untrusted: read access alone does not grant proposal
access. Each server run generates a high-entropy capability token that
callers must echo as the `capability` argument on every proposal call, and
publishes it only in the endpoint registry file. korvid creates that file
with an atomic POSIX owner-only (`0600`) open, never a chmod after the
fact. On Windows that mode argument does not map onto NTFS ACLs, so the
token's confidentiality there rests on the enclosing directory's inherited
permissions rather than on the mode korvid requested — the same platform
limit [`docs/threat-model.md`](threat-model.md) records for private
exports. MCP `clientInfo` is caller-supplied metadata, never treated as
authenticated identity.

## Representative tools

Not a full schema inventory — a compact map of what each family does and how
it is disclosed:

| Family | Examples | Backend | Disclosure / follow |
|---|---|---|---|
| Recursively redacted manifests | `get_resource` | Kubernetes API | Producer-side recursive **document** redaction (`Secret` data, last-applied annotation, credential-named keys); may emit a follow activity note |
| Pattern-masked compound diagnoses | `diagnose_workload` | Kubernetes API | Producer-side **credential-pattern text** redaction of each shaped section, applied before compaction — not a recursive document pass; may emit a follow activity note |
| Shaped Kubernetes reads | `list_resources`, `get_logs`, `get_events`, `diagnose_pod`/`_service`/`_pvc`, `helm_list_releases` | Kubernetes API | Tool-specific shaping and size caps only — **not** credential-pattern masked; may emit a follow activity note |
| Observability reads | Prometheus / Loki query tools | Prometheus / Loki | Activity note only — never followable navigation |
| Write proposals (opt-in) | `propose_write`, `get_write_proposal`, `cancel_write_proposal` | Kubernetes API, gated | Inert until a fresh user keystroke in the TUI |
