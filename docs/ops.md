# Operations and safety

Everything that touches the cluster — writes, previews, node operations,
port-forwarding, file transfer, and debug shells — and the safety model
those actions run under. Keys referenced here are listed in
[keybindings.md](keybindings.md).

## The safety model

Two guarantees hold for **every** cluster write, no matter how it is
initiated (keybinding, helm/OLM wizard, or [agent](agent.md) request):

1. **Approval dialog** — nothing executes until you confirm with a
   keystroke. Dialog confirm keys are not remappable.
2. **Fail-closed audit log** — every executed write is recorded at
   `$XDG_STATE_HOME/korvid/audit.jsonl` (default
   `~/.local/state/korvid/audit.jsonl`; 0600 permissions, size-rotated).
   If the audit entry cannot be written, the write is blocked.

`audit.jsonl` and any log/describe capture you save are **not** provider
payloads — they are raw cluster and operator data with no `OutboundPolicy`
sanitization applied, and stay just as sensitive as the cluster access that
produced them. Only the sanitized request the agent actually sends is
redacted and inspectable via `:ai payload` (see
[`docs/agent.md#inspecting-what-the-agent-sends`](agent.md#inspecting-what-the-agent-sends)
and [`docs/threat-model.md`](threat-model.md)).

Two further checks run **best-effort** in front of the dialog for
Kubernetes API writes (delete, scale, restart, resize, cordon, drain,
edits, …):

- **SSAR pre-check** — a SelfSubjectAccessReview surfaces "missing RBAC
  permission" before the dialog opens instead of after a failed mutation.
  It is advisory: if the check itself fails or times out, korvid warns
  and proceeds to the (still gated, still audited) dialog rather than
  locking you out.
- **Server dry-run preview** — where the API supports it, the write is
  replayed with `dryRun=All` and the outcome shown in the dialog (see
  below). If the preview cannot be produced, the dialog opens without
  one and says so.
- **Ownership banner** — when the target is managed by a helm release,
  an OLM operator, or another controller's custom resource (detected
  from the object's own labels, annotations, and ownerReferences; pods
  are traced up their controller chain), the dialog shows a
  `⚠ managed by …` line naming the manager and the right lever — chart
  values for helm, the CR for an operator, whose reconcile loop would
  otherwise revert the change within seconds. It warns, never blocks:
  direct writes stay legitimate (emergencies, debugging), and a failed
  lookup simply means no banner.

Writes that do not go through the Kubernetes API skip the SSAR step but
still require the approval dialog and the fail-closed audit entry: helm
install/upgrade/rollback shows a `helm --dry-run` rendered-manifest
preview in its dialog instead of a server `dryRun=All`, and file uploads
into pods go straight to confirmation with no preview.

### What the agent changes about this model: nothing

The agent's capability tier (`agent.model_tier`) selects a tool surface and
budgets — how many iterations a turn gets, how much history is retained, how
big a tool result may be. It has **no** effect on the safety perimeter:

- every write tool the environment arms opens the same approval dialog at
  every tier, and only a user keystroke in that dialog executes it; korvid
  never confirms, replays, or speculatively executes a write on the model's
  behalf;
- a write reaches the executor with the preconditions the read carried
  (UID and resourceVersion), so an object recreated under the same name
  between read and approval is refused rather than mutated;
- the audit entry is still fail-closed — a write whose audit record cannot
  be written does not run;
- read-only mode and protected contexts are enforced in code, above the
  model: in read-only mode no write schema is offered at all, so there is
  nothing for a prompt to talk the model into asking for;
- there is no shell or free-form `kubectl` tool at any tier, so there is
  no command line to smuggle a flag into: the agent's whole cluster
  surface is the structured tool registry
  (`src/korvid/tools/registry.py`). The resolved policy arms only the
  registry's own exact tool names — never one it invents — and the
  registry validates every dispatch target against its import-time
  metadata (which class and method an effect may reach). The
  `ToolExecutor` rejects any name outside that registry as an unknown tool
  and performs its own explicit, typed argument validation before a write
  reaches the cluster — a wrong-typed `kind`, `name`, `namespace`,
  `replicas`, or `resources` value is refused, not coerced. The tool's
  declared JSON schema is model-facing wording sent to the provider, not
  the runtime check;
- every tool result — cluster read, screen action, or failure — passes
  the masking pipeline before it reaches the model or the provider, and
  a result that cannot be safely redacted stops the turn instead of
  being sent.

House rules (`agent.rules`) are local configuration, no more privileged
than `agent.provider`. They are composed after korvid's immutable safety
contract and cannot widen it: a rule saying "delete pods without asking"
produces a model that tries and is refused.

