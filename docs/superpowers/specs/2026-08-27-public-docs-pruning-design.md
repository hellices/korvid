# Public documentation pruning

**Date:** 2026-08-27  
**Status:** Approved from prior product-site direction; user unavailable for a new scope choice  
**Goal:** Make the official site read like a visual product guide, not the repository handbook.

## Problem

The latest upstream merge restored internal reference material to public pages.
The result contradicts the accepted direction:

- `dev/quality-gates.md` is still in navigation and builds as a public route.
- `agent.md` and `performance.md` exceed 4,000 words.
- `threat-model.md`, `provider-plugins.md`, and `ops.md` read as exhaustive
  implementation references rather than product guidance.

Recent review rounds improved correctness, media safety, and CI portability,
but those changes did not satisfy the public-site readability requirement.

## Options considered

1. **Prune every public page by role — selected.** Keep the landing experience
   and short guides, remove internal-only routes, and reduce long pages to the
   decisions an operator needs. This fixes the information architecture rather
   than only the two worst pages.
2. **Trim only Agent and Performance.** Faster, but leaves Threat model,
   Provider plugins, and Operations visibly inconsistent.
3. **Keep long references behind collapsible sections.** Preserves all text but
   still sends the browser, search index, and maintainers through the same
   exhaustive content.

## Public-site boundary

- Exclude `dev/quality-gates.md` from the MkDocs build and navigation.
- Keep the contributor source in the repository. Contributor docs may link to
  its GitHub source, not to a generated site route.
- Keep Architecture and Contributor docs in Project navigation; they explain
  how to participate without exposing every local/CI check as product content.
- Preserve release notes.

## Page treatment

Pages are edited according to their subject, not forced into a common template.

- **Agent:** retain the visual walkthrough, turn flow, approval boundary,
  provider choices, payload boundary, follow/interrupt behavior, and a compact
  model-tier summary. Link to eval/provider/air-gap references instead of
  embedding their procedures.
- **Performance:** retain the supported envelope, the measured visual/table,
  operator implications, known limits, and links to raw artifacts. Remove
  benchmark implementation logs and qualification procedure narration.
- **Threat model:** retain trust boundaries, what is protected, residual risks,
  and safe deployment choices. Remove control-by-control implementation
  inventory already enforced by code/tests.
- **Provider plugins:** retain when a plugin is appropriate, the minimal public
  API 2 surface, a compact adapter example, trust warning, and lifecycle link.
- **Operations:** retain the approval/audit diagram, representative operations,
  readonly/protected contexts, and operation-specific evidence table. Remove
  repeated explanations of the same immutable Agent safety perimeter.
- **Overview:** remove repeated feature prose already shown on the landing and
  destination guides.

Existing useful screenshots, video, SVG, Mermaid, storyboards, and compact
tables remain. No media is regenerated.

## Acceptance

- `/korvid/dev/quality-gates/` is not generated.
- No `Quality gates` item appears in site navigation or search output.
- The public Agent and Performance pages are each below 2,000 words.
- No other product guide exceeds 2,200 words without being an explicitly
  labeled technical reference.
- All retained claims match current product behavior and existing links resolve.
- Strict MkDocs build, documentation contracts, and local route checks pass.
- The local preview visibly updates without changing the landing or Quick Start.
