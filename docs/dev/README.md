# Development documents

Internal documents for people (and agents) working **on** korvid, as
opposed to the user-facing feature docs that live directly under
[`docs/`](../).

- [`specs/`](specs/) — the product design document and the engineering
  standards. These are the durable references: layer rules, quality
  gates, and the security invariants that code must never weaken.
- [`plans/`](plans/) — dated implementation plans for individual phases
  and slices. Historical once executed; kept for traceability, not
  updated retroactively.
- [`contract-tests.md`](contract-tests.md) — the live-cluster contract
  suite and the Korvid test-only AKS infrastructure it runs against.
- [`agent-decisions.md`](agent-decisions.md) — why the agent is shaped the
  way it is: which capability directions were tried, measured, or
  rejected, and what evidence settled them. Read this before proposing new
  agent capabilities.

If you are looking for how to *use* korvid, start at the
[project README](../../README.md).
