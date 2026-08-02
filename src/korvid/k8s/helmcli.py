"""Async wrapper around a detected `helm` binary (issue #31).

Browsing releases (issue #28) reads release Secrets and needs no helm at
all; *installing/upgrading/rolling back* requires helm's rendering and repo
machinery, so korvid shells out to the binary it finds on PATH - the same
pattern as `kubectl exec`/`kubectl debug`. Repos and installable charts come
from the user's own helm config (`helm search repo`); nothing is hardcoded.

Pure layer: stdlib only, no Textual. The UI decides when to call and gates
every mutating command behind the approval dialog and the fail-closed audit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_STDERR_TAIL_LINES = 5


def find_helm() -> str | None:
    """Absolute path of the `helm` binary on PATH, or None when absent."""
    return shutil.which("helm")


class HelmError(Exception):
    """A helm invocation failed (non-zero exit, timeout, or bad output)."""


class HelmPreviewUnsupported(HelmError):
    """This helm binary rejected a *preview-only* flag (`--hide-secret`,
    helm 3.15+): the failure says nothing about the mutation itself, which
    never carries the flag - callers must not treat it as a render verdict."""


@dataclass(frozen=True)
class ChartHit:
    """One row of `helm search repo -o json`."""

    name: str
    version: str
    app_version: str
    description: str


@dataclass(frozen=True)
class HelmRepo:
    """One row of `helm repo list -o json`."""

    name: str
    url: str


async def _execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run one subprocess to completion: (exit code, stdout, stderr).

    Killed (with its output discarded) when `timeout` passes - a hung helm
    must never wedge the TUI event loop's worker.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # The binary can vanish or lose its execute bit after detection;
        # surface it as HelmError so the UI's helm notification fires.
        raise HelmError(f"failed to start helm: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HelmError(f"helm timed out after {timeout:.0f}s") from None
    except asyncio.CancelledError:
        # Callers cancel previews (wait_for) and superseded searches
        # (exclusive workers): helm must not outlive the request - its temp
        # values file is deleted the moment the caller unwinds.
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class HelmCLI:
    """Typed argv builders over one helm binary.

    Every command gets `--kube-context` when korvid itself was pointed at a
    non-default context, so helm mutates the same cluster the UI shows.
    """

    def __init__(
        self, binary: str, *, kube_context: str | None = None, timeout: float = 120.0
    ) -> None:
        self._binary = binary
        self._kube_context = kube_context
        self._timeout = timeout

    async def _run(self, *args: str) -> str:
        argv = [self._binary, *args]
        if self._kube_context:
            argv += ["--kube-context", self._kube_context]
        code, stdout, stderr = await _execute(argv, self._timeout)
        if code != 0:
            tail = "\n".join(stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]).strip()
            raise HelmError(tail or f"helm exited with code {code}")
        return stdout

    async def search_repo(self, keyword: str = "") -> list[ChartHit]:
        """Installable charts from the user's configured repos.

        Raises HelmError when no repos are configured (helm exits non-zero
        for that) - the caller turns it into an actionable message.
        """
        args = ["search", "repo"]
        if keyword:
            args.append(keyword)
        args += ["-o", "json"]
        stdout = await self._run(*args)
        try:
            data = json.loads(stdout or "[]")
        except json.JSONDecodeError as exc:
            raise HelmError(f"unexpected helm output: {exc}") from exc
        hits: list[ChartHit] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            hits.append(
                ChartHit(
                    name=str(item.get("name", "")),
                    version=str(item.get("version", "")),
                    app_version=str(item.get("app_version", "")),
                    description=str(item.get("description", "")),
                )
            )
        return hits

    async def has_diff_plugin(self) -> bool:
        """Whether the `helm diff` plugin is installed (best-effort: any
        failure means "no plugin", never an error surfaced to the user)."""
        try:
            stdout = await self._run("plugin", "list")
        except HelmError:
            return False
        return any(line.split()[:1] == ["diff"] for line in stdout.splitlines()[1:])

    async def repo_list(self) -> list[HelmRepo]:
        """Configured chart repositories, in helm's own order.

        `helm repo list` exits non-zero with "no repositories to show" when
        none are configured yet - that is an empty list here, not an error,
        so a fresh setup lands on the repo screen instead of a failure toast.
        """
        try:
            stdout = await self._run("repo", "list", "-o", "json")
        except HelmError as exc:
            if "no repositories" in str(exc).lower():
                return []
            raise
        try:
            data = json.loads(stdout or "[]")
        except json.JSONDecodeError as exc:
            raise HelmError(f"unexpected helm output: {exc}") from exc
        repos: list[HelmRepo] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            repos.append(HelmRepo(name=str(item.get("name", "")), url=str(item.get("url", ""))))
        return repos

    async def repo_add(self, name: str, url: str) -> str:
        """`helm repo add` - a local helm-config write (no cluster access);
        the caller collects name and URL through an explicit typed form."""
        return await self._run("repo", "add", name, url)

    async def repo_update(self) -> str:
        """`helm repo update` - refresh the local index of every repo."""
        return await self._run("repo", "update")

    @staticmethod
    def _version_args(version: str) -> list[str]:
        return ["--version", version] if version else []

    async def show_values(self, chart: str, version: str = "") -> str:
        """`helm show values`: the chart's full annotated default values
        (issue #151) - repo-local, no cluster access."""
        return await self._run("show", "values", chart, *self._version_args(version))

    async def show_readme(self, chart: str, version: str = "") -> str:
        """`helm show readme`: the chart's README (issue #151)."""
        return await self._run("show", "readme", chart, *self._version_args(version))

    async def show_schema(self, chart: str, version: str = "") -> dict[str, Any] | None:
        """The chart's `values.schema.json`, or None when it ships none.

        `helm show` does not expose the schema, so the chart is pulled
        (`helm pull --untar`) into a private temp dir and the file is read
        from the unpacked chart; the directory is removed either way. The
        schema is advisory (issue #151): any failure - pull error, missing
        file, malformed JSON - degrades to None, never an exception.
        """
        tmp = tempfile.mkdtemp(prefix="korvid-helm-schema-")
        try:
            await self._run(
                "pull", chart, *self._version_args(version), "--untar", "--untardir", tmp
            )
            schemas = list(Path(tmp).glob("*/values.schema.json"))
            if not schemas:
                return None
            data = json.loads(schemas[0].read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (HelmError, OSError, ValueError, RecursionError):
            # RecursionError: deeply nested but valid JSON from an
            # untrusted chart can blow the parser's stack.
            return None
        finally:
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp)

    @staticmethod
    def _release_args(
        release: str,
        chart: str,
        namespace: str,
        version: str | None,
        values_file: str | None,
    ) -> list[str]:
        args = [release, chart, "--namespace", namespace]
        if version:
            args += ["--version", version]
        if values_file:
            args += ["--values", values_file]
        return args

    async def install(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        """`helm install` - a cluster write; callers gate it behind approval."""
        return await self._run(
            "install", *self._release_args(release, chart, namespace, version, values_file)
        )

    async def dry_run_install(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        """`helm install --dry-run`: rendered manifests, nothing applied.

        `--hide-secret` (helm 3.15+) keeps generated Secret manifests out of
        the preview - the approval dialog must not bypass korvid's masked
        Secret display. On older helm the flag is unknown and the render
        fails with `HelmPreviewUnsupported` after an error-only fallback
        render (issue #139: only failures the *mutation* would share may
        stop the approval flow, but they must keep stopping it).
        """
        return await self._dry_run(
            "install",
            *self._release_args(release, chart, namespace, version, values_file),
        )

    async def _dry_run(self, *args: str) -> str:
        """One `--dry-run --hide-secret` render.

        helm < 3.15 does not know the preview-only flag: fall back to a
        render *without* it for the verdict alone - the unmasked output is
        discarded (generated Secrets must never reach a preview) and a
        successful fallback still raises `HelmPreviewUnsupported`, so the
        caller shows no preview but a real render error keeps stopping the
        approval flow (issue #139) on old helm too.
        """
        try:
            return await self._run(*args, "--dry-run", "--hide-secret")
        except HelmError as exc:
            if "unknown flag" not in str(exc) or "--hide-secret" not in str(exc):
                raise
            # A failing fallback raises plain HelmError with the render's
            # own stderr - exactly the verdict the mutation would share.
            await self._run(*args, "--dry-run")
            raise HelmPreviewUnsupported(str(exc)) from exc

    async def upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
        reuse_values: bool = False,
    ) -> str:
        """`helm upgrade` - a cluster write; callers gate it behind approval.

        `reuse_values` keeps the release's existing overrides (`helm upgrade`
        resets them to chart defaults otherwise - a silent operational trap).
        """
        args = self._release_args(release, chart, namespace, version, values_file)
        if reuse_values:
            args.append("--reuse-values")
        return await self._run("upgrade", *args)

    async def dry_run_upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
        reuse_values: bool = False,
    ) -> str:
        """`helm upgrade --dry-run`: rendered manifests, nothing applied.

        `--hide-secret` for the same reason as `dry_run_install`.
        """
        args = self._release_args(release, chart, namespace, version, values_file)
        if reuse_values:
            args.append("--reuse-values")
        return await self._dry_run("upgrade", *args)

    async def diff_upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
        reuse_values: bool = False,
    ) -> str:
        """`helm diff upgrade` (plugin): live-vs-proposed diff for previews."""
        args = self._release_args(release, chart, namespace, version, values_file)
        if reuse_values:
            args.append("--reuse-values")
        return await self._run("diff", "upgrade", *args)

    async def rollback(self, release: str, revision: int, namespace: str) -> str:
        """`helm rollback` - a cluster write; callers gate it behind approval."""
        return await self._run("rollback", release, str(revision), "--namespace", namespace)

    @staticmethod
    def _uninstall_args(release: str, namespace: str, keep_history: bool) -> list[str]:
        args = ["uninstall", release, "--namespace", namespace]
        if keep_history:
            args.append("--keep-history")
        return args

    async def uninstall(self, release: str, namespace: str, *, keep_history: bool = False) -> str:
        """`helm uninstall` - deletes every resource the release owns.

        `keep_history` retains the release Secrets so `helm rollback` can
        resurrect it later; the default mirrors helm's (history removed).
        Callers gate this behind the approval dialog and fail-closed audit.
        """
        return await self._run(*self._uninstall_args(release, namespace, keep_history))

    async def dry_run_uninstall(
        self, release: str, namespace: str, *, keep_history: bool = False
    ) -> str:
        """`helm uninstall --dry-run`: simulates the uninstall, nothing deleted.

        Output is helm's uninstall summary (hooks and the would-be-removed
        release), which the approval dialog shows as the preview.
        """
        return await self._run(*self._uninstall_args(release, namespace, keep_history), "--dry-run")

    async def diff_rollback(self, release: str, revision: int, namespace: str) -> str:
        """`helm diff rollback` (plugin): live-vs-revision diff for previews."""
        return await self._run("diff", "rollback", release, str(revision), "--namespace", namespace)


def _field_summary(prop: dict[str, Any]) -> str:
    """One-line type/enum description for a required schema property."""
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(str(v) for v in enum if str(v))
    return str(prop.get("type") or "?")


def required_values_from_schema(
    schema: dict[str, Any] | None, *, _prefix: str = "", _depth: int = 0
) -> list[tuple[str, str]]:
    """(field path, type-or-enum) rows for a schema's required values.

    Follows JSON Schema `required` + `properties` (issue #151): required
    object fields recurse (bounded depth - schemas are untrusted chart
    content), everything else renders its type or enum. Junk shapes yield
    an empty list; the display is advisory, never a gate.
    """
    if not isinstance(schema, dict) or _depth > 3:
        return []
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        return []
    rows: list[tuple[str, str]] = []
    for name in required:
        if not isinstance(name, str):
            continue
        path = f"{_prefix}{name}"
        prop = properties.get(name)
        if not isinstance(prop, dict):
            rows.append((path, "?"))
            continue
        nested = required_values_from_schema(prop, _prefix=f"{path}.", _depth=_depth + 1)
        if nested:
            rows.extend(nested)
        else:
            rows.append((path, _field_summary(prop)))
    return rows
