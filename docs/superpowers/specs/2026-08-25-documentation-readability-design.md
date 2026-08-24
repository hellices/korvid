# Documentation Readability Redesign

**Date:** 2026-08-25
**Status:** Approved for implementation planning

## Context

Korvid's home page and Quick Start already communicate the product well through
real interface captures, diagrams, short copy, and a clear visual rhythm. The
guides beginning with Keybindings are less effective because many of them mix
product explanation, task instructions, exhaustive feature inventories,
configuration reference, and edge cases on the same page.

The redesign will not preserve that volume by placing the same material in
cards, tabs, accordions, or additional pages. It will edit the documentation
around the core experiences users need to understand, while retaining facts
whose omission would make setup fail, weaken a safety guarantee, or
misrepresent an evidence boundary.

## Goals

- Make the user-facing guides from Keybindings through Administration easy to
  scan and understand.
- Preserve and reuse the existing diagrams, screenshots, images, and recordings
  whenever they remain accurate and informative.
- Give each page a composition suited to its subject instead of applying a
  universal documentation template.
- Introduce Korvid through its core operational capabilities rather than an
  exhaustive inventory of features and options.
- Keep safety, evidence, installation, compatibility, and measured-performance
  claims precise.
- Extend the established Korvid visual language without creating an unrelated
  design system or unnecessary JavaScript.

## Non-goals

- Rewriting the home page or the approved Quick Start experience.
- Preserving every existing detail.
- Making every page look structurally identical.
- Splitting removed material into many new pages merely to retain it.
- Turning technical references such as provider plugin contracts into visual
  product pages.
- Changing Korvid application behavior, packaging, or runtime dependencies.

## Editorial Model

Each page starts with one or two questions it must answer. Content that does not
help answer those questions is removed unless it is required for safe or
successful use.

The visual form follows the content:

- **SVG** for spatial or contextual models such as keyboard contexts and
  annotated interface regions.
- **Mermaid** for flows, trust boundaries, evidence paths, and lifecycles.
- **Real captures** for actual product states, confirmations, and workflows.
- **Tables** for compact comparisons and small reference sets.
- **Storyboards** for ordered operational sequences.
- **Short prose** for interpretation, caveats, and decisions that visuals cannot
  convey accurately.

Visuals must carry information. Decorative diagrams that repeat adjacent prose
will not be added.

## Content Decisions

### Preserve

- Existing media that is accurate, legible, privacy-safe, and still supports the
  page's core story.
- Installation- and setup-critical commands, values, and prerequisites.
- Safety invariants and limits, including fresh-keystroke approval and
  fail-closed audit logging.
- Evidence provenance and freshness boundaries.
- Honest capability limitations and operation-specific qualifications.
- Measured performance results together with their environment and methodology.
- Useful deep links, semantic headings, and searchable terminology where the
  retained content still exists.

### Condense

- Repeated explanations of the shared Direct, Agent, and MCP contract.
- Long procedural descriptions that a diagram or storyboard can express more
  clearly.
- Large key, option, or feature tables into high-value subsets.
- Multiple examples that demonstrate the same behavior into one representative
  example.
- Configuration guidance into the minimum successful recipe plus links to
  genuinely necessary reference material.

### Remove

- Rare options and edge cases that do not belong in an introductory user guide.
- Internal implementation details that do not affect a user's decision or
  outcome.
- Exhaustive action, mutation, control, or configuration inventories.
- Duplicate prose already communicated by an accurate visual.
- Aspirational or decorative content that does not improve understanding.

Removal is intentional editorial selection, not hidden archival. Content will
not be moved into accordions or new pages by default.

## Page Treatments

### Keybindings

Answer: *How do controls change with context, and which keys are essential?*

- Replace the single exhaustive table with a contextual keyboard map.
- Keep compact tables for navigation, inspection, and action keys that users
  need frequently.
- Keep one short remapping recipe.
- State clearly that approval-dialog confirmation keys are not remappable.
- Remove the exhaustive action-name inventory from the main reading flow.

### TUI

Answer: *How does a user move from cluster overview to useful evidence?*

- Use an annotated real cockpit capture to orient the reader.
- Present one coherent workflow: navigate, filter, inspect, and open logs or
  related evidence.
- Reuse existing captures and recordings where they truthfully show the flow.
- Remove catalog-style explanations of every control, state, and option.

### Operations and Safety

Answer: *What happens before and after a Kubernetes write?*

- Preserve and refine the existing guarded-write Mermaid flow.
- Pair it with a real confirmation or preview state.
- Explain a small representative set of write operations rather than every
  mutation subtype.
- Preserve the approval, audit, read-only, and protected-context guarantees.
- Describe SSAR, dry-run, ownership, and impact previews as best-effort or
  operation-specific rather than universal prerequisites.

### Resource Relationships

Answer: *How does Korvid connect resources, and how certain is each connection?*

- Replace long taxonomy prose with a relationship graph.
- Add a compact legend for relation source, resolution state, confidence, and
  coverage.
- Keep limitations that prevent users from interpreting partial coverage as a
  complete dependency graph.
