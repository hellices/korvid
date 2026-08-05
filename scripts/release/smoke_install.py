#!/usr/bin/env python3
"""Smoke-test the built korvid wheel in a clean, disposable environment."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path

_VARIANT_EXTRAS = {
    "base": None,
    "agent": "agent",
    "mcp": "mcp",
    "all": "all",
}
_VARIANT_MODULES = {
    "base": frozenset(),
    "agent": frozenset({"keyring"}),
    "mcp": frozenset({"mcp"}),
    "all": frozenset({"keyring", "mcp"}),
}


def _normalize_variant(variant: str) -> str:
    if variant not in _VARIANT_EXTRAS:
        raise ValueError(f"unknown variant: {variant}")
    return variant


def requirement_for(wheel: Path, variant: str) -> str:
    """Return the install requirement for *variant* from the local wheel URL."""
    normalized = _normalize_variant(variant)
    wheel_url = wheel.resolve().as_uri()
    extra = _VARIANT_EXTRAS[normalized]
    if extra is None:
        return wheel_url
    return f"korvid[{extra}] @ {wheel_url}"


def required_modules(variant: str) -> set[str]:
    """Modules that must import after installing *variant*."""
    normalized = _normalize_variant(variant)
    return set(_VARIANT_MODULES[normalized])


def validate_wheel_version(wheel: Path, version: str) -> None:
    """Reject a wheel path whose filename does not match *version*."""
    expected_prefix = f"korvid-{version}-"
    if wheel.suffix != ".whl" or not wheel.name.startswith(expected_prefix):
        raise ValueError(
            f"wheel filename {wheel.name!r} does not match requested version {version!r}"
        )


def _venv_python(env_dir: Path) -> Path:
    scripts = env_dir / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python")


def _venv_launcher_dir(env_dir: Path) -> Path:
    return env_dir / ("Scripts" if os.name == "nt" else "bin")


def _resolve_launcher(env_dir: Path) -> Path | None:
    launcher = shutil.which("korvid", path=str(_venv_launcher_dir(env_dir)))
    return Path(launcher) if launcher is not None else None


@dataclass(frozen=True)
class _StateRoots:
    home: Path
    config: Path
    data: Path
    state: Path
    appdata: Path
    localappdata: Path


def _state_roots(workspace: Path) -> _StateRoots:
    return _StateRoots(
        home=workspace / "home",
        config=workspace / "xdg" / "config",
        data=workspace / "xdg" / "data",
        state=workspace / "xdg" / "state",
        appdata=workspace / "appdata",
        localappdata=workspace / "localappdata",
    )


def _command_env(roots: _StateRoots) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA": str(roots.appdata),
            "HOME": str(roots.home),
            "LOCALAPPDATA": str(roots.localappdata),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
            "USERPROFILE": str(roots.home),
            "XDG_CONFIG_HOME": str(roots.config),
            "XDG_DATA_HOME": str(roots.data),
            "XDG_STATE_HOME": str(roots.state),
        }
    )
    return env


def _format_failure(args: list[str], exc: subprocess.CalledProcessError) -> str:
    rendered = " ".join(args)
    parts = [f"command failed: {rendered}"]
    stdout = getattr(exc, "stdout", "")
    stderr = getattr(exc, "stderr", "")
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n".join(parts)


def _run(args: list[str], *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(_format_failure(args, exc)) from exc


def _pip_install(
    python: Path,
    requirement: str,
    *,
    env: dict[str, str],
    cwd: Path,
    upgrade: bool = False,
) -> None:
    args = [str(python), "-m", "pip", "install", requirement]
    if upgrade:
        args.insert(4, "--upgrade")
    _run(args, env=env, cwd=cwd)


def _pip_uninstall(python: Path, package: str, *, env: dict[str, str], cwd: Path) -> None:
    _run(
        [str(python), "-m", "pip", "uninstall", "--yes", package],
        env=env,
        cwd=cwd,
    )


def _assert_version(python: Path, version: str, *, env: dict[str, str], cwd: Path) -> None:
    _run(
        [
            str(python),
            "-c",
            f"import korvid; assert korvid.__version__ == {version!r}, korvid.__version__",
        ],
        env=env,
        cwd=cwd,
    )


def _assert_module_imports(
    python: Path, modules: set[str], *, env: dict[str, str], cwd: Path
) -> None:
    for module in sorted(modules):
        _run([str(python), "-c", f"import {module}"], env=env, cwd=cwd)


def _assert_help_and_version(
    launcher: Path, version: str, *, env: dict[str, str], cwd: Path
) -> None:
    help_result = _run([str(launcher), "--help"], env=env, cwd=cwd)
    if "usage" not in help_result.stdout.lower():
        raise RuntimeError("korvid --help did not print usage text")
    version_result = _run([str(launcher), "--version"], env=env, cwd=cwd)
    version_output = version_result.stdout.strip()
    if "korvid" not in version_output or not version_output.endswith(version):
        raise RuntimeError(f"unexpected korvid --version output: {version_output!r}")


def _assert_korvid_removed(python: Path, env_dir: Path, *, env: dict[str, str], cwd: Path) -> None:
    _run(
        [
            str(python),
            "-c",
            "import importlib.util; "
            "raise SystemExit(0 if importlib.util.find_spec('korvid') is None else 1)",
        ],
        env=env,
        cwd=cwd,
    )
    if _resolve_launcher(env_dir) is not None:
        raise RuntimeError("korvid console launcher still exists after uninstall")


def _unexpected_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _assert_no_user_state(roots: _StateRoots) -> None:
    unexpected: list[Path] = []
    for root in (
        roots.home,
        roots.config,
        roots.data,
        roots.state,
        roots.appdata,
        roots.localappdata,
    ):
        unexpected.extend(_unexpected_files(root))
    if unexpected:
        rendered = ", ".join(str(path) for path in unexpected)
        raise RuntimeError(f"noninteractive smoke created user-state files: {rendered}")


def _smoke_install(wheel: Path, version: str, variant: str, workspace: Path) -> None:
    validate_wheel_version(wheel, version)
    roots = _state_roots(workspace)
    env = _command_env(roots)
    env_dir = workspace / "venv"
    venv.create(env_dir, with_pip=True)
    python = _venv_python(env_dir)

    _pip_install(python, requirement_for(wheel, "base"), env=env, cwd=workspace)
    if variant != "base":
        _pip_install(
            python,
            requirement_for(wheel, variant),
            env=env,
            cwd=workspace,
            upgrade=True,
        )

    launcher = _resolve_launcher(env_dir)
    if launcher is None:
        raise RuntimeError("korvid console launcher was not installed")

    _assert_version(python, version, env=env, cwd=workspace)
    _assert_module_imports(python, required_modules(variant), env=env, cwd=workspace)
    _assert_help_and_version(launcher, version, env=env, cwd=workspace)
    _pip_uninstall(python, "korvid", env=env, cwd=workspace)
    _assert_korvid_removed(python, env_dir, env=env, cwd=workspace)
    _assert_no_user_state(roots)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--variant", required=True, choices=sorted(_VARIANT_EXTRAS))
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)

    wheel = Path(args.wheel)
    if not wheel.is_file():
        print(f"wheel not found: {wheel}", file=sys.stderr)
        return 1

    workspace = Path(args.workspace)
    if workspace.exists():
        print(f"workspace must not already exist: {workspace}", file=sys.stderr)
        return 1

    workspace.mkdir(parents=True)
    failure: Exception | None = None
    try:
        _smoke_install(wheel, args.version, args.variant, workspace)
    except Exception as exc:  # narrow runtime surface; subprocess errors are wrapped above
        failure = exc

    cleanup_error: OSError | None = None
    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        cleanup_error = exc

    if failure is not None:
        print(str(failure), file=sys.stderr)
        return 1
    if cleanup_error is not None:
        print(f"failed to clean workspace {workspace}: {cleanup_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
