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
    <h1>A tool-using bird<br>for your cluster.</h1>
    <p class="hero-copy">Browse, diagnose, and operate Kubernetes from a keyboard-first cockpit. Add an agent that sees your screen—or let your editor inspect the cluster over MCP—without giving either one an unchecked write path.</p>
    <div class="hero-actions">
      <a class="md-button md-button--primary" href="getting-started/">Start flying</a>
      <a class="md-button" href="https://github.com/hellices/korvid">View on GitHub</a>
    </div>
    <div class="install-command" tabindex="0" role="group" aria-label="Install korvid with Homebrew"><span class="install-command__prompt" aria-hidden="true">$</span><code>brew install hellices/korvid/korvid</code></div>
  </div>
  <aside class="hero-panel" aria-label="korvid key legend">
    <div class="hero-panel__bar" aria-hidden="true">
      <span class="hero-panel__lights"></span>
      <span class="hero-panel__title">korvid — ctx:(current) ns:shop</span>
    </div>
    <dl class="hero-panel__keys">
      <dt><kbd>:</kbd></dt><dd>command bar — <span class="hero-panel__literal">pods</span>, <span class="hero-panel__literal">ns shop</span>, <span class="hero-panel__literal">helm</span></dd>
      <dt><kbd>/</kbd></dt><dd>filter: fuzzy, regex, label, exclude</dd>
      <dt><kbd>d</kbd></dt><dd>describe — manifest and events</dd>
      <dt><kbd>l</kbd></dt><dd>follow logs, <kbd>L</kbd> merges the filtered set</dd>
      <dt><kbd>g</kbd></dt><dd>relationship graph for the selection</dd>
      <dt><kbd>Ctrl-A</kbd></dt><dd>ask the agent about what you see</dd>
    </dl>
    <p class="hero-panel__note">Every key is remappable, and <kbd>?</kbd> always shows the effective set.</p>
  </aside>
</section>

<figure class="product-demo">
  <img src="assets/demo.gif" alt="korvid browsing pods, filtering resources, describing a pod, following logs, and opening help">
  <figcaption>The cockpit works with your kubeconfig alone. AI is optional.</figcaption>
</figure>

## One cockpit. Three ways in.

<div class="feature-grid">
  <article><span>01</span><h3>Keyboard-first TUI</h3><p>Watch any resource, filter instantly, follow relationships, merge logs, and run guarded operations without memorizing kubectl flag order.</p></article>
  <article><span>02</span><h3>Agent inside</h3><p>An optional agent sees the active view and selection, reads evidence, cites it, and drives the real interface instead of chatting beside it.</p></article>
  <article><span>03</span><h3>MCP outside</h3><p>Give VS Code, Claude Code, Cursor, or Zed bounded cluster reads and visible UI follow mode. Writes remain proposals for a human.</p></article>
</div>

## Sharp tools. Human hands.

Every mutation requires a fresh keystroke confirmation. Executed writes are
audited fail-closed. Secret values are masked before model calls, and
`--readonly` removes the write path entirely.

[Read the safety model](ops.md){ .md-button .korvid-button } [Inspect the threat model](threat-model.md){ .md-button .korvid-button }

## Find your flight path

- **Operating a cluster?** Start with the [five-minute guide](getting-started.md), then keep the [key reference](keybindings.md) nearby.
- **Adding AI?** Configure the [embedded agent](agent.md) or connect an [external MCP client](mcp.md).
- **Evaluating production use?** Read [performance and scale](performance.md), [air-gapped operation](airgap.md), and the [threat model](threat-model.md).
- **Contributing?** Begin with the [architecture](dev/specs/2026-08-12-korvid-architecture.md) and [quality gates](dev/quality-gates.md).
