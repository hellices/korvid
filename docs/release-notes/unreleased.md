# Unreleased (main)

This page tracks changes on `main` after the v0.7.1 release candidate.

The [latest published release](https://github.com/hellices/korvid/releases/latest)
remains the stable install until v0.7.1's publication is verified. See the
[v0.7.1 release notes](v0.7.1.md) for the candidate summary. The
[current release milestone, v0.8.0](https://github.com/hellices/korvid/milestone/10)
tracks the next release scope.

## Current configuration and extension contracts

Existing migration links continue in the
[v0.5.0 migration notes](v0.5.0.md#breaking-changes-and-migration).

## Dependency updates

The `[agent]` extra now pins LiteLLM 1.109.1, up from 1.100.5, so dependency
scanners stop flagging the older release. LiteLLM 1.104.0 removed
`litellm.telemetry`, a setting that no SDK code ever read, so korvid now sets
seven settings when it loads LiteLLM, not eight. A korvid build that pins
1.100.5 fails to start its agent when a newer LiteLLM is installed into the
same environment. A stream that ends before the provider finishes the answer
is still reported as truncated, not as an unavailable provider.
