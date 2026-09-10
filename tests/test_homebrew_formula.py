"""The Homebrew formula is generated from `uv.lock`, not written by hand.

A tap formula lists every transitive dependency as a `resource` with a URL
and a hash. Maintaining that by hand drifts the moment anything moves, and
a stale hash is not a build failure the user can diagnose. `uv.lock`
already holds exactly that data, already resolves from PyPI only, and is
already guarded (`scripts/check_lock_hosts.py`), so it is the honest
source.

These tests pin the generator's contract rather than the current output:
the resource set, what is deliberately excluded, and the properties that
make the formula installable.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_LOCK = _ROOT / "uv.lock"


def _generator() -> Any:
    """Load the script as a module.

    It lives in `scripts/`, which is not a package - the same approach
    `tests/test_lockfile.py` uses for `check_lock_hosts.py`.
    """
    path = _ROOT / "scripts" / "generate_homebrew_formula.py"
    spec = importlib.util.spec_from_file_location("generate_homebrew_formula", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a module that is only referenced by a
    # local can have its globals cleared at collection time, which shows
    # up as an AttributeError on None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_GEN = _generator()
Resource = _GEN.Resource
render_formula = _GEN.render_formula
resolve_resources = _GEN.resolve_resources


def test_the_closure_is_transitive_not_just_the_direct_dependencies() -> None:
    """A formula that lists only direct dependencies does not install.

    `virtualenv_install_with_resources` installs with `--no-deps`, so a
    missing transitive resource is an ImportError at runtime, not a
    resolution error at build time.
    """
    names = {r.name for r in resolve_resources(_LOCK, extras=("agent",))}
    assert "textual" in names, "a direct dependency is missing"
    assert "markdown-it-py" in names, "textual's own dependency is missing"
    assert "mdurl" in names, "the closure stopped one level too early"
    # Textual requires `markdown-it-py[linkify]`; `linkify-it-py` is
    # reachable only through that extra. Reducing an edge to its package
    # name drops it, and `--no-deps` cannot recover it at install time.
    assert "linkify-it-py" in names, "an extra requested on a dependency edge was dropped"
    assert "uc-micro-py" in names, "the extra's own dependency is missing"


def test_the_release_job_calls_the_dedicated_homebrew_handoff_script() -> None:
    """The source workflow should orchestrate, not embed, tap-branch logic."""
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text()
    homebrew_job = workflow.split("  homebrew-formula:")[1]
    assert "python scripts/release/update_homebrew_tap.py" in homebrew_job
    assert "https://x-access-token:" not in homebrew_job
    assert "gh auth setup-git" not in homebrew_job


def test_the_selected_extras_and_nothing_else_are_installed() -> None:
    """The extras boundary is a security property, not packaging taste.

    `[mcp]` puts an HTTP server on the machine. A convenience channel must
    not quietly opt the user into it, so the formula ships the agent stack
    and stops there.
    """
    names = {r.name for r in resolve_resources(_LOCK, extras=("agent",))}
    assert "httpx" in names, "the agent extra is missing"
    assert "keyring" in names, "the agent extra is missing"
    for opted_in_only in ("mcp", "starlette", "uvicorn", "azure-identity"):
        assert opted_in_only not in names, f"{opted_in_only} was installed without being asked for"


def test_development_dependencies_never_reach_the_formula() -> None:
    """`uv.lock` holds the dev group too; shipping it would be absurd."""
    names = {r.name for r in resolve_resources(_LOCK, extras=("agent",))}
    for dev_only in ("pytest", "mypy", "ruff", "tach", "gitpython"):
        assert dev_only not in names, f"{dev_only} is a development tool"


def test_the_project_itself_is_not_one_of_its_own_resources() -> None:
    """korvid is the formula's `url`, not a resource of it."""
    names = {r.name for r in resolve_resources(_LOCK, extras=("agent",))}
    assert "korvid" not in names


def test_packages_for_platforms_homebrew_does_not_build_are_excluded() -> None:
    """Homebrew builds on macOS and Linux.

    `pywin32` has no sdist at all, so a formula naming it cannot even be
    fetched. `pywin32-ctypes` is the harder case: its own record carries
    no marker, and it is Windows-only solely because of the edge
    `keyring` declares — so the edge has to be read, not just the node.
    """
    names = {r.name for r in resolve_resources(_LOCK, extras=("agent",))}
    assert "pywin32" not in names
    assert "pywin32-ctypes" not in names, "a Windows-only edge was followed onto a brew target"


