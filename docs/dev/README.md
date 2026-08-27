# Development documents

Internal documents for people (and agents) working **on** korvid, as
opposed to the user-facing feature docs that live directly under
[`docs/`](../index.md).

**Start here if you are new:**
[`specs/2026-08-12-korvid-architecture.md`](specs/2026-08-12-korvid-architecture.md)
— how the layers fit together, the write path a model cannot bypass, the
provider boundary, and the evidence system, with diagrams. It documents the
system as built (and names the tensions it still has), where the 2026-07-23
design document states the original intent.

- [`specs/`](https://github.com/hellices/korvid/tree/main/docs/dev/specs) — the product design document and the engineering
  standards. These are the durable references: layer rules and the
  security invariants that code must never weaken.
- [`plans/`](https://github.com/hellices/korvid/tree/main/docs/dev/plans) — dated implementation plans for individual phases
  and slices. Historical once executed; kept for traceability, not
  updated retroactively.
- [`contract-tests.md`](contract-tests.md) — the live-cluster contract
  suite and the Korvid test-only AKS infrastructure it runs against.
- [`ui-controllers.md`](ui-controllers.md) — how `ui/app.py` is being
  decomposed (issue #187): what each controller owns, what deliberately
  stays on the app, and why the dependency getters are late-binding. Read
  this before extracting the next area.
- [`agent-decisions.md`](agent-decisions.md) — why the agent is shaped the
  way it is: which capability directions were tried, measured, or
  rejected, and what evidence settled them. Read this before proposing new
  agent capabilities.

If you are looking for how to *use* korvid, start at the
[project README](https://github.com/hellices/korvid/blob/main/README.md).

## Publishing the documentation site

Publishing is a merge, not a deploy script: no server or hosting
infrastructure is provisioned or operated for the site. GitHub Pages serves
the static build produced by
[`.github/workflows/docs.yml`](https://github.com/hellices/korvid/blob/main/.github/workflows/docs.yml).

Before the very first deployment, a repository admin must enable Pages
**once**: **Settings -> Pages -> Build and deployment -> Source: GitHub Actions.**
Until that one-time setting is made, the workflow's build job still
succeeds but the deploy job fails.

After that one-time setup, merging documentation-site changes to `main`
triggers the workflow and publishes <https://hellices.github.io/korvid/>.
Pull request builds validate the site (strict build, link checks) but do
not deploy it — only a push to `main` runs the deploy job.

A custom domain is optional and deliberately deferred. Adopting one later does
not require a content migration, but it does require updating `site_url`,
Pages settings, DNS, the optional `CNAME`, hosted links in `README.md`,
`pyproject.toml`'s Documentation URL, and the entry-point tests that pin those
canonical links.
