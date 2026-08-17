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

import importlib.util
import re
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from tests.release_contracts import run_scripts, workflow_jobs

#: The only hosts the lockfile may name.
_ALLOWED_LOCK_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})

#: `[tool.uv]` keys that point resolution at a different index.
_INDEX_KEYS = ("index-url", "extra-index-url", "index", "find-links")

_ROOT = Path(__file__).parents[1]
_RELOCK_WORKFLOW = _ROOT / ".github" / "workflows" / "relock.yml"


def _uv_lock() -> str:
    return (_ROOT / "uv.lock").read_text()


def test_workflow_jobs_returns_jobs_mapping_for_relock_workflow() -> None:
    jobs = workflow_jobs(_RELOCK_WORKFLOW)
    assert {"relock", "propose"} <= jobs.keys()


@pytest.mark.parametrize("contents", ["", "[]\n"])
def test_workflow_jobs_rejects_empty_or_non_mapping_yaml_root(
    tmp_path: Path, contents: str
) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(contents)

    with pytest.raises(AssertionError, match="YAML mapping at the document root"):
        workflow_jobs(workflow)


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
    jobs = workflow_jobs(_RELOCK_WORKFLOW)
    assert any("uv lock --upgrade-package" in script for script in run_scripts(jobs["relock"]))
    assert any("gh pr create" in script for script in run_scripts(jobs["propose"])), (
        "the lock must arrive through review, not a push to main"
    )


def test_the_relock_workflow_verifies_the_lock_it_produces() -> None:
    """A lock nobody can regenerate locally is exactly the one to check.

    It also has to be checked *here*. GitHub suppresses the workflow events
    raised by `GITHUB_TOKEN`, so the pull request this opens does not start
    CI — whatever the job skips is not checked at all before a human reads
    it. The gate below is therefore the full one, not just the tests.
    """
    scripts = "\n".join(run_scripts(workflow_jobs(_RELOCK_WORKFLOW)["relock"]))
    assert "scripts/check_lock_hosts.py" in scripts
    assert "uv sync --locked" in scripts
    for step in ("ruff check", "ruff format --check", "mypy src/", "tach check", "pytest"):
        assert step in scripts, f"the relock job skips {step}; CI will not run it either"


def test_the_relock_pull_request_says_ci_has_not_run() -> None:
    """Claiming "goes through the same CI" would be a comfortable lie.

    A reviewer who believes the checks ran is worse off than one told they
    did not, so the pull request body has to say it plainly and name the
    remedy.
    """
    propose = workflow_jobs(_RELOCK_WORKFLOW)["propose"]["steps"]
    script = next(step["run"] for step in propose if "gh pr create" in str(step.get("run", "")))
    assert "CI has not run on this branch" in script
    assert "empty commit" in script


def test_the_relock_job_holds_no_write_credential_while_it_runs_the_code() -> None:
    """The job that executes the new dependencies cannot write anything.

    Delaying authentication within one job is not enough: a dependency or a
    test can leave an executable `.git/hooks/pre-push` behind, and that hook
    runs later with the token in scope. So generation and verification are
    read-only, and the push happens in a separate job from a clean checkout
    that carries nothing across but `uv.lock` itself.
    """
    jobs = workflow_jobs(_RELOCK_WORKFLOW)
    assert jobs["relock"]["permissions"] == {"contents": "read"}
    assert jobs["propose"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    relock_steps = run_scripts(jobs["relock"])
    for command in ("uv lock", "uv sync --locked", "uv run pytest"):
        assert any(command in script for script in relock_steps), (
            f"{command} must run in the read-only job"
        )
    assert not any("uv run pytest" in script for script in run_scripts(jobs["propose"]))
    propose_steps = jobs["propose"]["steps"]
    for step in propose_steps:
        script = step.get("run", "")
        if not script:
            continue
        # Every line, not an indentation-filtered subset: YAML strips a
        # block scalar's common indent, so filtering on leading spaces
        # matched nothing at all and this assertion passed vacuously.
        # Continuation lines inside the pull-request body are excluded by
        # name instead, since that text legitimately mentions `uv sync`.
        for line in script.splitlines():
            command = line.strip()
            if command.startswith(("uv ", "python -m ", "pytest")):
                assert step.get("name", "").startswith("Refuse"), (
                    f"the write-token job executes {command!r} in step {step.get('name')!r}"
                )
    checkouts = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)