def test_linux_keyring_dependencies_survive_mixed_platform_markers() -> None:
    names = {r.name for r in resolve_resources(_LOCK, extras=("agent",))}
    assert {"secretstorage", "cryptography", "cffi", "pycparser", "jeepney"} <= names


@pytest.mark.parametrize(
    ("marker", "excluded"),
    [
        ("sys_platform == 'win32'", True),
        ("python_version >= '3.13' and sys_platform == 'win32'", True),
        (
            "(python_version < '3.13' and sys_platform == 'win32') or "
            "(python_version >= '3.13' and sys_platform == 'win32')",
            True,
        ),
        ("sys_platform == 'win32' or sys_platform == 'linux'", False),
        ("sys_platform == 'darwin' or sys_platform == 'win32'", False),
        (
            "(python_version >= '3.15' and sys_platform == 'win32') or "
            "(sys_platform != 'emscripten' and sys_platform != 'win32')",
            False,
        ),
        (None, False),
    ],
)
def test_platform_exclusion_requires_every_alternative_to_exclude_homebrew(
    marker: str | None, excluded: bool
) -> None:
    assert _GEN._marker_excludes(marker) is excluded
    if marker is not None:
        assert _GEN._is_excluded({"resolution-markers": [marker]}) is excluded


def test_the_formula_advertises_the_project_license() -> None:
    """`brew audit` does not check this, and a wrong licence is a licence
    claim about someone else's software."""
    import tomllib

    declared = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["license"]
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[],
    )
    assert f'license "{declared}"' in ruby


def test_every_resource_carries_a_pypi_url_and_a_sha256() -> None:
    """A resource without a hash is an unverified download."""
    resources = resolve_resources(_LOCK, extras=("agent",))
    assert resources, "the closure is empty"
    for resource in resources:
        assert resource.url.startswith("https://files.pythonhosted.org/"), resource
        assert len(resource.sha256) == 64, resource
        assert resource.sha256.islower(), resource


def test_the_rendered_formula_is_valid_ruby() -> None:
    """A syntax error in a formula is only found by whoever runs `brew`."""
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[Resource(name="rich", url="https://files.pythonhosted.org/x", sha256="b" * 64)],
    )
    if not _ruby_available():
        pytest.skip("ruby is not installed")
    check = subprocess.run(
        ["ruby", "-c", "-"], input=ruby, capture_output=True, text=True, check=False
    )
    assert check.returncode == 0, check.stderr


def test_the_rendered_formula_declares_what_brew_needs() -> None:
    """The pieces `brew audit` and the tap's CI both look for."""
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[Resource(name="rich", url="https://files.pythonhosted.org/x", sha256="b" * 64)],
    )
    assert "class Korvid < Formula" in ruby
    assert "include Language::Python::Virtualenv" in ruby
    assert 'depends_on "python@3.13"' in ruby, "the formula must build against brew's own Python"
    assert 'resource "rich" do' in ruby
    assert "virtualenv_install_with_resources" in ruby
    assert "test do" in ruby, "brew test is part of the acceptance for this tap"


def test_the_formula_passes_brew_audit_without_running_brew() -> None:
    """`brew audit --strict` findings, pinned as properties.

    Running brew here is not an option - auditing needs a tap and a
    network that can reach `files.pythonhosted.org`. These three came from
    a real `brew audit --strict` run and are the ones a formula can get
    wrong silently.
    """
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[
            Resource(name="pyyaml", url="https://files.pythonhosted.org/x", sha256="b" * 64)
        ],
    )
    # "`version` is redundant with version scanned from URL": brew derives
    # it, and a hand-set value can disagree with the artifact.
    assert "\n  version " not in ruby, "an explicit version duplicates the one brew reads"
    # PyYAML builds its C extension against libyaml; without it brew warns
    # and the build silently falls back to the pure-Python loader.
    assert 'depends_on "libyaml"' in ruby, "pyyaml needs libyaml declared"
    # brew orders build dependencies first, then runtime, alphabetically
    # within each group.
    declared = [line.strip() for line in ruby.splitlines() if line.startswith("  depends_on ")]
    assert declared == sorted(declared, key=lambda line: ("=>" not in line, line)), (
        f"depends_on is out of order: {declared}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "cryptography",
        "fastuuid",
        "hf-xet",
        "jiter",
        "litellm",
        "pydantic-core",
        "rpds-py",
        "tiktoken",
        "tokenizers",
    ],
)
def test_a_rust_built_resource_declares_the_rust_toolchain(name: str) -> None:
    """Each Rust resource needs a toolchain independently of other resources.

    Homebrew builds every resource from source, so the toolchain has to be
    declared or the install dies with "can't find Rust compiler" — which
    is exactly how this was found, on a runner rather than in review.
    `:build` because nothing links against it at runtime.
    """
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[Resource(name=name, url="https://files.pythonhosted.org/x", sha256="b" * 64)],
    )
    assert 'depends_on "rust" => :build' in ruby
    declared = [line.strip() for line in ruby.splitlines() if line.startswith("  depends_on ")]
    assert declared[0].startswith('depends_on "rust"'), (
        f"brew wants build dependencies first: {declared}"
    )


