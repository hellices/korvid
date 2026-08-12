"""Where korvid's dependencies come from is a supply-chain property.

`uv.lock` is guarded by the `no-private-index-in-lock` pre-commit hook, which
rejects a lock whose URLs or registries point somewhere other than PyPI. That
catches the *symptom*.

The cause is configuration, and configuration redirects every resolution -
including CI's - while the lock stays byte-for-byte clean:

* `[tool.uv]` index settings in `pyproject.toml`
* a repository-level `uv.toml`, which takes precedence over `[tool.uv]`

Committing one line of either moves the supply chain for everybody, and
nothing fails until somebody re-locks.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

#: The only hosts the lockfile may name.
_ALLOWED_LOCK_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})

#: `[tool.uv]` keys that point resolution at a different index.
_INDEX_KEYS = ("index-url", "extra-index-url", "index", "find-links")

_ROOT = Path(__file__).parents[1]


def _uv_lock() -> str:
    return (_ROOT / "uv.lock").read_text()


def _lock_hosts(lock: str) -> set[str]:
    """Every host the lock names, from any key that can carry one."""
    urls = re.findall(r'\b(?:url|registry|index)\s*=\s*"(https?://[^"]+)"', lock)
    return {host for url in urls if (host := urlsplit(url).hostname) is not None}


def test_lockfile_names_no_host_other_than_pypi() -> None:
    """The hook enforces this on commit; this fails the build as well.

    A test carries the recovery in its message and does not depend on the
    committer having installed the hooks.
    """
    hosts = _lock_hosts(_uv_lock())
    foreign = sorted(hosts - _ALLOWED_LOCK_HOSTS)
    assert not foreign, (
        f"uv.lock resolves through {foreign}, not {sorted(_ALLOWED_LOCK_HOSTS)}."
        " Re-locking behind a corporate index rewrites every URL and registry:"
        " restore it with `git checkout uv.lock` and re-lock without the mirror."
    )
    assert hosts, "uv.lock names no hosts at all; the guard is matching nothing"


def test_lockfile_records_both_the_artifact_url_and_the_serving_registry() -> None:
    """The guard is only as good as the keys it knows about.

    `url` alone would leave the `source = { registry = ... }` entries - the
    ones recording *which index served the package* - unguarded. Asserting
    both shapes still exist means a uv format change fails loudly instead of
    quietly shrinking the guard to whatever still matches.
    """
    lock = _uv_lock()
    assert re.search(r'\burl\s*=\s*"https://files\.pythonhosted\.org/', lock)
    assert re.search(r'\bregistry\s*=\s*"https://pypi\.org/simple"', lock)


def test_a_relock_path_exists_for_machines_that_cannot_reach_pypi() -> None:
    """The guards above have to leave a way forward.

    `uv lock` fetches wheel metadata from `files.pythonhosted.org`, so a
    TLS-intercepted machine cannot produce an acceptable lock at all — it
    can only produce one this repository rejects. Guarding the lock without
    providing a route to update it would make a legitimate dependency change
    impossible rather than careful.
    """
    workflow = (_ROOT / ".github" / "workflows" / "relock.yml").read_text()
    assert "uv lock --upgrade-package" in workflow
    assert "gh pr create" in workflow, "the lock must arrive through review, not a push to main"


def test_the_relock_workflow_verifies_the_lock_it_produces() -> None:
    """A lock nobody can regenerate locally is exactly the one to check.

    The runner should resolve from PyPI, so this never fires — which is the
    point: an unverified lock produced by automation is worth less than
    none, because no human can reproduce it to disagree.
    """
    workflow = (_ROOT / ".github" / "workflows" / "relock.yml").read_text()
    assert "files\\.pythonhosted\\.org|pypi\\.org" in workflow
    assert "uv sync --locked" in workflow
    assert "uv run pytest" in workflow


def test_pyproject_pins_no_alternate_package_index() -> None:
    """An index pinned here redirects every resolution in the repository.

    The lock would still look clean, so the hook that guards it stays green
    while CI installs from somewhere nobody reviewed.
    """
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    uv_config = pyproject.get("tool", {}).get("uv", {})
    for key in _INDEX_KEYS:
        assert key not in uv_config, (
            f"pyproject.toml pins a package index via [tool.uv] {key};"
            " korvid resolves from PyPI and nowhere else"
        )


def test_repository_declares_no_uv_configuration_file() -> None:
    """`uv.toml` overrides `[tool.uv]`, so guarding pyproject alone is not enough.

    If one is ever needed, this test is where to state which settings it may
    carry - rather than deleting the assertion.
    """
    assert not (_ROOT / "uv.toml").exists(), (
        "uv.toml overrides [tool.uv] in pyproject.toml; if it is introduced,"
        " assert here that it pins no alternate index"
    )
