# Visual storytelling for the official site

## Goal

Turn the official korvid site from a polished documentation portal into a
product-led explanation that a Kubernetes operator can understand before
reading the manuals.

The current site is accurate, searchable, and deployable, but its central
claims are still carried mostly by prose. A visitor must read several
paragraphs to understand:

- what the cockpit actually looks like;
- how direct operation, the embedded agent, and MCP relate;
- which parts of the experience are shared;
- where their read paths differ; and
- why an agent-initiated or MCP-initiated write cannot bypass a human.

The redesign must make those answers visible without weakening the precise
security and data-freshness language already established in the
documentation.

## Success criteria

At desktop and mobile widths, a first-time visitor should be able to answer
three questions from the landing page without opening another document:

1. What does korvid let me do?
2. How can a human, an embedded agent, or an external MCP client drive it?
3. What stops any of those paths from performing an unchecked write?

The page is successful when:

- an actual korvid screen is the dominant evidence above the fold;
- the three control surfaces and the shared safety boundary are understandable
  from a diagram and labels, not a paragraph;
- at least one real product scene supports each of direct, agent, and MCP use;
- the write path is shown as an ordered approval and audit sequence;
- major capabilities are represented by annotated product screens rather than
  a list of feature claims;
- every capability claim is limited to what its capture actually contains;
- every visual has an equivalent caption, transcript, or semantic text path;
  and
- the page remains a static, self-hosted MkDocs site with no runtime third-party
  requests.

This is a comprehension goal, not an animation goal. Motion is useful only
when it demonstrates real product behavior.

## Decision

Use **evidence-led product storytelling**.

Keep MkDocs Material and GitHub Pages, but restructure the landing page as a
short visual narrative:

1. see the real cockpit;
2. compare the three ways to drive it;
3. understand the product and trust boundaries;
4. follow one guarded write from intent to execution; and
5. inspect representative features in the interface.

Add only a small, dependency-free progressive-enhancement script where a
native document cannot express scene switching clearly. Do not build a
browser Kubernetes simulator or a second front-end application.

### Alternatives considered

**Interactive browser simulator**

A fake cluster and terminal emulator would be memorable, but it would create
a second behavioral implementation that can drift from korvid. It would also
expand accessibility, testing, load-time, and security scope substantially.
The result could look more "live" while being less truthful.

**Diagram-first documentation sweep**

Adding Mermaid diagrams throughout the manuals would improve reference
material, but it would not fix the landing page's weak product proof. The
highest-value diagrams should be added selectively after the landing
narrative is coherent.

## Visual narrative

### 1. Hero: product first

The hero becomes a two-part composition:

- a compact statement, install command, and primary actions; and
- a large product stage showing a real korvid recording with an intentional
  poster frame.

The key legend is removed from the hero. It is useful reference material, but
it competes with the product itself and asks a new visitor to decode commands
before seeing their result.

The initial copy should state only:

- korvid is a keyboard-first Kubernetes cockpit;
- AI is optional; and
- agent and MCP access do not introduce an unchecked write path.

The existing base-TUI recording remains the first proof because it requires
only a kubeconfig and establishes that korvid is a product, not an AI wrapper.
Its poster must show a populated, legible operational screen rather than an
empty table.

### 2. One incident, three drivers

Replace the numbered prose cards with a scene switcher titled
**One incident. Three ways to drive it.**

The three scenes use the same operational scenario: find a failing workload,
inspect its evidence, and navigate to the relevant logs or owner.

- **Direct:** the operator filters and follows the relationship from the TUI.
- **Agent:** the operator asks from `Ctrl-A`; in the product the agent performs
  bounded reads, cites evidence, and drives the TUI. The media this design
  ships is a **deterministic scripted AgentPanel walkthrough** — the harness
  runtime discards the prompt and screen context and emits fixed tool and
  citation events — so the scene's own labels claim only real panel input,
  submission, and rendering of scripted events, plus one sentence disclosing
  that the capture does not execute the provider or tool pipeline.
- **MCP:** an editor or external assistant performs bounded reads and asks
  korvid to navigate or submit a write proposal when MCP writes are enabled.

Each scene contains:

- a real recording or annotated screenshot;
- a one-sentence description;
- visible input, evidence, and result labels; and
- a link to the detailed guide.

The switcher uses semantic buttons with a tablist pattern. A small local
script changes the active scene and pauses inactive media. Without JavaScript,
all three scenes render in document order. The fallback is complete content,
not a success-shaped empty panel.

### 3. Product contract map

Present a responsive architecture graphic immediately after the scenes.
It must describe the product contract without pretending that every path
shares one evidence snapshot or one code package.

The diagram has two explicit lanes:

**Read and navigation lane**

- Human operator -> TUI -> watch-backed `ResourceStore` table, plus korvid's
  own fresh describe and log reads
- Model/provider -> embedded agent -> bounded fresh tool reads
- Editor/external assistant -> MCP -> bounded fresh tool reads
- All three retain the active cluster context and shared navigation semantics
- The watch-backed table, korvid's own describe and log reads, and each tool's
  fresh reads are taken at different moments, so snapshots can differ