def test_a_formula_without_pyyaml_does_not_declare_libyaml() -> None:
    """The dependency is a consequence of the closure, not a constant."""
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[Resource(name="rich", url="https://files.pythonhosted.org/x", sha256="b" * 64)],
    )
    assert 'depends_on "libyaml"' not in ruby
    assert "rust" not in ruby
    assert "append_to_rustflags" not in ruby


def test_macos_python_extension_symbols_are_resolved_at_load_time() -> None:
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[
            Resource(name="tokenizers", url="https://files.pythonhosted.org/x", sha256="b" * 64)
        ],
    )
    linking = 'ENV.append_to_rustflags "-C link-arg=-Wl,-undefined,dynamic_lookup" if OS.mac?'
    assert linking in ruby
    assert ruby.index(linking) < ruby.index("virtualenv_install_with_resources")


@pytest.mark.parametrize("names", [["hf-xet"], ["litellm"], ["litellm", "hf-xet"]])
def test_entropy_resources_build_with_scoped_unoptimized_c(names: list[str]) -> None:
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[
            Resource(name=name, url="https://files.pythonhosted.org/x", sha256="b" * 64)
            for name in names
        ],
    )
    normal_install = (
        f"venv = virtualenv_install_with_resources(without: {json.dumps(sorted(names))})"
    )
    calls = ", ".join(f'resource("{name}")' for name in sorted(names))
    entropy_safe_install = f"ENV.O0 {{ venv.pip_install [{calls}] }}"
    assert normal_install in ruby
    assert entropy_safe_install in ruby
    assert ruby.index(normal_install) < ruby.index(entropy_safe_install)
    assert "AWS_LC_SYS_NO_JITTER_ENTROPY" not in ruby


def test_a_formula_without_entropy_resources_keeps_standard_resource_installation() -> None:
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[],
    )
    assert "    virtualenv_install_with_resources\n" in ruby
    assert "ENV.O0" not in ruby
    assert "without:" not in ruby
    assert 'system libexec/"bin/python"' not in ruby


@pytest.mark.parametrize(
    ("names", "modules"),
    [
        (["hf-xet"], "hf_xet"),
        (["litellm"], "litellm.rust_bridge._native"),
        (["tokenizers"], "tokenizers.tokenizers"),
        (
            ["tokenizers", "litellm", "hf-xet"],
            "hf_xet, litellm.rust_bridge._native, tokenizers.tokenizers",
        ),
    ],
)
def test_formula_smokes_native_imports_with_its_own_python(names: list[str], modules: str) -> None:
    ruby = render_formula(
        version="1.2.3",
        url="https://files.pythonhosted.org/packages/aa/korvid-1.2.3.tar.gz",
        sha256="a" * 64,
        resources=[
            Resource(name=name, url="https://files.pythonhosted.org/x", sha256="b" * 64)
            for name in names
        ],
    )
    test_block = " ".join(ruby.split("  test do\n", 1)[1].split())
    smoke = f'system libexec/"bin/python", "-I", "-c", "import {modules}"'
    offline = 'ENV["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"'
    assert smoke in test_block
    if "litellm" in names:
        assert offline in test_block
        assert test_block.index(offline) < test_block.index(smoke)
    else:
        assert offline not in test_block


def test_the_version_travels_into_the_test_block() -> None:
    """`brew test` asserting the wrong version passes on a stale install."""
    ruby = render_formula(
        version="9.9.9",
        url="https://files.pythonhosted.org/packages/aa/korvid-9.9.9.tar.gz",
        sha256="a" * 64,
        resources=[],
    )
    assert "9.9.9" in ruby.split("test do")[1], "brew test does not check the built version"


