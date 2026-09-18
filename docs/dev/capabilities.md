# Living capability ledger

This internal ledger records current product truth. It is a living capability
inventory, not an issue backlog, release promise, or public roadmap. Update a
row when shipped behavior or its evidence changes; use issues and plans for
proposed work.

## Status vocabulary

| Status | Meaning |
|---|---|
| `implemented` | Shipped behavior with maintained user documentation. |
| `partial` | Shipped with a deliberately bounded surface. |
| `validated` | Implemented and qualified by explicit cross-platform, integration, or evaluation evidence. |
| `deferred` | Plausible future work that is not committed to a release. |
| `rejected` | A direction intentionally excluded from korvid's product model. |

## Current capabilities

| Capability | Status | Current scope and evidence |
|---|---|---|
| Keyboard-first TUI | `implemented` | The resource workspace, contextual keys, logs, relationships, and guarded operations ship as the primary interface; see the [TUI guide](../tui.md) and [keybindings](../keybindings.md). |
| Pulse | `partial` | Bounded current Pod and Deployment findings plus recent Warning Events and explicit coverage gaps; it is neither a health verdict nor a fleet view. See [Pulse / Problems](../pulse.md). |
| Action Palette | `implemented` | `Ctrl-P` searches applicable shipped app actions and built-in commands, including disabled reasons; it is not an arbitrary command launcher. See [keybindings](../keybindings.md#move-and-inspect). |
| Approval and audit safety | `validated` | Direct, Agent, and proposed MCP writes converge on fresh in-TUI approval and fail-closed audit-before-mutation; the production path is exercised by deterministic operation journeys. See the [current architecture](specs/2026-08-12-korvid-architecture.md) and [evaluation method](../evals/operations.md). |
| Embedded Agent | `implemented` | The optional in-TUI Agent uses a configured local or remote provider for bounded reads, cited evidence, workspace control, and approval-gated write requests; it is not an autonomous or headless operator. See the [Agent guide](../agent.md). |
| Local MCP adapter | `implemented` | The optional stdio adapter reaches the authenticated loopback endpoint of an already-running TUI for bounded reads, UI drive, and opt-in write proposals; remote and headless backends are outside this surface. See the [MCP guide](../mcp.md). |
| Observability connectors | `implemented` | Optional bounded, read-only Prometheus and Loki tools ship for Agent/MCP use, not as panels. The surface is implemented; validation remains contract-focused, not a compatibility claim for every backend deployment. See [observability connectors](../observability.md). |
| Evaluation harness | `implemented` | Offline task, conversation, and write-lifecycle harnesses are implemented, with a live-AKS tier. Validation evidence remains bounded: published model rows are historical/pre-tier and live calibration is incomplete, so it does not claim broad live-model qualification. See the [methodology](../evals/methodology.md) and [scoreboard](../evals/scoreboard.md). |
| External extension contracts | `implemented` | `korvid.provider` and `korvid.credential` are the only public external extension contracts. `korvid.panel` is unimplemented and is not a public contract; `korvid.tool` is unimplemented and is not a public contract. See [provider extensions](../provider-plugins.md). |
| Fleet / simultaneous multi-cluster | `deferred` | Context switching exists, but a fleet or simultaneous multi-cluster view is not shipped and future work is not committed to a release. See the current [product overview](../overview.md). |
| Incident persistence and replay | `deferred` | No durable incident investigation or replay surface is shipped; considering one later is not committed to a release. Current evidence behavior is described in the [Agent guide](../agent.md#from-prompt-to-cited-answer). |
| Autonomous writes / approval bypass | `rejected` | No model or external client may execute a write autonomously or bypass fresh human approval and fail-closed audit. This is a structural product boundary, not deferred automation; see the [write-path architecture](specs/2026-08-12-korvid-architecture.md#4-the-write-path-why-a-model-cannot-mutate-your-cluster). |
