# PEP 668-safe installation guidance

**Issue:** #302
**Status:** Approved for implementation
**Scope:** Public installation guidance, runtime missing-extra hints, and their
release-policy tests

## Problem

korvid is a CLI application, but parts of the README still present
`python -m pip install ...` as the general installation command. On Debian,
Ubuntu, and other distributions that mark their system interpreter as
externally managed, that command fails before korvid is resolved:

```text
error: externally-managed-environment
```

This is PEP 668 working as intended. The interpreter's package set belongs to
the operating-system package manager, so pip refuses to mutate it. The failure
is not caused by korvid's wheel, extras, or Python-version support.

The README quick start already recommends `uv tool install` or `pipx install`,
but later installation, upgrade, source-install, and artifact-verification
examples return to unqualified pip commands. Readers can reasonably copy those
commands without seeing the earlier virtual-environment caveat.

## Goals

- Make isolated application installation the consistent public path.
- Give users who hit PEP 668 an immediately actionable recovery command.
- Keep in-app and startup missing-extra hints consistent with that command.
- Keep valid pip workflows for virtual environments, containers, air-gapped
  bundles, and release verification.
- Never recommend `--break-system-packages`.
- Keep release-version and install-documentation contracts machine checked.

## Non-goals

- Adding an installer shell script or bootstrap executable.
- Changing package metadata, dependencies, extras, or build-system pins.
- Changing the release artifact matrix.
- Automating installation of `uv`, `pipx`, or Homebrew.
- Supporting mutation of an operating-system-managed Python environment.

## Considered approaches

### 1. Normalize public documentation around tool installers

Use `uv tool install` as the primary command and `pipx install` as the
equivalent alternative. Keep pip examples only where the text establishes an
isolated interpreter first.

This is the selected approach. It fixes the misleading source, follows the
project's existing packaging design, and adds no runtime or installer code.

### 2. Add a korvid installer wrapper

A shell script could detect PEP 668 and choose an installer. This adds a new
distribution and trust surface, duplicates mature tool managers, and still
needs installation documentation. It is unnecessary for this failure.

### 3. Reply to the issue without changing documentation

An issue reply would unblock one reporter but leave the contradictory commands
that caused the report. It does not prevent recurrence.

## Installation command policy

### End-user application installation

Public examples use:

```sh
uv tool install 'korvid[all]==0.2.0'
# or
pipx install 'korvid[all]==0.2.0'
```

Slim extra combinations use the same tool-install form. The README may show
the requirement strings once and explain that either tool accepts them rather
than duplicating every combination for both tools.

### Upgrade and extra changes

Application upgrades use the tool manager that owns the environment:

```sh
uv tool install --force 'korvid[all]==0.2.0'
# or
pipx install --force 'korvid[all]==0.2.0'
```

`--force` makes the desired extra set explicit and avoids implying that a bare
pip command should modify a tool-managed environment.

### Unreleased source installation

Source installs remain isolated:

```sh
uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'
```

The pipx equivalent may be named in prose or shown alongside it.
Release documentation tests derive this version from `pyproject.toml`, so a
future release cannot retain stale commands.

### Valid pip contexts

Pip remains documented where its environment is explicit:

- an activated virtual environment;
- a container image whose Python environment the image author controls;
- release smoke and upgrade procedures that create a dedicated venv first;
- air-gapped wheelhouse installation into a user-created isolated environment.

The README's pip fallback must state the boundary immediately before the
command, not only in a distant section.

## PEP 668 recovery

The installation section will identify
`error: externally-managed-environment` as a system-Python protection and
direct the user to rerun the install with `uv tool` or `pipx`.

It will explicitly say not to use `--break-system-packages`. That flag defeats
the distribution's ownership boundary and can damage system-managed Python
packages. Users who specifically need pip must create and activate a virtual
environment first.

## Documentation surfaces

The implementation will update:

- `README.md`
  - extras matrix;
  - upgrade/reinstall example;
  - unreleased source example;
  - release artifact install/verification example;
  - PEP 668 troubleshooting next to installation guidance.
- `docs/agent.md` and `docs/observability.md`
  - optional-extra install hints that currently show bare pip.
- `docs/release.md`
  - retain maintainer-only pip commands where a venv is explicit;
  - avoid presenting raw pip as the end-user install path.
- runtime missing-extra hints
  - use one version-aware helper for `uv tool install --force` and `pipx
    install --force`;
  - use `[all]` for the standard feature set and `[all,entra]` for Entra;
  - never direct a tool-managed korvid executable to an unrelated pip
    environment.
- release smoke descriptions
  - describe base-to-extra expansion as a disposable CI-venv pip check;
  - do not claim that it executes the documented end-user tool-manager
    reinstall command.
- release documentation tests
  - require isolated commands in public sections;
  - reject `--break-system-packages`;
  - continue verifying the exact release version and extras.

Historical implementation plans and immutable release notes are not rewritten
unless they are active end-user instructions for the current release.

## Error handling and safety

This change does not intercept installer errors at runtime: korvid is not
running when PEP 668 rejects pip. The safe handling boundary is therefore the
command the documentation asks the user to execute.

No command will silently fall back to a user site or mutate system Python. All
supported public paths create or use a dedicated application environment.

## Verification

Tests will establish these contracts:

1. The README installation section leads with `uv tool` and includes `pipx`.
2. Public extras, upgrade, source, and artifact examples remain isolated.
3. PEP 668's error text and recovery path are documented.
4. `--break-system-packages` appears nowhere in active installation guidance.
5. Pip commands that remain in active docs are adjacent to an explicit venv,
   container, air-gap, or maintainer context.
6. Version references still match `pyproject.toml`.
7. Runtime missing-extra hints contain both isolated tool-manager commands and
   contain no raw `pip install`.
8. Release smoke prose distinguishes its CI-only pip expansion check from the
   documented end-user `--force` reinstall path.

Targeted release-policy and documentation tests run first. The final branch
gate runs ruff, formatting, mypy, pytest, and tach without regenerating
`uv.lock`.

## Issue resolution

After the documentation and contracts land, issue #302 receives a concise
recovery example:

```sh
uv tool install 'korvid[all]==0.2.0'
# or
pipx install 'korvid[all]==0.2.0'
```

The response explains that the reported error comes from PEP 668 and that
`--break-system-packages` is intentionally not recommended.