**Write lane**

- Direct action, agent proposal, or opt-in MCP proposal
- In-TUI confirmation requiring a fresh human keystroke
- Fail-closed audit append
- Validated execution against Kubernetes

This should be semantic HTML styled as a diagram, not a raster image. HTML
allows the lanes to reflow vertically on narrow screens, keeps the labels
selectable, and gives assistive technology the same ordering as the visual
arrows. The architecture document may retain a more detailed Mermaid diagram;
the landing map is the product contract, not a package dependency graph.

### 4. Guarded write sequence

Replace the long **Sharp tools. Human hands.** paragraph with a compact ordered
sequence:

1. **Observe** — direct, agent, and MCP paths gather bounded evidence.
2. **Propose** — the requested mutation is rendered for review.
3. **Confirm** — only a fresh user keystroke can approve it.
4. **Audit** — the entry must be written successfully.
5. **Execute** — validation and Kubernetes execution occur only after audit.

An explicit blocked branch leaves **Audit**:

> Audit write failed -> action blocked

The branch is essential. A generic shield icon would imply safety; this
sequence explains the enforced mechanism.

The diagram does not collapse disclosure rules. Embedded-agent provider calls
use credential-pattern masking, while MCP results follow each tool's external
disclosure contract. Those details remain linked from the safety and threat
model pages.

### 5. Capability evidence mosaic

Show six representative product scenes in an asymmetric mosaic:

1. resource browsing with status, scope, and restart signals;
2. relationship graph navigation;
3. a filtered, live single-pod log stream;
4. diagnosis with events and evidence;
5. an agent panel walkthrough rendering a prompt, a scripted tool event, and a
   scripted answer whose citation marker the panel flags as unsupported; and
6. an MCP-driven follow or navigation sequence.

Use real screens captured from the deterministic in-memory synthetic harness
in `docs/demo/demo.py` or from a disposable local cluster. Annotation text
must be HTML overlays or adjacent captions, not text burned into an image.
This preserves readability, responsive layout, and accessible alternatives.

The harness has no metrics source, so its CPU/MEM columns render placeholders.
No tile, caption, or criterion may present a capture from it as evidence of
utilization.

The agent media likewise comes from a scripted harness runtime rather than the
product's agent pipeline: it discards the prompt and screen context and emits
fixed tool and citation events. Because that turn mints no evidence, the
harness reports its `[E1]` marker as **uncited**, so the recording shows
korvid's own yellow **unsupported citation** note under the scripted answer —
the product behaving correctly on an unsourced claim. Every surface that ships
it labels it a deterministic scripted AgentPanel walkthrough whose citation the
panel flags, and no tile, scene label, caption, or criterion may present it
otherwise: the capture is **not evidence of bounded fresh reads, live tool
execution, or validated citations**, and the row the demo happens to have
selected is not grounding for the scripted answer. Documenting those production
capabilities on the linked agent page is unaffected.

The mosaic is not a feature dump. Every tile has the same structure:

- the operator's intent;
- the visible korvid response; and
- the guide where the behavior is documented.

### 6. Flight paths

The final section remains a documentation router, but changes from a prose
list to four compact destination cards:

- Operate a cluster
- Add the embedded agent
- Connect an MCP client
- Evaluate production use

Contributor links move to the footer or project navigation. They are
important, but they are not part of the first product narrative.

## Content density rules

The redesign must not reproduce the current text density inside more
decorative containers.

- At desktop width, the actual product occupies at least half of the hero's
  visual area.
- Each visual section uses at most one short setup paragraph before its
  evidence.
- A scene, feature tile, or destination card uses no more than one sentence of
  body copy.
- Detailed qualifications live in the linked documentation, while captions
  retain the facts necessary to interpret the visual accurately.
- No more than one landing-page section may be prose-led; every other section
  must be anchored by real media, a semantic diagram, or an ordered flow.
- Decorative icons do not count as evidence.

These are layout and editorial constraints, not a reason to remove accessible
descriptions. Transcripts and semantic fallback text remain complete even when
the primary visual presentation is concise.

## Selective documentation enrichment

Do not redesign every documentation page. Add one high-value visual to each
page where a diagram or screen replaces substantial explanation:

- **Overview:** the complete modular product map and installation composition.
- **TUI:** an annotated interface anatomy screen and the core navigation loop.
- **Embedded agent:** a four-step storyboard of a production turn — current
  selection to reads, citations, and UI drive — kept visibly separate from the
  illustrative scripted capture beside it, which carries its own note that no
  provider and no real read tool run.
- **MCP:** external client to MCP read/UI-drive flow, with the opt-in proposal
  boundary shown separately.
- **Operations and safety:** the full confirmation, audit, and execution
  sequence.
- **Resource relationships:** one real graph screen with its traversal model.

Other pages keep conventional documentation layouts. Reference material
should be readable, not turned into a marketing storyboard.

## Components and file boundaries

The implementation remains inside the documentation surface:

