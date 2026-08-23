"""Shared test helpers for platform-specific expectations."""

import errno
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

WINDOWS = os.name == "nt"
POSIX = os.name == "posix"


def posix_only(reason: str) -> pytest.MarkDecorator:
    """Skip a test when POSIX-specific behavior is unavailable."""
    return pytest.mark.skipif(not POSIX, reason=reason)


def read_text_utf8(path: Path) -> str:
    """Read a repository text file with an explicit UTF-8 encoding."""
    return path.read_text(encoding="utf-8")


def symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symlink, or skip only when Windows symlink privilege is absent."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        missing_privilege = WINDOWS and (
            getattr(exc, "winerror", None) == 1314 or exc.errno == errno.EPERM
        )
        if missing_privilege:
            pytest.skip(
                "Windows symlink creation requires Developer Mode or an elevated"
                " administrator shell"
            )
        raise


def _matching_action_uses(values: object, action: str) -> Iterator[Mapping[object, object]]:
    if not isinstance(values, list):
        return
    for value in values:
        if not isinstance(value, Mapping):
            continue
        uses = value.get("uses")
        if isinstance(uses, str) and uses.partition("@")[0].casefold() == action.casefold():
            yield value


def _job_action_uses(
    job: Mapping[object, object], action: str
) -> Iterator[Mapping[object, object]]:
    yield from _matching_action_uses([job], action)
    yield from _matching_action_uses(job.get("steps"), action)


def _action_uses(workflow: object, action: str) -> Iterator[Mapping[object, object]]:
    if isinstance(workflow, list):
        yield from _matching_action_uses(workflow, action)
        return
    if not isinstance(workflow, Mapping):
        return
    jobs = workflow.get("jobs")
    if not isinstance(jobs, Mapping):
        yield from _job_action_uses(workflow, action)
        return
    for job in jobs.values():
        if not isinstance(job, Mapping):
            continue
        yield from _job_action_uses(job, action)


def _assert_pinned_refs(uses: tuple[Mapping[object, object], ...], action: str) -> tuple[str, ...]:
    refs = tuple(str(use["uses"]).partition("@")[2] for use in uses)
    expected = f"expected {action}@<40 lowercase hex characters>"
    assert refs, expected
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), expected
    return refs


def find_action_refs(workflow_text: str, action: str) -> tuple[str, ...]:
    """Return revisions from structurally matched action use-sites."""
    uses = _action_uses(yaml.safe_load(workflow_text), action)
    return tuple(str(use["uses"]).partition("@")[2] for use in uses)


def assert_pinned_action_refs(workflow_text: str, action: str) -> tuple[str, ...]:
    """Require action use-sites pinned to lowercase full commit SHAs."""
    uses = tuple(_action_uses(yaml.safe_load(workflow_text), action))
    return _assert_pinned_refs(uses, action)


def assert_pinned_action_version(workflow_text: str, action: str, version: str) -> tuple[str, ...]:
    """Require each pinned action use-site to select the expected tool version."""
    uses = tuple(_action_uses(yaml.safe_load(workflow_text), action))
    refs = _assert_pinned_refs(uses, action)
    expected = f"expected every {action} step to use version {version}"
    for use in uses:
        options = use.get("with")
        assert isinstance(options, Mapping), expected
        assert options.get("version") == version, expected
    return refs


def assert_pinned_action_ref(workflow_text: str, action: str) -> str:
    """Require an action use-site to be pinned to a lowercase full commit SHA."""
    return assert_pinned_action_refs(workflow_text, action)[0]
