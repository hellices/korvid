# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities **privately** through GitHub's Security
Advisory flow — never in a public issue, discussion, or pull request:

1. Go to the repository's **Security** tab.
2. Choose **"Report a vulnerability"** to open a private advisory draft.

Include, when known:

- the affected version or commit;
- a reproduction (steps, minimal config, or a fixture);
- the impact (what an attacker gains, what data or action is exposed);
- a suggested mitigation or fix, if you have one.

### Response targets

- **Acknowledgment:** within 3 business days of the report.
- **Status update:** within 7 calendar days, including either a fix timeline
  or an explicit risk decision.

These are targets, not contractual SLAs — korvid is maintained on a
best-effort basis.

## Supported versions

Before the first public `v0.1.2` release, there is **no supported release**:
`v0.1.0` and `v0.1.1` are immutable, unpublished audit history, and `main` is
development-only and may contain unfixed issues. Once `v0.1.2` publishes, the
latest `0.1.x` patch release is the supported line; `main` remains
development-only and is not a supported target for security fixes. This policy
will be revised as the release cadence matures past `0.1.x`.

## Coordinated disclosure

Once a report is triaged, disclosure (a public advisory, changelog entry, or
issue) is coordinated with the reporter and happens **after** a fix is
released, or after an explicit decision that the report is not a
vulnerability, won't be fixed, or requires accepting the residual risk. We
will credit reporters who want to be credited.

## Scope

For the specific data korvid sends to embedded AI providers, what stays
local, and the trust boundaries around the MCP server and provider plugins,
see [`docs/threat-model.md`](docs/threat-model.md).
