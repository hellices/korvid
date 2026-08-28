# Distinctive homepage highlights

**Date:** 2026-08-28  
**Status:** Selected in unattended mode after the user requested subtle
differentiation without competitor comparisons  
**Direction:** One shared workspace, checkable evidence, human authority

This document supersedes only the highlight taxonomy in
`2026-08-27-compact-homepage-design.md` section 3 and its acceptance item that
names `SEE / GROUND / CONTROL`. Every other compact-homepage constraint remains
in force.

## Problem

The compact homepage's `SEE / GROUND / CONTROL` cards are accurate, but they
describe broad product qualities rather than the interaction model that makes
korvid feel different. Renaming the same categories with more technical verbs
would not materially improve that impression.

The homepage must surface korvid's distinctive behavior without naming or
comparing another product, claiming universal snapshots, or presenting AI as an
autonomous operator.

## Selected structure

Keep the existing three-card section and its position below the Direct / Agent /
MCP media stage. Replace the generic taxonomy with three product-specific
promises:

### 1. One workspace

Keyboard input, the embedded Agent, and an external MCP client all operate the
same visible cockpit rather than separate interfaces. Agent navigation changes
the panes the operator sees. MCP follow may mirror successful reads when enabled;
with follow disabled, activity remains visible through notifications.

This card links to the resource cockpit, Agent UI-driving behavior, and MCP
follow mode.

### 2. Checkable evidence

The embedded Agent gathers bounded cluster reads, mints references only for
successful reads, and lets the operator open those references in the actual
resource view. Deterministic diagnosis tools report evidence gaps instead of
silently presenting incomplete data as complete.

This card links to the Agent evidence flow, diagnosis surfaces, and MCP evidence
boundary. It must not imply that every sentence is cited, that Direct, Agent, and
MCP share one snapshot, or that every MCP result receives the embedded-provider
masking policy.

### 3. Human authority

Direct actions, Agent write requests, and opt-in MCP proposals converge on a
human-controlled write path. Agent and MCP integrations cannot confirm their own
actions. A fresh in-TUI user action remains required, and a failed audit append
blocks the mutation.

This card links to approval and audit, the threat model, and the architecture
overview. Preview language remains operation-specific and best-effort.

## Section framing

Replace the section heading with:

> One cockpit. Three ways in. You stay in command.

The media stage answers **who can drive**. The highlight cards answer **what
stays visible, verifiable, and human-controlled while they drive**. The cards
must not repeat the video narratives or add a comparison table.

## Copy constraints

- Do not mention competitors or use comparative words such as "better",
  "unlike", or "more than".
- Describe current behavior, not an aspirational AI capability.
- Keep each card to one short paragraph and no more than three destination links.
- Preserve the distinction between the embedded Agent's provider boundary and
  MCP's tool-specific disclosure.
- Describe MCP follow as optional and fire-and-forget; it does not alter or gate
  tool results.
- Do not imply that Agent or MCP can approve a write.
- Do not claim a universal preview or a shared read snapshot.

## Scope

This change updates only the homepage highlight heading, labels, descriptions,
links, and the exact landing-page assertions that pin them. It does not change
the hero, videos, navigation, CSS layout, JavaScript behavior, guides, or product
functionality.

## Acceptance

- The three labels are exactly `ONE WORKSPACE`, `CHECKABLE EVIDENCE`, and
  `HUMAN AUTHORITY`, in that order.
- The cards reveal the shared-workspace, inspectable-evidence, and
  human-controlled-write model without mentioning another product.
- Agent navigation and optional MCP follow are described without claiming that
  all reads are mirrored.
- Evidence copy preserves citation, snapshot, and disclosure limitations.
- Write copy preserves fresh user approval, operation-specific previews, and
  fail-closed audit behavior.
- The homepage remains below 800 source words and retains exactly three major
  content blocks.
- Existing responsive, accessibility, playback, no-JavaScript, and strict-build
  contracts remain unchanged.