- Remove exhaustive per-resource enumeration where it adds no new semantics.

### Helm and Operators

Answer: *How does Korvid support the release lifecycle?*

- Use an install, inspect, upgrade, and rollback storyboard.
- Show the transition between release state and underlying resources.
- Keep safeguards and prerequisites that affect write outcomes.
- Remove exhaustive navigation and configuration inventories.

### Observability

Answer: *How does live resource state connect to metrics and logs?*

- Reuse existing evidence and integration visuals where accurate.
- Show the boundary between watch-backed table state and fresh independent
  reads.
- Keep setup-critical backend configuration and explicit limitations.
- Remove repeated backend capability lists.

### Embedded Agent

Answer: *How does a prompt become a grounded answer or an approved proposal?*

- Show prompt, bounded tool/evidence use, citations, and optional write proposal
  as an evidence flow.
- Preserve provider payload boundaries and mandatory approval for writes.
- Keep the disclosure that existing Agent media is a deterministic AgentPanel
  walkthrough, not live provider execution or grounded tool calls.
- Remove tuning, evaluation, and configuration exposition that is not required
  for the first successful use.

### MCP Server

Answer: *What can an external client observe or propose through Korvid?*

- Show external client, MCP tools, Korvid, and Kubernetes as a trust and evidence
  flow.
- Distinguish read behavior, optional follow behavior, and opt-in write
  proposals.
- Preserve tool-specific evidence disclosure; do not imply an identical
  snapshot across Direct, Agent, and MCP drivers.
- Condense exhaustive tool listings to representative capabilities and the
  information needed to connect safely.

### Provider Plugins

Provider Plugins remains a technical reference. It may receive navigation,
heading, and prose cleanup, but it will not be aggressively reduced or forced
into the visual product-guide style. Contract details needed by implementers
remain available.

### Administration

Air-gapped Operation, Performance and Scale, and Threat Model answer decisions
an operator must make rather than enumerate every internal mechanism.

- **Air-gapped operation:** show the dependency and artifact path, then retain
  only the steps required for an offline deployment.
- **Performance and scale:** lead with an executive visual summary while
  preserving benchmark conditions and measured data.
- **Threat model:** visualize trust boundaries and guarded transitions while
  retaining concrete assumptions, exclusions, and residual risks.

## Information Architecture

The current top-level navigation remains stable:

- Getting Started
- Operate
- AI and Integrations
- Administration
- Project

Routes will not be split merely to reduce page length. If a removed section has
a known inbound deep link, the redesign will either retain a meaningful anchor
near the corresponding concise content or update repository-owned links. Project
and contributor documentation remain dense technical material where that form
is appropriate.

## Visual and Technical Constraints

- Reuse canonical repository media before producing new assets.
- New captures must be deterministic, reproducible, privacy-safe, and accurately
  disclosed.
- New SVGs must have useful accessible text or an equivalent textual
  explanation.
- Mermaid diagrams must remain understandable in rendered, print, and
  reduced-motion contexts.
- Responsive layouts must work on desktop, tablet, and mobile without requiring
  horizontal page scrolling.
- No new JavaScript will be added unless a specific interaction communicates
  information that static HTML, CSS, SVG, or Mermaid cannot.
- Documentation remains outside `src/korvid` and outside the distributed Python
  package.

## Accuracy Invariants

The redesign must not weaken or blur these facts:

- Direct TUI, embedded Agent, and external MCP share operational context and
  safety contracts, but not necessarily an identical evidence snapshot.
- Resource tables are watch-backed.
- Describe, events, and logs may be fresh independent reads.
- MCP evidence disclosure is tool-specific.
- Agent write tools always pass through approval.
- Approval requires a fresh user keystroke.
- Audit logging is fail-closed: an audit append failure blocks the mutation.
- SSAR, dry-run, ownership, and impact previews are best-effort or
  operation-specific.
- Approval-dialog confirmation keys are not presented as remappable.

## Verification

Implementation is complete only when:

- The selected pages no longer read as exhaustive feature inventories.
- Existing informative media remains present or has an explicitly documented
  reason for removal or replacement.
- Strict MkDocs build and repository documentation-link tests pass.
- Added or changed visual contracts have focused tests following the existing
  landing-page test style.
- Desktop, tablet, and mobile layouts are inspected.
- Keyboard navigation, focus visibility, no-JavaScript fallback, reduced motion,
  and meaningful alternative text are verified where relevant.
- Media provenance, loading behavior, and privacy checks continue to pass.
- Repository-owned links to removed headings are updated or compatible anchors
  are retained.

## Implementation Boundary

This is one editorial redesign cycle covering the user-facing pages from
Keybindings through Administration. Work should proceed in coherent page groups
so each group can be reviewed and validated before the next:

1. Keybindings, TUI, and Operations.
2. Resource Relationships, Helm and Operators, and Observability.
3. Embedded Agent and MCP.
4. Administration, followed by restrained Provider Plugins cleanup.

This grouping controls implementation and review risk; it does not create new
public navigation phases or require separate pull requests.
