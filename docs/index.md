---
template: home.html
title: korvid
hide:
  - navigation
  - toc
---

<section class="hero">
  <div class="hero-copy-column">
    <p class="eyebrow">AI-NATIVE KUBERNETES TUI</p>
    <h1>See the cluster.<br>Drive the response.</h1>
    <p class="hero-copy">Operate from the keyboard, delegate bounded investigation to an agent, or connect an external assistant over MCP. Every write still stops for you.</p>
    <div class="hero-actions">
      <a class="md-button md-button--primary" href="getting-started/">Start flying</a>
      <a class="md-button" href="https://github.com/hellices/korvid">View on GitHub</a>
    </div>
    <div class="install-command" tabindex="0" role="group" aria-label="Install the current korvid release with uv"><span class="install-command__prompt" aria-hidden="true">$</span><code>uv tool install 'korvid[all]==0.3.0'</code></div>
  </div>
  <figure class="hero-demo">
    <div class="hero-demo__frame">
      <div class="hero-demo__bar" aria-hidden="true"><span></span><strong>ctx:(current) · ns:shop</strong></div>
      <video src="assets/demo.mp4" poster="assets/scenes/cockpit-poster.png" controls muted loop playsinline preload="metadata" aria-label="korvid browsing, filtering, describing, and following logs for a failing workload">Your browser does not support the korvid demo video.</video>
    </div>
    <figcaption><strong>Real korvid, synthetic cluster.</strong> The cockpit needs only your kubeconfig; AI is optional.</figcaption>
  </figure>
</section>

## One operational experience. Three ways to drive it.

Korvid coordinates one operational experience around the active cluster
context, navigation semantics, and approval/audit boundary. The TUI presents
watch-backed snapshots while embedded-agent and MCP tools can perform fresh
reads, so their snapshots can differ in time. **Different surfaces. Shared
context and safety.**

<div class="feature-grid">
  <article><span>01</span><h3>Drive it yourself</h3><p>Browse, filter, follow relationships, and run guarded operations through the watch-backed TUI while staying within the active context and safety boundary.</p></article>
  <article><span>02</span><h3>Delegate to the agent</h3><p>An embedded agent performs bounded fresh reads in the active context, cites its evidence, and drives korvid on your behalf. Its writes still cross the confirmation gate.</p></article>
  <article><span>03</span><h3>Connect over MCP</h3><p>When enabled, give VS Code, Claude Code, Cursor, or Zed bounded fresh reads in the active context. Write proposals stay opt-in and always need a human to confirm.</p></article>
</div>

## Sharp tools. Human hands.

Every mutation requires a fresh keystroke confirmation — whether you
triggered it directly, the embedded agent proposed it after reading the
evidence, or an MCP client submitted a request when MCP is enabled. MCP
write proposals are opt-in and never executed automatically. Whoever
initiates it, the write converges on the same in-TUI confirmation and the
same fail-closed audit path. Embedded-agent provider calls pass through
credential-pattern masking. MCP
results instead follow the per-tool disclosure boundaries documented for
external clients and their models. `--readonly` removes the write path entirely.

[Read the safety model](ops.md){ .md-button .korvid-button } [Inspect the threat model](threat-model.md){ .md-button .korvid-button }

## Find your flight path

- **Operating a cluster?** Start with the [five-minute guide](getting-started.md), then keep the [key reference](keybindings.md) nearby.
- **Adding AI?** Configure the [embedded agent](agent.md) or connect an [external MCP client](mcp.md).
- **Evaluating production use?** Read [performance and scale](performance.md), [air-gapped operation](airgap.md), and the [threat model](threat-model.md).
- **Contributing?** Begin with the [architecture](dev/specs/2026-08-12-korvid-architecture.md) and [quality gates](dev/quality-gates.md).
