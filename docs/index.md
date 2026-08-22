---
template: home.html
title: korvid
hide:
  - navigation
  - toc
---

<section class="hero">
  <div class="hero-heading">
    <p class="eyebrow">AI-NATIVE KUBERNETES TUI</p>
    <h1>See the cluster.<br>Drive the response.</h1>
  </div>
  <figure class="hero-demo">
    <div class="hero-demo__frame">
      <div class="hero-demo__bar" aria-hidden="true"><span></span><strong>ctx:(current) · ns:shop</strong></div>
      <video src="assets/demo.mp4" poster="assets/scenes/cockpit-poster.png" controls muted loop playsinline preload="metadata" aria-label="korvid browsing, filtering, describing, and following logs for a failing workload">Your browser does not support the korvid demo video.</video>
    </div>
    <figcaption><strong>Real korvid, synthetic cluster.</strong> The cockpit needs only your kubeconfig; AI is optional.</figcaption>
  </figure>
  <div class="hero-copy-column">
    <p class="hero-copy">Operate from the keyboard, delegate bounded investigation to an agent, or connect an external assistant over MCP. Every write still stops for you.</p>
    <div class="hero-actions">
      <a class="md-button md-button--primary" href="getting-started/">Start flying</a>
      <a class="md-button" href="https://github.com/hellices/korvid">View on GitHub</a>
    </div>
    <div class="install-command" tabindex="0" role="group" aria-label="Install the current korvid release with uv"><span class="install-command__prompt" aria-hidden="true">$</span><code>uv tool install 'korvid[all]==0.3.0'</code></div>
  </div>
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
      <video src="assets/scenes/agent-demo.mp4" data-poster="assets/scenes/agent-poster.png" controls muted loop playsinline preload="none" aria-label="The embedded agent diagnoses the failing payment worker and cites its evidence">Your browser does not support this embedded-agent demo.</video>
      <noscript><img src="assets/scenes/agent-poster.png" width="1280" height="720" alt="Korvid embedded agent diagnosing a synthetic failing payment worker with cited evidence"></noscript>
      <div><strong>Input</strong> Current selection + prompt</div>
      <div><strong>Evidence</strong> Bounded fresh reads + citations</div>
      <div><strong>Result</strong> Answer and UI drive</div>
      <p>The agent investigates in context while writes remain proposals.</p>
      <a href="agent/">Explore the embedded agent</a>
    </article>
    <article id="scene-mcp" class="scene-panel" role="tabpanel" aria-labelledby="scene-tab-mcp" tabindex="0">
      <video src="assets/scenes/mcp-follow-demo.mp4" data-poster="assets/scenes/mcp-poster.png" controls muted loop playsinline preload="none" aria-label="An external MCP client reads the cluster while korvid follow mode mirrors its navigation">Your browser does not support this MCP follow demo.</video>
      <noscript><img src="assets/scenes/mcp-poster.png" width="1280" height="710" alt="An external MCP client reading disposable local cluster data while korvid mirrors the navigation"></noscript>
      <div><strong>Input</strong> External assistant</div>
      <div><strong>Evidence</strong> Tool-specific bounded fresh reads</div>
      <div><strong>Result</strong> MCP response + optional follow</div>
      <p>MCP exposes bounded tools; write proposals are off by default.</p>
      <a href="mcp/">Explore MCP</a>
    </article>
  </div>
</section>

<section class="contract-map" aria-labelledby="contract-title">
  <div class="section-heading">
    <p class="eyebrow">ONE PRODUCT CONTRACT</p>
    <h2 id="contract-title">Different reads. Shared context and safety.</h2>
    <p>Korvid keeps each surface honest about where its evidence came from while preserving the same operational frame.</p>
  </div>
  <div class="contract-map__drivers">
    <article><span>Human operator</span><strong>Watch-backed TUI snapshot</strong></article>
    <article><span>Model / provider</span><strong>Bounded fresh reads</strong></article>
    <article><span>Editor / external assistant</span><strong>Bounded fresh reads over MCP</strong></article>
  </div>
  <div class="contract-map__shared" aria-label="Shared operational contract">
    <strong>Active cluster context</strong>
    <strong>Navigation semantics</strong>
    <strong>Approval gate</strong>
    <strong>Fail-closed audit</strong>
  </div>
  <p class="contract-map__truth">Fresh tool reads and the watch-backed TUI snapshot can differ in time, so snapshots can differ without splitting the product contract.</p>
  <a href="overview/">Inspect the complete architecture</a>
</section>