def test_the_lock_is_revalidated_after_the_code_that_could_rewrite_it_ran() -> None:
    """Validating before `pytest` leaves a window.

    The read-only job checks the lock, then installs and runs the very
    dependencies it just resolved — any of which could rewrite `uv.lock`
    before the upload. The check and the upload are not the same instant, so
    the file is checked again in the clean job, in a workspace that ran none
    of that code, and before `GH_TOKEN` is exposed.
    """
    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "relock.yml").read_text())
    steps = workflow["jobs"]["propose"]["steps"]
    names = [step.get("name", "") for step in steps]
    guard = next(i for i, name in enumerate(names) if "anything but PyPI" in name)
    exposed = next(i for i, step in enumerate(steps) if "GH_TOKEN" in str(step.get("env", {})))
    assert guard < exposed, "the write token is exposed before the lock is re-checked"


def test_the_committed_bytes_are_the_bytes_that_were_verified() -> None:
    """A PyPI-only lock is not necessarily *this* PyPI-only lock.

    The hostname check alone would accept a different, valid, PyPI-only
    file substituted by code running during the gate — the shape would pass
    while the bytes nobody verified got committed. The digest is taken
    before any resolved code runs and checked on both sides of the artifact
    boundary.
    """
    jobs = workflow_jobs(_RELOCK_WORKFLOW)
    relock, propose = jobs["relock"], jobs["propose"]
    assert "digest" in relock["outputs"], "the verified digest is never published"

    names = [step.get("name", "") for step in relock["steps"]]
    recorded = next(i for i, name in enumerate(names) if "digest" in name)
    gate = next(i for i, name in enumerate(names) if "passes the gate" in name)
    assert recorded < gate, "the digest is taken after the code that could rewrite the lock"

    relock_scripts = "\n".join(run_scripts(relock))
    assert "sha256sum --check --strict" in relock_scripts, (
        "the producing job never re-checks the lock it verified"
    )
    propose_scripts = "\n".join(run_scripts(propose))
    assert "sha256sum --check --strict" in propose_scripts, (
        "the committing job trusts the artifact's shape but not its identity"
    )