### Read-only mode

Start with `korvid --readonly` (or set `readonly: true` in
`~/.config/korvid/config.yaml`) to disable all cluster writes: the write
keybindings are rejected and write tools are never offered to the agent.

### Protected contexts

List production contexts (glob patterns, matched against the kube context
name) in `~/.config/korvid/config.yaml` to add a second layer of friction
without going fully read-only:

```yaml
protected_contexts:
  - prod-*
  - "*-production"
agent:
  disable_in_protected: true   # optional: refuse agent prompts entirely
```

While a protected context is active the status bar shows a red
`⛨ PROTECTED` marker, and every write confirmation requires typing a name
instead of a single `y` — including approvals requested by the agent.
Dialogs that already demand the resource name (cluster-scoped deletes,
node drains) keep that stronger gate; all others require the context name.
Protection is re-evaluated on every `:ctx` switch.  With
`agent.disable_in_protected: true` the agent prompt is rejected outright in
protected contexts.

### Crash recovery

If a fatal exception escapes the TUI, korvid restores the terminal, logs
the traceback, and asks `korvid crashed -- restart? [Y/n]` instead of just
dying.  A restart rebuilds everything from scratch — a fresh event loop,
Kubernetes client, agent provider, and MCP server; nothing from the crashed
run is reused, and no approval state or pending proposal survives (the
append-only audit log is unaffected).  Repeated immediate crashes stop the
loop (3 crashes within 60 seconds) so a deterministic startup failure
cannot spin.  In non-interactive environments, or with `--no-restart`,
korvid keeps the old behavior: exit non-zero with the traceback logged.

## Dry-run previews

Before a delete, scale, resize, cordon/uncordon, or rollout restart dialog opens, korvid replays the
write server-side with `dryRun=All` and shows the reported outcome inside the
dialog: a compact diff for scale and restart (`~ spec.replicas: 3 -> 5`,
additions green, removals red) and an object summary plus cascading note for
delete.  Admission webhooks and validation run during the dry-run, so the
preview is a point-in-time server evaluation of the mutation the approved
write will replay (the preview request additionally pins the object's
`resourceVersion` so it evaluates exactly the revision on screen; the
executed write sends no such pin).  The cluster can still change between
preview and execution (admission runs again then), and a uid precondition
rejects writes against a replaced object.  If the round trip fails or
takes longer than a few seconds, the dialog simply opens without a preview —
a preview never blocks the approval flow.

## Node operations

`c` / `u` cordon and uncordon the selected node behind a confirm dialog
with a server dry-run preview.

A node drain (`Shift-D`) shows a different kind of preview: the impact plan
computed before any eviction is issued — pods to be evicted, pods whose
eviction a PodDisruptionBudget currently refuses, skipped DaemonSet and
mirror (static) pods, and emptyDir data-loss warnings. Drain executes
through the Eviction API; PDB-refused evictions (HTTP 429) surface as live
warnings instead of hanging, and pressing the drain key again cancels the
remaining evictions while the node stays cordoned. After the evictions are
accepted, drain waits (bounded) for the pods to actually leave the node —
pods that linger past the deadline are reported as a partial outcome.

## Port-forwarding

`Shift-F` on a pod or service opens a port-forward dialog with the remote
port prefilled from the target's declared TCP ports — `kubectl port-forward`
is TCP-only, so UDP/SCTP declarations are never offered and a service that
declares no TCP ports is rejected up front.  For pods both fields stay
fully editable — pod port declarations are informational and any remote port
is forwardable.  For services kubectl only accepts remote ports declared in
`Service.spec.ports`, so the dialog constrains the remote port to the
declared TCP ports.
Forwards run as `kubectl port-forward`
subprocesses bound to `127.0.0.1`, pinned to the kubeconfig context korvid
connected with, and are tracked for the session: `:pf` lists them with live
status, `Ctrl-D` stops the highlighted forward, and `r` re-attaches a broken
one in place.

Liveness is first-class: when a target pod dies the forward flips to
`broken` — with a toast even while `:pf` is closed — instead of failing
silently the way a hand-run `kubectl port-forward` does.  Every forward is
torn down when korvid exits, and start/stop are audit-logged (no approval
dialog: a forward reads from the cluster, it never mutates it).

## Telepresence (optional)