<section class="write-path" aria-labelledby="write-path-title">
  <div class="section-heading">
    <p class="eyebrow">SHARP TOOLS. HUMAN HANDS.</p>
    <h2 id="write-path-title">Every mutation stops at the same gate.</h2>
  </div>
  <div class="write-path__origins" aria-label="Write initiators">
    <span>Direct action</span>
    <span>Agent proposal</span>
    <span>Opt-in MCP proposal</span>
  </div>
  <ol class="write-path__stages">
    <li data-stage="observe"><span>01</span><strong>Observe</strong><small>Gather bounded evidence</small></li>
    <li data-stage="propose"><span>02</span><strong>Propose</strong><small>Render the intended change</small></li>
    <li data-stage="confirm"><span>03</span><strong>Confirm</strong><small>Fresh human keystroke</small></li>
    <li data-stage="audit"><span>04</span><strong>Audit</strong><small>Append must succeed</small></li>
    <li data-stage="execute"><span>05</span><strong>Execute</strong><small>Validate and mutate</small></li>
  </ol>
  <p class="write-path__blocked"><strong>Audit write failed</strong><span aria-hidden="true">→</span> action blocked. The audit path is fail-closed.</p>
  <p class="write-path__boundary">Embedded provider payloads are masked; MCP result disclosure is tool-specific. <a href="threat-model/">Inspect both boundaries.</a></p>
</section>

<section class="evidence-mosaic" aria-labelledby="evidence-title">
  <div class="section-heading">
    <p class="eyebrow">PRODUCT EVIDENCE</p>
    <h2 id="evidence-title">Built for the incident, not the screenshot.</h2>
    <p>Each surface below is a real korvid view captured against synthetic cluster data.</p>
  </div>
  <div class="evidence-mosaic__grid">
    <article class="evidence-card evidence-card--wide">
      <figure><img src="assets/scenes/cockpit-poster.png" width="1280" height="720" loading="lazy" alt="Korvid pod table filtered to a crash-looping payment worker"><figcaption>Resource cockpit</figcaption></figure>
      <p>Filter live resources and keep status, scope, and utilization in one keyboard path.</p>
      <a href="tui/">Browse the cockpit</a>
    </article>
    <article class="evidence-card">
      <figure><img src="assets/scenes/relationship-graph.png" width="1280" height="720" loading="lazy" alt="Korvid relationship screen showing a pod's declared ConfigMap dependency and the Service that depends on the pod"><figcaption>Relationship graph</figcaption></figure>
      <p>Follow metadata-only dependencies without exposing Secret values.</p>
      <a href="resource-relationships/">Follow relationships</a>
    </article>
    <article class="evidence-card">
      <figure><img src="assets/scenes/merged-logs.png" width="1280" height="720" loading="lazy" alt="Korvid log pane following synthetic payment worker logs"><figcaption>Merged logs</figcaption></figure>
      <p>Stream, merge, filter, and reconnect without leaving the selected workload.</p>
      <a href="tui/#log-viewer">Read the log workflow</a>
    </article>
    <article class="evidence-card">
      <figure><img src="assets/scenes/diagnosis.png" width="1280" height="720" loading="lazy" alt="Korvid describe view showing a synthetic failing pod and warning events"><figcaption>Operational evidence</figcaption></figure>
      <p>Put manifests, warning events, and failure context beside the selected resource.</p>
      <a href="tui/#ops-hints">Inspect diagnosis surfaces</a>
    </article>
    <article class="evidence-card">
      <figure><img src="assets/scenes/agent-poster.png" width="1280" height="720" loading="lazy" alt="Korvid embedded agent diagnosing a synthetic failing payment worker with cited evidence"><figcaption>Agent with citations</figcaption></figure>
      <p>Ask from the current screen and keep checkable evidence in the answer.</p>
      <a href="agent/">Use the embedded agent</a>
    </article>
    <article class="evidence-card evidence-card--wide">
      <figure><img src="assets/scenes/mcp-poster.png" width="1280" height="710" loading="lazy" alt="An external MCP client reading synthetic cluster data while korvid mirrors the navigation"><figcaption>MCP follow</figcaption></figure>
      <p>Let an external assistant read bounded tools while korvid can mirror where it went.</p>
      <a href="mcp/#follow-mode">Connect over MCP</a>
    </article>
  </div>
</section>

<nav class="flight-paths" aria-labelledby="flight-paths-title">
  <div class="section-heading">
    <p class="eyebrow">CHOOSE A FLIGHT PATH</p>
    <h2 id="flight-paths-title">Go from proof to practice.</h2>
  </div>
  <div class="flight-paths__grid">
    <a href="getting-started/"><strong>Operate a cluster</strong><span>Install and take the five-minute route.</span></a>
    <a href="agent/"><strong>Add the embedded agent</strong><span>Choose a provider and inspect its boundary.</span></a>
    <a href="mcp/"><strong>Connect an MCP client</strong><span>Expose bounded reads and optional proposals.</span></a>
    <a href="performance/"><strong>Evaluate production use</strong><span>Check scale, air-gap, and threat assumptions.</span></a>
  </div>
</nav>
