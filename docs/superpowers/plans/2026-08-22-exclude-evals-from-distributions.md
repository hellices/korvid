# Exclude Evaluation Harness from Distributions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exclude `src/korvid/evals` and `tests/evals` from Korvid wheels and source distributions while keeping the harness usable from a source checkout.

**Architecture:** Configure Hatch's shared file-selection layer so the rule applies to both artifact targets. Extend the existing release artifact validator to fail closed if a future packaging change reintroduces the harness, and unit-test that contract with synthetic wheel and sdist archives.

**Tech Stack:** Python 3.11+, Hatchling, `tarfile`, `zipfile`, pytest, uv

## Global Constraints

- Neither the wheel nor the source distribution may contain `korvid/evals` or `tests/evals`.
- The source tree and development evaluation workflow remain unchanged.
- The wheel must retain the production `korvid` package.
- The source distribution must retain `pyproject.toml`.
- Build with the repository's hash-pinned build constraints.
- Do not rewrite `uv.lock`.

## File Structure

- `pyproject.toml`: declares the shared Hatch exclusion applied to wheel and sdist.
- `scripts/release/check_artifacts.py`: validates production members and rejects evaluation-harness members in built artifacts.
- `tests/test_release_scripts.py`: constructs representative archives and pins the validator's allow/reject behavior.

---

### Task 1: Enforce the Distribution Content Contract

**Files:**
- Modify: `pyproject.toml:89`
- Modify: `scripts/release/check_artifacts.py:17-33,156-185`
- Test: `tests/test_release_scripts.py:7-17,796-890`

**Interfaces:**
- Consumes: wheel member names from `zipfile.ZipFile.namelist()` and sdist member names from `tarfile.TarFile.getnames()`.
- Produces: `_validate_contents(artifact: Path, members: tuple[str, ...], *, required_suffix: tuple[str, ...]) -> None`.

- [ ] **Step 1: Write the failing artifact-content tests**

Add `io` and extend `_fake_dist` so valid synthetic artifacts contain their required production members and callers can inject forbidden members:

```python
import io


def _fake_dist(
    tmp_path: Path,
    metadata_text: str,
    *,
    wheel_members: tuple[str, ...] = (),
    sdist_members: tuple[str, ...] = (),
) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "korvid-1.2.3-py3-none-any.whl", "w") as wheel:
        wheel.writestr("korvid/__init__.py", "")
        wheel.writestr("korvid-1.2.3.dist-info/METADATA", metadata_text)
        for member in wheel_members:
            wheel.writestr(member, "")
    pkg_info = tmp_path / "PKG-INFO"
    pkg_info.write_text(metadata_text)
    with tarfile.open(dist / "korvid-1.2.3.tar.gz", "w:gz") as sdist:
        sdist.add(pkg_info, arcname="korvid-1.2.3/PKG-INFO")
        for member in ("korvid-1.2.3/pyproject.toml", *sdist_members):
            info = tarfile.TarInfo(member)
            payload = b"" if member != "korvid-1.2.3/pyproject.toml" else b"[build-system]\n"
            info.size = len(payload)
            sdist.addfile(info, io.BytesIO(payload))
    return dist
```

Add a parameterized regression that exercises both artifact types:

```python
@pytest.mark.parametrize(
    ("wheel_members", "sdist_members", "artifact_name"),
    [
        pytest.param(
            ("korvid/evals/runner.py",),
            (),
            "korvid-1.2.3-py3-none-any.whl",
            id="wheel",
        ),
        pytest.param(
            (),
            ("korvid-1.2.3/src/korvid/evals/runner.py",),
            "korvid-1.2.3.tar.gz",
            id="sdist",
        ),
        pytest.param(
            (),
            ("korvid-1.2.3/tests/evals/test_operation.py",),
            "korvid-1.2.3.tar.gz",
            id="sdist-tests-evals",
        ),
    ],
)
def test_artifacts_reject_the_evaluation_harness(
    tmp_path: Path,
    wheel_members: tuple[str, ...],
    sdist_members: tuple[str, ...],
    artifact_name: str,
) -> None:
    dist = _fake_dist(
        tmp_path,
        _metadata_text(),
        wheel_members=wheel_members,
        sdist_members=sdist_members,
    )
    with pytest.raises(
        ValueError,
        match=rf"{re.escape(artifact_name)}: contains development-only evaluation harness",
    ):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])
```

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
uv run pytest -p no:tach \
  tests/test_release_scripts.py::test_artifacts_reject_the_evaluation_harness -q