def test_the_validator_comes_from_a_revision_that_is_not_under_review() -> None:
    """A branch must not supply the check that clears it.

    `inputs.base` is the thing being evaluated. Running its copy of
    `check_lock_hosts.py` lets a branch ship a no-op validator beside an
    index redirect, after which the digest chain faithfully carries an
    unvalidated lock — every downstream check passes because the check
    itself was replaced. The trust anchor has to sit outside the change.
    """
    checkouts = [
        step["with"]
        for step in workflow_jobs(_RELOCK_WORKFLOW)["propose"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, "the committing job never checks anything out"
    for options in checkouts:
        assert options["ref"] == "main", (
            "the committing job takes the validator from the branch under review"
        )


def test_the_lock_is_committed_onto_the_revision_it_was_tested_against() -> None:
    """A branch name is not a revision.

    The lock is resolved and exercised against the commit `relock` checked
    out. Re-resolving the branch name afterwards can land it on a newer tip,
    pairing an untested source revision with a lock generated for the old
    one — while the pull request reports that the suite passed.
    """
    jobs = workflow_jobs(_RELOCK_WORKFLOW)
    assert "base_sha" in jobs["relock"]["outputs"], "the tested revision is never published"
    propose = jobs["propose"]["steps"]
    commit_step = next(step for step in propose if "gh pr create" in step.get("run", ""))
    assert "BASE_SHA" in str(commit_step.get("env", {}))
    assert 'git checkout -b "$branch" "$BASE_SHA"' in commit_step["run"], (
        "the commit is branched from a moving ref rather than the tested revision"
    )


def test_the_relock_guard_fails_closed_on_anything_it_cannot_read() -> None:
    """A text match over an untrusted file reports success on nonsense.

    The job that produces the lock also runs the dependencies it resolved,
    so the checker is handed a file it cannot trust. `registry="https://…"`
    without spaces is valid TOML and matches no line pattern; an empty file
    matches nothing at all. A grep-based check called both of those clean.
    """
    checker = _ROOT / "scripts" / "check_lock_hosts.py"
    spec = importlib.util.spec_from_file_location("check_lock_hosts", checker)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        compact = root / "compact.toml"
        compact.write_text('registry="https://evil.example/simple"\n')
        assert module.check(compact), "compact TOML slipped through"

        empty = root / "empty.toml"
        empty.write_text("")
        assert module.check(empty), "an empty file was treated as verified"

        malformed = root / "bad.toml"
        malformed.write_text("not toml [[[\n")
        assert module.check(malformed), "unparsable input was treated as verified"

        plain = root / "plain.toml"
        plain.write_text('url = "http://files.pythonhosted.org/x.whl"\n')
        assert module.check(plain), "an http downgrade was accepted"

        # A lock can be entirely PyPI apart from one VCS dependency, whose
        # remote is recorded under `git` rather than `url` or `registry`.
        vcs = root / "vcs.toml"
        vcs.write_text(
            '[[package]]\nname = "a"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            '[[package]]\nname = "b"\n'
            'source = { git = "https://git.evil.example/pkg.git" }\n'
        )
        assert module.check(vcs), "a git dependency from an arbitrary host was accepted"

    assert not module.check(_ROOT / "uv.lock"), "the committed lock must pass"


def test_the_relock_workflow_uses_the_parsing_guard_in_both_jobs() -> None:
    jobs = workflow_jobs(_RELOCK_WORKFLOW)
    for job in ("relock", "propose"):
        assert any("scripts/check_lock_hosts.py" in script for script in run_scripts(jobs[job])), (
            f"{job} does not verify the lock"
        )


def test_the_lock_can_actually_be_staged_from_the_sparse_checkout() -> None:
    """The trust anchor and the file being committed pull in opposite ways.

    Taking the validator from `main` means a sparse checkout containing only
    `scripts/check_lock_hosts.py` — and `uv.lock` then sits outside the
    sparse definition, where plain `git add` refuses to stage it. Every run
    with a changed lock would fail at the last step, after doing all the
    work. Reproduced locally before fixing.
    """
    steps = workflow_jobs(_RELOCK_WORKFLOW)["propose"]["steps"]
    sparse = any("sparse-checkout" in str(step.get("with", {})) for step in steps)
    commit = next(step for step in steps if "gh pr create" in step.get("run", ""))
    if sparse:
        assert "git add --sparse uv.lock" in commit["run"], (
            "a sparse checkout cannot stage uv.lock without --sparse"
        )


def test_the_relock_pull_request_does_not_promise_codeql_it_cannot_deliver() -> None:
    """CodeQL runs only for pull requests targeting `main`.

    The workflow accepts any `base`, so telling a reviewer that an empty
    commit brings CodeQL is false for every other base — and a false
    remedy is worse than none, because it stops them looking.
    """
    codeql = yaml.safe_load((_ROOT / ".github" / "workflows" / "codeql.yml").read_text())
    # `on` parses as the boolean True in YAML 1.1.
    triggers = codeql.get("on", codeql.get(True))
    assert triggers["pull_request"]["branches"] == ["main"], (
        "CodeQL's trigger changed; the relock wording may now be wrong in the other direction"
    )
    propose = workflow_jobs(_RELOCK_WORKFLOW)["propose"]["steps"]
    script = next(step["run"] for step in propose if "gh pr create" in str(step.get("run", "")))
    assert "CodeQL only runs for pull requests targeting" in script


def test_the_relock_workflow_survives_a_repository_that_forbids_bot_pull_requests() -> None:
    """`gh pr create` fails when Actions may not open pull requests.

    Observed on the first real dispatch: the branch pushed and the lock was
    verified, then `createPullRequest` was refused by a repository setting
    the API cannot even read. Exiting there would discard verified work for
    a reason nobody can fix from inside the job, so it reports the setting
    and the one command that finishes the job by hand.
    """
    propose = workflow_jobs(_RELOCK_WORKFLOW)["propose"]["steps"]
    commit_step = next(step for step in propose if "gh pr create" in str(step.get("run", "")))
    script = str(commit_step["run"])
    assert "if ! gh pr create" in script, "a refused pull request aborts without explanation"
    # Assert against the branch that actually runs, not the whole file: a
    # comment mentioning the setting would otherwise satisfy this while the
    # job died silently.
    _, _, after = script.partition("if ! gh pr create")
    failure_branch, _, _ = after.partition("exit 1")
    printed = "\n".join(
        line
        for line in failure_branch.splitlines()
        if line.strip().startswith(("echo ", "printf "))
    )
    assert "Allow GitHub Actions to create and" in printed, (
        "the operator is not told which setting refused the pull request"
    )
    assert "Nothing is lost" in printed
    # Isolate the copyable command. Asserting `$branch` anywhere in the
    # failure branch is satisfied by the status message above it, so a
    # remedy naming the wrong head would still pass.
    remedy = next((line for line in printed.splitlines() if "gh pr create --base" in line), None)
    assert remedy is not None, "the manual remedy is not printed"
    assert "$BASE" in remedy, "the remedy must name the base the run actually used"
    assert "$branch" in remedy, "the remedy must name the branch the run actually pushed"
    # The claim is only true if the branch is already on the remote when
    # creation is attempted. Reordering these would leave the message
    # intact and the work lost.
    assert script.index('git push origin "$branch"') < script.index("if ! gh pr create"), (
        "the branch must be pushed before the pull request can fail"
    )
    # The line gets pasted into a shell, so it must be escaped rather than
    # interpolated: git allows ';' in a ref name, and $BASE is a dispatch
    # input.
    assert "--base $BASE" not in remedy, (
        "an unescaped base ref lets a branch name run extra shell commands"
    )
    assert remedy.strip().startswith("printf "), "render the remedy with shell escaping"
    assert remedy.count("%q") == 2, "both the base and the branch must be escaped"


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
