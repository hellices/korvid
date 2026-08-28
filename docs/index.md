---
template: home.html
title: korvid
hide:
  - navigation
  - toc
---

<section class="hero hero--drivers" data-scene-switcher>
  <div class="hero-heading">
    <p class="eyebrow">AI-NATIVE KUBERNETES TUI</p>
    <h1>See the cluster.<br>Drive the response.</h1>
  </div>
  <div class="hero-copy-column">
    <p class="hero-copy">Operate from the keyboard, delegate bounded investigation to an agent, or connect an external assistant over MCP. Every write still stops for you.</p>
    <div class="hero-actions">
      <a class="md-button md-button--primary" href="getting-started/">Start flying</a>
      <a class="md-button" href="https://github.com/hellices/korvid">View on GitHub</a>
    </div>
    <div class="install-command" tabindex="0" role="group" aria-label="Install the current korvid release with uv"><span class="install-command__prompt" aria-hidden="true">$</span><code>uv tool install 'korvid[all]==0.3.0'</code></div>
  </div>
  <figure class="hero-demo hero-driver-stage">
    <div class="scene-tabs" role="tablist" aria-label="Choose who drives korvid">
      <button id="scene-tab-direct" type="button" role="tab" aria-selected="true" aria-controls="scene-direct">Direct</button>
      <button id="scene-tab-agent" type="button" role="tab" aria-selected="false" aria-controls="scene-agent" tabindex="-1">Agent</button>
      <button id="scene-tab-mcp" type="button" role="tab" aria-selected="false" aria-controls="scene-mcp" tabindex="-1">MCP</button>
    </div>
    <div class="scene-panels">
      <article id="scene-direct" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-direct" tabindex="0">
        <video src="assets/demo.mp4" poster="assets/scenes/cockpit-poster.png" controls muted loop playsinline preload="metadata" aria-label="korvid browsing, filtering, describing, and following logs for a failing workload">Your browser does not support the korvid demo video.</video>
      </article>
      <article id="scene-agent" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-agent" tabindex="0" hidden>
        <video data-src="assets/scenes/agent-demo.mp4" data-poster="assets/scenes/agent-poster.png" controls muted loop playsinline preload="none" aria-label="A deterministic synthetic-cluster walkthrough: a prompt submitted in korvid's real AgentPanel, real diagnose_pod and get_logs reads that agent follow mirrors onto the screen, and a grounded answer citing E1 and E2">Your browser does not support this deterministic synthetic-cluster walkthrough.</video>
        <img class="scene-panel__fallback" src="assets/scenes/agent-poster.png" width="1280" height="720" loading="lazy" alt="Korvid's real AgentPanel ending a deterministic synthetic-cluster walkthrough: the submitted prompt, real diagnose_pod and get_logs tool events, a grounded answer citing E1 and E2, and the describe pane agent follow mirrored beside it">
      </article>
      <article id="scene-mcp" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-mcp" tabindex="0" hidden>
        <video data-src="assets/scenes/mcp-follow-demo.mp4" class="mcp-media" data-poster="assets/scenes/mcp-poster.png" controls muted loop playsinline preload="none" aria-label="A clean local MCP SDK client making real read-only MCP requests over Streamable HTTP while korvid follow mode mirrors each answer onto the screen">Your browser does not support this MCP follow demo.</video>
        <img class="scene-panel__fallback mcp-media" src="assets/scenes/mcp-poster.png" width="1280" height="710" loading="lazy" alt="A clean local MCP SDK client reading a synthetic cluster over Streamable HTTP while korvid follow mode mirrors the same table, describe, and log views">
      </article>
    </div>
    <figcaption><strong>Real korvid, synthetic cluster.</strong> The Agent tab is a deterministic synthetic-cluster walkthrough: real read tools over a fixture, not a live model, a live cluster, or an answer-quality claim.</figcaption>
  </figure>
</section>

<section class="feature-highlights" aria-labelledby="highlights-title">
  <h2 id="highlights-title">One product contract, whoever drives.</h2>
  <div class="feature-highlights__grid">
    <article>
      <span>SEE</span>
      <p>Keyboard-first browsing of live resource tables, log streams, and metadata-only relationships.</p>
      <ul>
        <li><a href="tui/">Resource cockpit</a></li>
        <li><a href="tui/#work-with-logs">Log workflow</a></li>
        <li><a href="resource-relationships/">Relationships</a></li>
      </ul>
    </article>
    <article>
      <span>GROUND</span>
      <p>The watch-backed table, korvid's own fresh describe and log reads, and the bounded fresh reads behind the embedded agent and every MCP client all land at different moments, so snapshots can differ.</p>
      <ul>
        <li><a href="agent/">Embedded agent</a></li>
        <li><a href="mcp/">MCP tools</a></li>
        <li><a href="tui/#follow-one-signal">Diagnosis surfaces</a></li>
      </ul>
    </article>
    <article>
      <span>CONTROL</span>
      <p>Every write previews the change and waits for a fresh approval keystroke; a fail-closed audit append that fails blocks the action. MCP write proposals stay off by default. Embedded provider payloads are masked; MCP result disclosure is tool-specific.</p>
      <ul>
        <li><a href="ops/">Approval and audit</a></li>
        <li><a href="threat-model/">Provider masking, MCP disclosure</a></li>
        <li><a href="overview/">Architecture</a></li>
      </ul>
    </article>
  </div>
</section>

<nav class="flight-paths" aria-labelledby="flight-paths-title">
  <h2 id="flight-paths-title">Go from proof to practice.</h2>
  <div class="flight-paths__grid">
    <a href="getting-started/"><strong>Start operating</strong><span>Install korvid and take the five-minute route.</span></a>
    <a href="agent/"><strong>Explore Agent and MCP</strong><span>Choose a provider, then expose bounded read-only tools.</span></a>
    <a href="performance/"><strong>Evaluate production use</strong><span>Check scale, air-gap, and threat assumptions.</span></a>
  </div>
</nav>
