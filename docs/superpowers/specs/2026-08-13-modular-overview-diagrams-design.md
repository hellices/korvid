# Modular overview diagram redesign

## Goal

Make [`docs/overview.md`](../../../overview.md) read as a durable product
concept, not as a runtime flow or a stack of implementation layers.

The reader should understand in one glance:

1. the base korvid install is a complete Kubernetes cockpit;
2. Agent and MCP are independent modules plugged into stable korvid
   capabilities;
3. either module, both modules, or neither module is a valid product shape;
4. approval and audit are shared platform properties rather than another
   optional feature.

## Chosen visual metaphor: ports and adapters

Use a stable **korvid capability boundary** as the center of the first
diagram. Four adapters connect external actors to it:

- **TUI adapter** — included; connects a human operator;
- **Kubernetes adapter** — included; connects a kubeconfig and cluster;
- **Agent adapter** — optional `korvid[agent]`; connects a model/provider;
- **MCP adapter** — optional `korvid[mcp]`; connects an editor or external
  assistant.

External systems sit outside the product boundary and connect to the card that
adapts them:

- human → Cockpit;
- model/provider → Agent;
- editor/external assistant → MCP;
- kubeconfig/cluster → Kubernetes adapter.

The central boundary contains stable product capabilities shared by every
adapter:

- cluster observation and diagnostics;
- navigation and UI control;
- approval-gated operations;
- fail-closed audit.

This is not an internal Python module diagram. "Core" means the durable product
contract, not `src/korvid/core/`.

## Second diagram: valid compositions

Replace the write-flow diagram with four compact product assemblies:

1. **Cockpit**;
2. **Cockpit + Agent**;
3. **Cockpit + MCP**;
4. **Cockpit + Agent + MCP**.

Each assembly uses the same boundary and adapter shapes as the first diagram.
The included TUI and Kubernetes adapters are always present. Agent and MCP
occupy independent optional ports. This turns the optional-extras statement
into a visual truth table and prevents the old ladder reading where MCP
appeared to imply Agent.

Entra is a small provider-auth badge attached to Agent, not a fifth product
shape: it changes authentication, not korvid's role.

## Shared safety foundation

Approval and fail-closed audit sit *inside* the stable capability boundary.
They are properties every adapter inherits, not another adapter or an optional
foundation users can remove.

The document retains the safety explanation in prose. It does not need a
second flowchart: this overview is about product modularity, while detailed
write ordering belongs in the implementation architecture document.

## Language changes

Replace "Layer 1/2/3" headings with:

- **Base — the cockpit**
- **Agent module — an agent inside the cockpit**
- **MCP module — korvid as a tool for other agents**

Avoid "layer", "stack", or wording that implies either extra depends on the
other.

## Visual rules

- Stable capability boundary: dark neutral.
- Included adapters: green.
- Agent adapter: purple; MCP adapter: blue.
- External actors and systems: dashed borders.
- Included adapters: solid border; optional adapters: dashed outer edge or `OPTIONAL`
  badge.
- Labels are roles and capabilities, not implementation names.
- The diagrams must render in GitHub Mermaid and remain readable at README
  width without horizontal scrolling.

## Verification

- Render every Mermaid block with `mermaid-cli`.
- Inspect the resulting images, not only parser success.
- Confirm the four assemblies exactly match the optional dependencies in
  `pyproject.toml`.
- Confirm no diagram implies Agent → MCP or MCP → Agent.
- Run pre-commit and the documentation/README contract tests.