- `docs/index.md` owns semantic landing-page structure and copy.
- `docs/stylesheets/extra.css` owns layout, diagrams, annotations, and
  responsive/reduced-motion behavior.
- `docs/assets/javascripts/visual-storytelling.js` owns only scene selection,
  media pause/play behavior, and keyboard state for the tablist. It enhances
  each switcher independently: every tab's panel is resolved before any state
  is written, the enhancement hook is set only once the initial selection has
  succeeded, and a switcher that cannot be enhanced is restored to its
  no-JavaScript rendering and reported rather than left half-switched.
- `docs/assets/` owns optimized local posters, recordings, and screenshots.
- the selected concept pages own their detailed visual and explanatory text.
- `mkdocs.yml` loads the local script and does not add external runtime assets.

The Python package, application runtime, and wheel contents remain unchanged.
Documentation assets stay outside `src/korvid` and remain covered by packaging
boundary tests.

The script must expose no global service object, fetch no data, and have no
knowledge of Kubernetes. It enhances a document; it is not an application
layer.

## Asset production and performance

Visuals must come from reproducible demo scenarios against the deterministic
in-memory synthetic harness or a disposable local cluster. Whichever path a
scene uses, no capture may contain a real cluster, credential, customer, or
production identifier. Record the scenario, commands, fixture versions, and
capture steps under `docs/demo/` so a future behavior change can regenerate
the assets.

Asset rules:

- prefer MP4 plus a WebP or PNG poster for motion;
- do not autoplay more than the single muted hero demonstration;
- lazy-load media below the first viewport;
- pause inactive or off-screen scene media;
- avoid animated GIF for newly produced long recordings;
- preserve the existing no-runtime-third-party-request policy; and
- checksum executable JavaScript assets in the existing documentation asset
  contract tests.

The initial viewport should load the stylesheet, hero poster, and only the
media needed for the active hero. Below-fold recordings must not be eagerly
downloaded merely because they exist in the DOM.

## Accessibility and resilient behavior

Every visual must remain understandable without color, motion, hover, or
JavaScript.

- Videos have concise accessible names, captions beside the media, and a
  transcript or step summary.
- Diagram nodes use semantic headings and ordered lists in reading order.
- Connections use labels as well as lines or color.
- The scene switcher follows tablist keyboard behavior and exposes selected
  state.
- Focus indicators use the existing amber-on-charcoal visual system.
- `prefers-reduced-motion` disables decorative transitions and prevents
  programmatic autoplay.
- Poster frames and text remain present if media playback fails.
- If the enhancement script fails to load, all scenes remain visible and all
  guide links continue to work.

## Privacy and security

The site remains static and anonymous:

- no analytics;
- no cluster connection;
- no embedded third-party players;
- no remote fonts, scripts, images, or telemetry;
- no product screenshot may contain credentials, real cluster identifiers,
  customer names, or sensitive manifests; and
- recording fixtures use synthetic namespaces, workloads, logs, and events.

The visual architecture must preserve the documented security invariants:

- agent writes always cross the approval gate;
- MCP writes are opt-in proposals and never execute automatically;
- sensitive embedded-agent provider reads pass through masking;
- `run_kubectl` remains validated;
- approval requires a fresh user keystroke; and
- audit failure blocks the action.

## Testing and verification

Use the existing documentation toolchain and add focused contract coverage.

Automated checks must cover:

- `mkdocs build --strict`;
- all referenced images, posters, recordings, transcripts, and scripts exist;
- landing media uses local URLs and below-fold media is lazy;
- scene controls and panels have matching accessible identifiers;
- all three scenes remain present in the no-JavaScript source order;
- executable assets have pinned checksums;
- no documentation asset enters the Python wheel;
- canonical URLs and Pages deployment behavior remain unchanged; and
- representative security language and the audit-failure branch remain in the
  rendered source.

Manual browser verification covers widths near 390, 768, and 1440 pixels,
keyboard-only scene switching, reduced-motion behavior, media failure
fallbacks, and the absence of runtime third-party requests.

## Non-goals

This iteration does not add:

- a live or simulated Kubernetes cluster in the browser;
- a terminal emulator;
- user accounts, analytics, personalization, or a backend;
- a JavaScript framework or new frontend build toolchain;
- decorative animation unrelated to product behavior;
- a visual rewrite of every reference page;
- presenting the scripted AgentPanel capture as proof of the provider, tool, or
  citation-validation pipeline; or
- claims that TUI, agent, and MCP reads always share an identical real-time
  snapshot.

## Rollout

Implement the work in independently reviewable slices:

1. restructure the hero and reuse the existing base-TUI demo;
2. add the three-driver scene switcher with real direct, agent, and MCP proof;
3. add the product contract map and guarded-write sequence;
4. add the capability mosaic and compact flight paths;
5. enrich the selected concept pages; and
6. run strict build, contract tests, responsive review, and production Pages
   verification.

Each slice must leave the landing page complete and readable. A missing future
asset must not be represented by a placeholder card or invented product
screen.