def _ruby_available() -> bool:
    try:
        subprocess.run(["ruby", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def test_the_release_attaches_the_formula_before_it_needs_a_token() -> None:
    """A missing App binding must fail only after the formula is preserved."""
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text()
    steps = workflow.split("      - name: ")
    upload = next(i for i, step in enumerate(steps) if "Attach the formula" in step[:60])
    require_app = next(
        i for i, step in enumerate(steps) if "Require Homebrew App credentials" in step[:60]
    )
    mint = next(i for i, step in enumerate(steps) if "Mint the Homebrew tap App token" in step[:60])
    bump = next(i for i, step in enumerate(steps) if "Update the Homebrew tap" in step[:60])
    assert upload < require_app < mint < bump, "the tap is attempted before the artifact is safe"
    assert 'if [ -z "${HOMEBREW_APP_ID:-}" ]; then' in workflow
    assert 'if [ -z "${HOMEBREW_APP_PRIVATE_KEY:-}" ]; then' in workflow
    assert "handoff-ready=false" not in workflow
    assert "::warning::Skipping Homebrew tap handoff" not in workflow
    assert "HOMEBREW_TAP_TOKEN" not in workflow


def test_the_formula_job_runs_only_after_the_release_is_published() -> None:
    """The formula names a published sdist; generating it earlier would
    hash an artifact that does not exist yet."""
    import yaml

    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "release.yml").read_text())
    job = workflow["jobs"]["homebrew-formula"]
    assert "publish-pypi" in job["needs"]
    assert "finalize-github-release" in job["needs"]


def test_the_formula_job_mints_a_scoped_homebrew_app_token_late() -> None:
    import yaml

    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = workflow["jobs"]["homebrew-formula"]["steps"]
    mint = next(step for step in steps if step.get("id") == "homebrew-app-token")
    assert "if" not in mint
    assert (
        mint["uses"] == "actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349"
    )
    assert mint["with"]["owner"] == "hellices"
    assert mint["with"]["repositories"] == "homebrew-korvid"
    assert mint["with"]["app-id"] == "${{ vars.HOMEBREW_APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.HOMEBREW_APP_PRIVATE_KEY }}"
    assert mint["with"]["permission-contents"] == "write"
    assert mint["with"]["permission-pull-requests"] == "write"


def test_the_handoff_step_uses_the_app_slug_bot_login() -> None:
    import yaml

    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "release.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["homebrew-formula"]["steps"]
        if "scripts/release/update_homebrew_tap.py" in str(step.get("run", ""))
    )
    assert "if" not in step
    env = step["env"]
    assert env["GH_TOKEN"] == "${{ steps.homebrew-app-token.outputs.token }}"
    assert env["BOT_LOGIN"] == "${{ steps.homebrew-app-token.outputs.app-slug }}[bot]"
    assert '--bot-login "$BOT_LOGIN"' in step["run"]
    assert "uv run" not in step["run"]


def test_the_release_workflow_has_no_manual_homebrew_pat_fallback_or_merge_logic() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text()
    homebrew_job = workflow.split("  homebrew-formula:")[1]
    assert "The formula is attached to the release and is correct." not in homebrew_job
    assert 'gh release download "$TAG" korvid.rb' not in homebrew_job
    assert "gh pr merge" not in homebrew_job
    assert "auto-merge" not in homebrew_job


def test_an_extra_augments_a_package_rather_than_replacing_it(tmp_path: Path) -> None:
    """`foo[bar]` means foo *and* bar, not bar instead of foo.

    A package reached only through an extra must still contribute its
    ordinary dependencies. The real lock hides this - `rich` also reaches
    `markdown-it-py` without the extra, so its plain edges get followed
    anyway - hence a fixture where the only path is the extra.
    """
    lock = tmp_path / "uv.lock"
    digest = "a" * 64
    lock.write_text(
        f"""
version = 1

[[package]]
name = "korvid"
version = "0.0.0"
source = {{ editable = "." }}
dependencies = [{{ name = "solo", extra = ["fancy"] }}]

[[package]]
name = "solo"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "ordinary" }}]
sdist = {{ url = "https://files.pythonhosted.org/solo.tar.gz", hash = "sha256:{digest}" }}

[package.optional-dependencies]
fancy = [{{ name = "extraonly" }}]

[[package]]
name = "ordinary"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.pythonhosted.org/ordinary.tar.gz", hash = "sha256:{digest}" }}

[[package]]
name = "extraonly"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.pythonhosted.org/extraonly.tar.gz", hash = "sha256:{digest}" }}
""",
        encoding="utf-8",
    )
    names = {r.name for r in resolve_resources(lock, extras=())}
    assert "extraonly" in names, "the extra's own dependency is missing"
    assert "ordinary" in names, "an extra replaced the package's normal dependencies"
