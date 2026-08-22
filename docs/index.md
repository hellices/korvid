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

<section class="scene-switcher" data-scene-switcher aria-labelledby="scene-switcher-title">
  <div class="section-heading">
    <p class="eyebrow">ONE OPERATIONAL EXPERIENCE</p>
    <h2 id="scene-switcher-title">One incident. Three ways to drive it.</h2>
    <p>Find the failing workload, inspect its evidence, and land on the useful screen—directly, through the embedded agent, or from an external MCP client.</p>
  </div>
  <div class="scene-tabs" role="tablist" aria-label="Choose who drives korvid">
    <button id="scene-tab-direct" type="button" role="tab" aria-selected="true" aria-controls="scene-direct">You drive</button>
    <button id="scene-tab-agent" type="button" role="tab" aria-selected="false" aria-controls="scene-agent" tabindex="-1">Agent delegates</button>
    <button id="scene-tab-mcp" type="button" role="tab" aria-selected="false" aria-controls="scene-mcp" tabindex="-1">MCP connects</button>
  </div>
  <div class="scene-panels">
    <article id="scene-direct" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-direct" tabindex="0">
      <video src="assets/demo.mp4" poster="assets/scenes/cockpit-poster.png" controls muted loop playsinline preload="none" aria-label="An operator filters to a failing pod, describes it, and opens its logs in korvid">Your browser does not support this direct-operation demo.</video>
      <div><strong>Input</strong> Keyboard intent</div>
      <div><strong>Evidence</strong> Watch-backed TUI snapshot</div>
      <div><strong>Result</strong> The real korvid view</div>
      <p>You stay on the live cockpit and choose every next step.</p>
      <a href="tui/">Explore the TUI</a>
    </article>
    <article id="scene-agent" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-agent" tabindex="0">
      <video src="assets/scenes/agent-demo.mp4" poster="assets/scenes/agent-poster.png" controls muted loop playsinline preload="none" aria-label="The embedded agent diagnoses the failing payment worker and cites its evidence">Your browser does not support this embedded-agent demo.</video>
      <div><strong>Input</strong> Current selection + prompt</div>
      <div><strong>Evidence</strong> Bounded fresh reads + citations</div>
      <div><strong>Result</strong> Answer and UI drive</div>
      <p>The agent investigates in context while writes remain proposals.</p>
      <a href="agent/">Explore the embedded agent</a>
    </article>
    <article id="scene-mcp" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-mcp" tabindex="0">
      <video src="assets/scenes/mcp-follow-demo.mp4" poster="assets/scenes/mcp-poster.png" controls muted loop playsinline preload="none" aria-label="An external MCP client reads the cluster while korvid follow mode mirrors its navigation">Your browser does not support this MCP follow demo.</video>
      <div><strong>Input</strong> External assistant</div>
      <div><strong>Evidence</strong> Tool-specific bounded fresh reads</div>
      <div><strong>Result</strong> MCP response + optional follow</div>
      <p>MCP exposes bounded tools; write proposals are off by default.</p>
      <a href="mcp/">Explore MCP</a>
    </article>
  </div>
</section>

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