```

Expected: both cases fail with `Failed: DID NOT RAISE <class 'ValueError'>`.

- [ ] **Step 3: Implement fail-closed artifact content validation**

Add `PurePosixPath` to the imports in `scripts/release/check_artifacts.py`, then add:

```python
def _archive_members(path: Path) -> tuple[str, ...]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            return tuple(wheel.namelist())
    with tarfile.open(path) as sdist:
        return tuple(sdist.getnames())


def _has_contiguous_parts(name: str, expected: tuple[str, ...]) -> bool:
    parts = PurePosixPath(name).parts
    width = len(expected)
    return any(parts[index : index + width] == expected for index in range(len(parts) - width + 1))


def _validate_contents(
    artifact: Path,
    members: tuple[str, ...],
    *,
    required_suffix: tuple[str, ...],
) -> None:
    forbidden_patterns = [("korvid", "evals"), ("tests", "evals")]
    offender = next(
        (
            name
            for name in members
            for forbidden in forbidden_patterns
            if _has_contiguous_parts(name, forbidden)
        ),
        None,
    )
    if offender is not None:
        raise ValueError(
            f"{artifact.name}: contains development-only evaluation harness: {offender}"
        )
    if not any(PurePosixPath(name).parts[-len(required_suffix) :] == required_suffix for name in members):
        required = "/".join(required_suffix)
        raise ValueError(f"{artifact.name}: missing required production member: {required}")
```

In `main`, immediately after locating the single wheel and sdist, validate both member lists:

```python
    _validate_contents(
        wheels[0],
        _archive_members(wheels[0]),
        required_suffix=("korvid", "__init__.py"),
    )
    _validate_contents(
        sdists[0],
        _archive_members(sdists[0]),
        required_suffix=("pyproject.toml",),
    )
```

Add the shared Hatch exclusion before the wheel target in `pyproject.toml`:

```toml
[tool.hatch.build]
exclude = ["/src/korvid/evals", "/tests/evals"]

[tool.hatch.build.targets.wheel]
packages = ["src/korvid"]
```

- [ ] **Step 4: Run targeted tests and lint**

Run:

```bash
uv run pytest -p no:tach \
  tests/test_release_scripts.py::test_wheel_and_sdist_metadata_match_version_and_extras \
  tests/test_release_scripts.py::test_artifacts_reject_the_evaluation_harness -q
uv run ruff check scripts/release/check_artifacts.py tests/test_release_scripts.py
uv run ruff format --check scripts/release/check_artifacts.py tests/test_release_scripts.py
```

Expected: `3 passed`; Ruff exits successfully without changing files.

- [ ] **Step 5: Build and inspect real artifacts**

Run from the feature worktree:

```bash
ARTIFACT_DIR="$(mktemp -d)"
env -u UV_INDEX -u UV_DEFAULT_INDEX -u UV_INDEX_URL \
  -u UV_EXTRA_INDEX_URL -u UV_FIND_LINKS \
  uv build --no-config \
    --build-constraints scripts/release/build-constraints.txt \
    --require-hashes \
    --out-dir "$ARTIFACT_DIR"
VERSION="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
uv run --no-project python scripts/release/check_artifacts.py \
  --dist "$ARTIFACT_DIR" --version "$VERSION"
rm -rf "$ARTIFACT_DIR"
git diff --exit-code -- uv.lock
```

Expected: build emits one wheel and one sdist; validator prints `wheel and sdist metadata verified`; `uv.lock` is unchanged.

- [ ] **Step 6: Commit the implementation**

```bash
git add pyproject.toml scripts/release/check_artifacts.py tests/test_release_scripts.py
git commit -m "build: exclude evals from distributions" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```
