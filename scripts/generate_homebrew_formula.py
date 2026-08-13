#!/usr/bin/env python3
"""Generate the Homebrew formula for korvid from `uv.lock`.

A tap formula lists every transitive dependency as a `resource` with a URL
and a hash, because `virtualenv_install_with_resources` installs with
`--no-deps`. Maintaining that list by hand drifts silently: a stale hash
is a checksum mismatch on a stranger's machine, not a build failure here.

`uv.lock` already holds that data, already resolves from PyPI only, and is
already guarded by `scripts/check_lock_hosts.py` - so it is generated from
there rather than by querying an index at build time. That also makes the
formula reproducible offline, which matters on a network that cannot
reach `files.pythonhosted.org` at all.

Usage:
    generate_homebrew_formula.py --version 0.1.2 [--lock uv.lock] [-o korvid.rb]

The project's own sdist URL and hash come from PyPI, since the lock names
the local project rather than a published artifact.
"""

from __future__ import annotations

import argparse
import json
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT = "korvid"
#: Shipped by the formula. `[mcp]` is deliberately absent: it puts an HTTP
#: server on the machine, and a convenience channel must not opt a user
#: into that. `[agent]` is included because the agent is the product.
DEFAULT_EXTRAS = ("agent",)
#: Brew builds on macOS and Linux only, so a Windows-only dependency is
#: not merely unnecessary - `pywin32` publishes no sdist, and a formula
#: naming it cannot be fetched.
EXCLUDED_MARKER_PLATFORMS = ("win32",)
PYTHON_FORMULA = "python@3.13"
#: Brew builds every resource from source, so a package with a compiled
#: extension needs its toolchain declared or the install dies mid-way with
#: a message about a missing compiler. Keyed by resource name; the value
#: maps a formula to its `depends_on` suffix.
SYSTEM_DEPENDENCIES: dict[str, dict[str, str]] = {
    # Compiles a Rust extension, and links against OpenSSL.
    "cryptography": {"rust": " => :build", "openssl@3": ""},
    # Compiles its C loader; without libyaml the build falls back to the
    # pure-Python parser, several times slower on every manifest korvid
    # reads.
    "pyyaml": {"libyaml": ""},
}


@dataclass(frozen=True)
class Resource:
    """One `resource` stanza: a source archive and its hash."""

    name: str
    url: str
    sha256: str


def resolve_resources(lock_path: Path, extras: tuple[str, ...] = DEFAULT_EXTRAS) -> list[Resource]:
    """Every package the formula must install, in formula order.

    Walks the runtime closure from the project's dependencies plus the
    named extras. The development group is not a root, so it never
    enters; neither does an extra that was not asked for.

    Args:
        lock_path: Path to `uv.lock`.
        extras: Optional-dependency groups to include.

    Returns:
        Resources sorted by name, excluding the project itself and any
        package that only applies to a platform Homebrew does not build.
    """
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    root = packages[PROJECT]

    pending = [dep["name"] for dep in root.get("dependencies", [])]
    optional = root.get("optional-dependencies", {})
    for extra in extras:
        pending.extend(dep["name"] for dep in optional.get(extra, []))

    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen or name == PROJECT:
            continue
        package = packages.get(name)
        if package is None or _is_excluded(package):
            continue
        seen.add(name)
        pending.extend(dep["name"] for dep in package.get("dependencies", []))

    resources = []
    for name in sorted(seen):
        sdist = packages[name].get("sdist")
        if sdist is None:
            raise ValueError(f"{name} publishes no source archive; the formula cannot build it")
        resources.append(
            Resource(name=name, url=sdist["url"], sha256=_hash(sdist["hash"], name)),
        )
    return resources


def _is_excluded(package: dict[str, object]) -> bool:
    """Whether a package applies only to a platform Homebrew does not build.

    Read from the package's own resolution marker rather than from its
    name, so a future Windows-only dependency needs no edit here.
    """
    marker = package.get("resolution-markers")
    if not isinstance(marker, list) or not marker:
        return False
    return all(
        any(f"sys_platform == '{platform}'" in str(entry) for platform in EXCLUDED_MARKER_PLATFORMS)
        for entry in marker
    )


def _hash(value: str, name: str) -> str:
    if not value.startswith("sha256:"):
        raise ValueError(f"{name}: expected a sha256 hash, got {value!r}")
    return value.removeprefix("sha256:")


def fetch_project_sdist(version: str) -> tuple[str, str]:
    """The published sdist URL and hash for one korvid release.

    The lock names the working tree, not an artifact, so this is the one
    value that has to come from the index.
    """
    url = f"https://pypi.org/pypi/{PROJECT}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:  # https, fixed host
        payload = json.load(response)
    for artifact in payload["urls"]:
        if artifact["packagetype"] == "sdist":
            return artifact["url"], artifact["digests"]["sha256"]
    raise ValueError(f"{PROJECT} {version} publishes no source archive")


def render_formula(*, version: str, url: str, sha256: str, resources: list[Resource]) -> str:
    """Render `korvid.rb`.

    `virtualenv_install_with_resources` builds against brew's own Python,
    which is the entire point of the tap: korvid needs 3.11+, and macOS
    ships 3.9.
    """
    # brew scans the version from the URL; setting it as well is flagged
    # as redundant and can disagree with the artifact.
    #
    # brew requires the stanzas in alphabetical order by formula name, so
    # the Python formula is sorted in with the rest rather than written
    # first.
    required = {PYTHON_FORMULA: ""}
    for resource in resources:
        required.update(SYSTEM_DEPENDENCIES.get(resource.name, {}))
    # brew orders build-time dependencies ahead of runtime ones, and
    # sorts alphabetically within each group.
    depends_on = "".join(
        f'  depends_on "{name}"{suffix}\n'
        for name, suffix in sorted(required.items(), key=lambda item: (not item[1], item[0]))
    )
    stanzas = "\n".join(
        f'  resource "{resource.name}" do\n'
        f'    url "{resource.url}"\n'
        f'    sha256 "{resource.sha256}"\n'
        f"  end\n"
        for resource in resources
    )
    return f'''# typed: false
# frozen_string_literal: true

# Generated by scripts/generate_homebrew_formula.py in hellices/korvid.
# Do not edit by hand: the resource list is derived from that repository's
# uv.lock, which is what the release was tested against.
class Korvid < Formula
  include Language::Python::Virtualenv

  desc "AI-native Kubernetes TUI"
  homepage "https://github.com/hellices/korvid"
  url "{url}"
  sha256 "{sha256}"
  license "MIT"

{depends_on}
{stanzas}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "{version}", shell_output("#{{bin}}/korvid --version")
  end
end
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="published korvid version")
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("-o", "--output", type=Path, default=Path("korvid.rb"))
    args = parser.parse_args(argv)

    url, sha256 = fetch_project_sdist(args.version)
    formula = render_formula(
        version=args.version,
        url=url,
        sha256=sha256,
        resources=resolve_resources(args.lock),
    )
    args.output.write_text(formula, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
