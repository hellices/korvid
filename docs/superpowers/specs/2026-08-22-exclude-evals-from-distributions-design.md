# Exclude Evaluation Harness from Distributions

## Goal

Keep Korvid's evaluation harness available to contributors from a source
checkout while excluding it from artifacts installed by end users. Neither the
wheel nor the source distribution may contain `korvid/evals`.

## Design

Add a Hatch build exclusion for the complete `src/korvid/evals` subtree. Apply
the exclusion at the shared build level so it covers both wheel and source
distribution targets without duplicating configuration.

The source tree and development workflow remain unchanged. Contributors can
continue importing and running the evaluation harness from a checkout, while
the installed `korvid` command and runtime package contain only production
code.

## Verification

Add a distribution contract test that builds both artifact types and inspects
their member lists. The test must prove:

- neither artifact contains a path below `korvid/evals`;
- the wheel still contains the production `korvid` package; and
- the source distribution still contains the project metadata needed to build
  the wheel.

Run the targeted contract test and lint the touched files. Building must use the
repository's existing constrained build command so it does not rewrite
`uv.lock` or resolve through an unintended package index.

## Non-goals

- Moving the harness into `tests/`.
- Publishing a separate evaluation package.
- Changing evaluation behavior or production runtime behavior.
- Removing development dependencies used by the harness.
