# Official documentation site

## Goal

Publish the existing korvid user and contributor documentation as a searchable,
version-controlled static site without creating a second documentation source.
The site becomes the canonical documentation entry point while the README stays
focused on product positioning, installation, and a short quick start.

The first release of the site must:

- render the Markdown already maintained under `docs/`;
- give users a clear path from evaluation to installation and operation;
- keep contributor-only material visibly separate from user guidance;
- validate links and site generation in CI;
- deploy from `main` without credentials stored in the repository; and
- remain inexpensive to maintain while korvid is still evolving quickly.

Custom application code, a documentation backend, analytics, localization,
version switching, and a blog are outside the initial scope.

## Decision

Use MkDocs with the Material theme and GitHub Pages.

MkDocs fits the repository's Python toolchain, consumes the existing Markdown
directly, supports Mermaid diagrams and built-in client-side search, and
requires only a small configuration file. Material provides usable navigation,
responsive layout, code copying, and dark mode without a custom front end.
GitHub Pages provides static hosting behind the repository's existing GitHub
trust boundary.

Alternatives considered:

- **Docusaurus:** stronger built-in versioning and React extensibility, but it
  introduces a Node toolchain and more application surface than the current
  documentation needs.
- **Sphinx:** strong Python API-reference tooling, but korvid is an application
  rather than a public Python library and the existing user documentation is
  already plain Markdown rather than reStructuredText.

Versioned documentation can be added later if incompatible user-facing
behavior exists across multiple supported releases. It is not justified for
the first public documentation site.

## Information architecture

The landing page is `docs/index.md`, a product-led page inspired by the direct,
playful structure of the K9s official site. It is not a generic documentation
index. A concise hero, one-command install, the existing TUI demo, and a small
set of capability sections explain the product before directing readers into
the documentation.

The page tells a deliberate story:

1. korvid is a keyboard-first cockpit for a Kubernetes cluster;
2. the TUI is useful without AI;
3. optional agent and MCP surfaces add diagnosis and external integration;
4. writes remain human-approved and fail-closed audited; and
5. the reader can install immediately or enter the task-oriented guides.

Primary navigation is organized by user intent:

1. **Getting started**
   - Overview
   - Installation and quick start
   - Keybindings
2. **Operate**
   - TUI
   - Operations and safety
   - Resource relationships
   - Helm and operators
   - Observability
3. **AI and integrations**
   - Embedded agent
   - MCP server
   - Provider plugins
4. **Administration**
   - Air-gapped operation
   - Performance and scale
   - Threat model
5. **Project**
   - Release notes
   - Contributor documentation

Historical implementation plans and superseded design documents remain in the
repository for traceability but are excluded from site navigation. Current
architecture, engineering standards, quality gates, and contributor guidance
remain reachable under the contributor section.

## Content ownership and links

Files under `docs/` remain the canonical content. The site configuration maps
them into navigation; it does not copy their prose into generated files.
`README.md` links to the deployed documentation URL, and the package metadata
uses the same URL for its `Documentation` project link.

Repository-relative Markdown links must render both on GitHub and in MkDocs.
Absolute GitHub links are retained where they deliberately target repository
artifacts or release assets. Site-internal links should be relative so local
preview and pull-request builds do not depend on production hosting.

Generated HTML is never committed. The Pages workflow builds it from the
reviewed source revision and uploads the static artifact.

## Build and deployment

Documentation dependencies live in a dedicated `docs` dependency group in
`pyproject.toml`. This keeps application installations unchanged while making
local and CI commands reproducible through uv.

The repository exposes:

```sh
uv sync --frozen --group docs
uv run --group docs mkdocs serve
uv run --group docs mkdocs build --strict
```

`mkdocs build --strict` is the documentation quality gate. Warnings such as a
missing navigation target, invalid site configuration, or unresolved internal
link fail the build.

A dedicated GitHub Pages workflow:

- runs for pull requests that change documentation-site inputs and builds the
  site without deployment;
- runs for pushes to `main` and deploys the built artifact;
- uses pinned GitHub Actions;
- grants read-only permissions to the build job and Pages-specific permissions
  only to the deployment job;
- uses GitHub's Pages environment and concurrency controls; and
- never executes untrusted documentation code or needs a repository secret.

The existing CI remains responsible for documentation-to-code contract tests.
The Pages workflow adds rendering and link/configuration validation rather than
replacing those checks.

## Presentation

The site borrows K9s's product-site qualities without copying its branding or
content: a memorable opening line, prominent terminal product imagery, short
feature statements, and documentation that feels like part of the product
rather than a separate portal.

Korvid uses its own name, “A tool-using bird for your cluster” tagline, and
existing demo asset. The visual system is a dark terminal-oriented palette with
warm amber accents, off-white text, monospace details, generous spacing, and
subtle panel borders. The hero includes the install command and two clear
actions: start the guide or view the project on GitHub.

Below the hero:

- a three-part product model presents cockpit, embedded agent, and MCP;
- a safety section makes approval, masking, and fail-closed auditing visible
  before users have to search for the threat model;
- the demo is shown at a readable width rather than treated as decoration; and
- a compact pathfinder sends operators, AI users, and contributors to the right
  guide.

Theme customization is limited to MkDocs template extension, configuration,
and a focused stylesheet. The initial site does not add a custom JavaScript
application bundle. Documentation pages retain conventional readable layouts
and navigation even though the landing page is more expressive.

Mermaid diagrams render through a pinned client-side Mermaid asset configured
by MkDocs. The site must preserve the architecture diagrams already embedded in
the overview and architecture documents.

## Failure handling

A failed strict build blocks deployment. A failed Pages deployment leaves the
previously published site intact. Pull requests must be able to prove the site
build without requiring Pages write permissions.

External links are not treated as a hard build-time dependency in the first
release because network failures would make documentation publication
non-deterministic. Internal links and navigation targets are validated
deterministically by MkDocs.

## Verification

- Build the site locally with `mkdocs build --strict`.
- Confirm every configured navigation target exists.
- Confirm Mermaid diagrams render in a browser.
- Confirm the landing page and navigation work at desktop and narrow viewport
  widths.
- Confirm search returns a known page and heading.
- Confirm the generated site contains no draft plans in primary navigation.
- Confirm README and package metadata point to the official documentation URL.
- Validate the workflow with the repository's existing workflow linter and
  pre-commit checks.

## Rollout

The first deployment uses `https://hellices.github.io/korvid/`. A custom domain
is deferred until a domain name and DNS ownership are explicitly chosen;
adopting one later requires only `site_url`, Pages settings, DNS, and an
optional `CNAME`, not a content migration.