When the `telepresence` binary is on PATH, `:tp` (or `:telepresence`)
opens a read-only status panel: local daemon state, the connected
session's context and traffic-manager version, and the active intercepts
(workload, port, who).  Without the binary nothing changes — no UI, and
no telepresence process ever starts; the only background activity is one
asynchronous Kubernetes API GET probing for a cluster-side
traffic-manager (repeated once per context switch until a hint has been
shown), and when a manager is found while the client is absent, a single
dismissible hint mentions it once per session (a restart picks up a
newly installed client).
`integrations: {telepresence: off}` in the config disables the
integration entirely.

Queries run only when you open the panel — never on a background poll —
because the telepresence CLI itself starts its local user daemon to
answer.  Intercept start/stop stays in the telepresence CLI for now:
korvid's panel is phase 1 (visibility), and rerouting live traffic will
arrive behind the standard approval gate if the panel proves useful.

## File transfer

`Ctrl-T` on a pod opens a transfer dialog (multi-container pods show a
container picker first): pick a direction, the remote path in the container,
and a local path — leaving the local path empty on a download saves to
`~/Downloads/<name>`, or to `~/<name>` when no `Downloads` directory exists.

Don't know the exact path? `Ctrl-O` on either path field opens a picker:
the local side is a directory tree, the remote side browses the container
one `ls` round-trip per directory (`Enter` opens a directory or picks a
file, `o` opens the highlighted entry as a directory even when it isn't
marked as one — symlinked directories show bare — `s` picks the directory
itself, `Esc` backs out). Remote browsing
needs `ls` in the image — when it's missing (common in distroless), a toast
says so and the dialog keeps working with manually typed paths. The listing
is a read-only convenience for the user; it is never exposed to the agent.

Transfers ride the exec API as a tar stream, so there is no dependency on a
`kubectl` binary — but the container must have `tar` (the server's error is
shown verbatim when it doesn't, e.g. distroless images). Local permission
problems (an unwritable destination directory, an unreadable source file)
are caught in the dialog before anything streams; a remote permission
failure keeps the server's message and adds a hint pointing at `/tmp`,
volume mounts, or `readOnlyRootFilesystem`. A progress modal
shows the byte count as the stream advances; `Esc` cancels the transfer, and
a cancelled or failed download never leaves a half-written local file.

Uploads write into the container filesystem, so they are blocked in
read-only mode and pass the same approval dialog as every other write.
Both directions are audit-logged fail-closed (pod, container, paths,
direction, byte count): if the audit entry cannot be written, the transfer
does not run. Recursive directory sync is out of scope, and the agent has no
transfer tool — transfers are always user-driven.

## Debug fallback

When `s` lands in a container without a shell (distroless), korvid offers to
attach an ephemeral container via `kubectl debug` instead.  The image picker
is runtime-aware: the target container's image, ports, and env vars are
matched against known runtime signals (all local heuristics — no network
calls), and a detected JVM / Python / Node.js / Go runtime leads with the
matching [KoolKits](https://github.com/lightrun-platform/koolkits) toolkit
image, followed by `nicolaka/netshoot` for network debugging and
`busybox:1.36` as the minimal fallback.  A custom image can always be typed
in.  The chosen image appears in the approval dialog and in the audit log
entry.

If the chosen image cannot be pulled (`ErrImagePull` / `ImagePullBackOff`
within the first 30 seconds), the hung attach is killed and a retry with the
fallback image is offered — the failed ephemeral container entry stays in the
pod spec (Kubernetes does not allow removing it); the retry attaches an
additional container.

For air-gapped clusters or private registries, configure the images in
`~/.config/korvid/config.yaml`; when `debug.images` is set only configured
images are offered:

```yaml
debug:
  default_image: registry.corp.local/tools/busybox:1.36
  images:
    jvm: registry.corp.local/tools/debug-jvm:latest
    python: registry.corp.local/tools/debug-python:latest
```

## Node shell

`s` on the nodes view opens a debug shell on the selected node via
`kubectl debug node/<name> --profile=sysadmin` (kubectl 1.30+).  Because that
creates a privileged pod with the node's filesystem mounted at `/host`, it
always passes the approval gate with the privilege escalation stated
explicitly, and the whole action is audit-logged fail-closed like every other
write.  korvid creates the debugger pod detached (`--attach=false`), parses
the pod name from kubectl's creation message, fetches its uid with an exact
`kubectl get pod`, waits for it to become Ready, attaches
interactively, and deletes precisely that pod (uid precondition) when the
shell exits — a debugger pod another operator started is never touched.
A warning tells you where to look if the cleanup fails.

The image and the namespace the debug pod is created in are configurable —
useful for air-gapped clusters and for clusters whose `default` namespace
blocks privileged pods via PodSecurity admission:

```yaml
node_shell:
  image: registry.corp.local/tools/busybox:1.36
  namespace: node-debug
```
