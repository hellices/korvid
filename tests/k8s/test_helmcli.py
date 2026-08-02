"""Tests for the helm CLI wrapper (issue #31).

`_execute` is patched so no real helm binary is needed; one test runs the
real `_execute` against `/bin/echo`-style stand-ins to keep the subprocess
plumbing honest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from korvid.k8s.helmcli import (
    ChartHit,
    HelmCLI,
    HelmError,
    HelmPreviewUnsupported,
    HelmRepo,
    find_helm,
)

pytestmark = pytest.mark.asyncio


def _cli(**kwargs: object) -> tuple[HelmCLI, mock.AsyncMock]:
    """A HelmCLI over a patched `_execute`; returns (cli, execute mock)."""
    execute = mock.AsyncMock(return_value=(0, "", ""))
    cli = HelmCLI("/usr/local/bin/helm", **kwargs)  # type: ignore[arg-type]  # kwargs typed per call site
    return cli, execute


SEARCH_JSON = json.dumps(
    [
        {
            "name": "bitnami/nginx",
            "version": "18.1.0",
            "app_version": "1.27.0",
            "description": "NGINX Open Source",
        },
        {"name": "bitnami/postgresql", "version": "15.5.0", "app_version": "16.3.0"},
    ]
)


async def test_search_repo_parses_chart_hits() -> None:
    cli, execute = _cli()
    execute.return_value = (0, SEARCH_JSON, "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        hits = await cli.search_repo()
    assert hits == [
        ChartHit("bitnami/nginx", "18.1.0", "1.27.0", "NGINX Open Source"),
        ChartHit("bitnami/postgresql", "15.5.0", "16.3.0", ""),
    ]
    argv = execute.await_args_list[0].args[0]
    assert argv[:4] == ["/usr/local/bin/helm", "search", "repo", "-o"]
    assert "json" in argv


async def test_search_repo_passes_keyword() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "[]", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        hits = await cli.search_repo("nginx")
    assert hits == []
    argv = execute.await_args_list[0].args[0]
    assert argv[:4] == ["/usr/local/bin/helm", "search", "repo", "nginx"]


async def test_search_repo_no_repositories_raises_helm_error() -> None:
    cli, execute = _cli()
    execute.return_value = (1, "", "Error: no repositories configured\n")
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match="no repositories configured"),
    ):
        await cli.search_repo()


async def test_search_repo_skips_malformed_entries() -> None:
    cli, execute = _cli()
    execute.return_value = (0, json.dumps(["junk", {"name": "r/c", "version": "1.0.0"}]), "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        hits = await cli.search_repo()
    assert hits == [ChartHit("r/c", "1.0.0", "", "")]


async def test_search_repo_invalid_json_raises_helm_error() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "not json", "")
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match="unexpected helm output"),
    ):
        await cli.search_repo()


async def test_kube_context_appended_to_every_invocation() -> None:
    cli, execute = _cli(kube_context="prod")
    execute.return_value = (0, "[]", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.search_repo()
    argv = execute.await_args_list[0].args[0]
    assert argv[-2:] == ["--kube-context", "prod"]


async def test_install_builds_argv_and_returns_stdout() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "NAME: web\nSTATUS: deployed\n", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.install("web", "bitnami/nginx", "default", version="18.1.0")
    assert "STATUS: deployed" in out
    argv = execute.await_args_list[0].args[0]
    assert argv == [
        "/usr/local/bin/helm",
        "install",
        "web",
        "bitnami/nginx",
        "--namespace",
        "default",
        "--version",
        "18.1.0",
    ]


async def test_dry_run_install_appends_dry_run_flag() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "manifest...", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.dry_run_install("web", "bitnami/nginx", "default")
    argv = execute.await_args_list[0].args[0]
    assert "--dry-run" in argv
    assert argv[1] == "install"


async def test_dry_run_hide_secret_rejection_raises_preview_unsupported() -> None:
    """helm < 3.15 does not know the preview-only `--hide-secret` flag: its
    rejection must surface as HelmPreviewUnsupported so callers never
    mistake it for a render verdict (issue #139) - the real install and
    upgrade never carry the flag. The fallback render's output is discarded
    (Secrets must stay masked), only its verdict is kept."""
    cli, execute = _cli()
    execute.side_effect = [
        (1, "", "Error: unknown flag: --hide-secret\n"),
        (0, "SECRET-BEARING-MANIFEST", ""),  # fallback render succeeds
    ]
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmPreviewUnsupported, match="hide-secret") as excinfo,
    ):
        await cli.dry_run_install("web", "bitnami/nginx", "default")
    # the unmasked render never leaks through the exception
    assert "SECRET-BEARING-MANIFEST" not in str(excinfo.value)
    fallback_argv = execute.await_args_list[1].args[0]
    assert "--dry-run" in fallback_argv
    assert "--hide-secret" not in fallback_argv


async def test_old_helm_fallback_render_still_delivers_the_render_verdict() -> None:
    """helm 3.13/3.14 (no --hide-secret) must not skip issue #139's
    protection: the error-only fallback render re-runs without the flag and
    a failing render surfaces as the plain HelmError verdict."""
    cli, execute = _cli()
    execute.side_effect = [
        (1, "", "Error: unknown flag: --hide-secret\n"),
        (1, "", "Error: execution error: 'image.repository' must be set\n"),
    ]
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match=r"image\.repository") as excinfo,
    ):
        await cli.dry_run_install("web", "bitnami/nginx", "default")
    assert not isinstance(excinfo.value, HelmPreviewUnsupported)


async def test_dry_run_upgrade_hide_secret_rejection_raises_preview_unsupported() -> None:
    cli, execute = _cli()
    execute.side_effect = [
        (1, "", "Error: unknown flag: --hide-secret\n"),
        (0, "rendered", ""),
    ]
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmPreviewUnsupported, match="hide-secret"),
    ):
        await cli.dry_run_upgrade("web", "bitnami/nginx", "default")


async def test_dry_run_render_error_stays_a_plain_helm_error() -> None:
    """A real render failure must NOT be softened to the preview-only
    class - it is the exact error the mutation would produce."""
    cli, execute = _cli()
    execute.return_value = (1, "", "Error: execution error: 'image.repository' must be set\n")
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match=r"image\.repository") as excinfo,
    ):
        await cli.dry_run_install("web", "bitnami/nginx", "default")
    assert not isinstance(excinfo.value, HelmPreviewUnsupported)


async def test_install_with_values_file() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.install("web", "r/c", "ns", values_file="/tmp/v.yaml")
    argv = execute.await_args_list[0].args[0]
    assert argv[-2:] == ["--values", "/tmp/v.yaml"]


async def test_upgrade_builds_upgrade_argv() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.upgrade("web", "bitnami/nginx", "default", version="18.2.0")
    argv = execute.await_args_list[0].args[0]
    assert argv[1:4] == ["upgrade", "web", "bitnami/nginx"]
    assert argv[-2:] == ["--version", "18.2.0"]


async def test_dry_run_upgrade_appends_dry_run_flag() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.dry_run_upgrade("web", "r/c", "ns")
    argv = execute.await_args_list[0].args[0]
    assert argv[1] == "upgrade"
    assert "--dry-run" in argv


async def test_rollback_builds_argv() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "Rollback was a success!", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.rollback("web", 2, "default")
    assert "success" in out
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["rollback", "web", "2", "--namespace"]


async def test_uninstall_builds_argv() -> None:
    cli, execute = _cli()
    execute.return_value = (0, 'release "web" uninstalled\n', "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.uninstall("web", "default")
    assert "uninstalled" in out
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["uninstall", "web", "--namespace", "default"]
    assert "--keep-history" not in argv
    assert "--dry-run" not in argv


async def test_uninstall_keep_history_flag() -> None:
    cli, execute = _cli()
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.uninstall("web", "default", keep_history=True)
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["uninstall", "web", "--namespace", "default"]
    assert "--keep-history" in argv
    assert "--dry-run" not in argv


async def test_dry_run_uninstall_appends_dry_run_flag() -> None:
    cli, execute = _cli()
    execute.return_value = (0, 'release "web" uninstalled\n', "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.dry_run_uninstall("web", "default")
    assert "uninstalled" in out
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["uninstall", "web", "--namespace", "default"]
    assert "--dry-run" in argv


async def test_nonzero_exit_raises_helm_error_with_stderr_tail() -> None:
    cli, execute = _cli()
    execute.return_value = (1, "", "Error: chart not found: bogus/none\n")
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match="chart not found"),
    ):
        await cli.install("web", "bogus/none", "default")


async def test_nonzero_exit_without_stderr_reports_exit_code() -> None:
    cli, execute = _cli()
    execute.return_value = (3, "", "")
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match="exited with code 3"),
    ):
        await cli.rollback("web", 1, "ns")


async def test_has_diff_plugin_true_when_listed() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "NAME\tVERSION\tDESCRIPTION\ndiff\t3.9.5\tPreview upgrades\n", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        assert await cli.has_diff_plugin() is True


async def test_has_diff_plugin_false_when_absent_or_failing() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "NAME\tVERSION\tDESCRIPTION\n", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        assert await cli.has_diff_plugin() is False
    execute.return_value = (1, "", "boom")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        assert await cli.has_diff_plugin() is False


async def test_diff_upgrade_uses_diff_plugin() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "+ line", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.diff_upgrade("web", "r/c", "ns", version="1.2.3")
    assert out == "+ line"
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["diff", "upgrade", "web", "r/c"]


async def test_diff_rollback_uses_diff_plugin() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "- line", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.diff_rollback("web", 2, "ns")
    assert out == "- line"
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["diff", "rollback", "web", "2"]


async def test_execute_runs_a_real_subprocess() -> None:
    """The real `_execute` captures exit code, stdout and stderr."""
    from korvid.k8s.helmcli import _execute

    code, out, err = await _execute(
        [sys.executable, "-c", "import sys; print('o'); print('e', file=sys.stderr); sys.exit(4)"],
        timeout=30.0,
    )
    assert code == 4
    assert out.strip() == "o"
    assert err.strip() == "e"


async def test_execute_timeout_raises_helm_error() -> None:
    from korvid.k8s.helmcli import _execute

    with pytest.raises(HelmError, match="timed out"):
        await _execute([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)


async def test_find_helm_uses_path_lookup() -> None:
    with mock.patch("korvid.k8s.helmcli.shutil.which", return_value="/opt/helm") as which:
        assert find_helm() == "/opt/helm"
    which.assert_called_once_with("helm")
    with mock.patch("korvid.k8s.helmcli.shutil.which", return_value=None):
        assert find_helm() is None


async def test_execute_kills_the_subprocess_on_cancellation() -> None:
    """UI previews wrap `_execute` in `wait_for` and exclusive workers cancel
    in-flight searches: cancellation must kill and reap helm, not leave it
    running after its temp values file is gone."""
    import asyncio

    from korvid.k8s.helmcli import _execute

    class HungProc:
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False
            self.reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()  # hang until cancelled
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.reaped = True
            return -9

    proc = HungProc()
    with mock.patch("korvid.k8s.helmcli.asyncio.create_subprocess_exec", return_value=proc):
        task = asyncio.ensure_future(_execute(["helm", "version"], timeout=30.0))
        await asyncio.sleep(0)  # let the task reach communicate()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert proc.killed
    assert proc.reaped


async def test_spawn_failure_raises_helm_error() -> None:
    """A vanished/unexecutable binary must surface as HelmError so the UI
    shows its actionable helm notification instead of a raw worker crash."""
    from korvid.k8s.helmcli import _execute

    with pytest.raises(HelmError, match="failed to start helm"):
        await _execute(["/nonexistent/helm-binary-for-tests"], timeout=5.0)


async def test_dry_run_previews_hide_generated_secrets() -> None:
    """Dry-run stdout is rendered in the approval dialog: generated Secret
    manifests must be hidden so chart data/stringData cannot bypass the
    masked Secret display."""
    cli, execute = _cli()
    execute.return_value = (0, "manifest...", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.dry_run_install("web", "bitnami/nginx", "default")
        await cli.dry_run_upgrade("web", "bitnami/nginx", "default")
    for call in execute.await_args_list:
        assert "--hide-secret" in call.args[0]


async def test_upgrade_reuse_values_flag() -> None:
    """`--reuse-values` keeps the release's existing overrides; without it a
    default upgrade silently resets them to chart defaults."""
    cli, execute = _cli()
    execute.return_value = (0, "ok", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.upgrade("web", "bitnami/nginx", "default", reuse_values=True)
        await cli.dry_run_upgrade("web", "bitnami/nginx", "default", reuse_values=True)
        await cli.diff_upgrade("web", "bitnami/nginx", "default", reuse_values=True)
    for call in execute.await_args_list:
        assert "--reuse-values" in call.args[0]


async def test_upgrade_without_reuse_omits_the_flag() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "ok", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.upgrade("web", "bitnami/nginx", "default")
    assert "--reuse-values" not in execute.await_args_list[0].args[0]


REPO_LIST_JSON = json.dumps(
    [
        {"name": "bitnami", "url": "https://charts.bitnami.com/bitnami"},
        {"name": "jetstack", "url": "https://charts.jetstack.io"},
    ]
)


async def test_repo_list_parses_repos() -> None:
    cli, execute = _cli()
    execute.return_value = (0, REPO_LIST_JSON, "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        repos = await cli.repo_list()
    assert repos == [
        HelmRepo("bitnami", "https://charts.bitnami.com/bitnami"),
        HelmRepo("jetstack", "https://charts.jetstack.io"),
    ]
    argv = execute.await_args_list[0].args[0]
    assert argv[1:4] == ["repo", "list", "-o"]


async def test_repo_list_empty_when_no_repos_configured() -> None:
    """`helm repo list` exits non-zero with 'no repositories to show' when
    none are configured - that is an empty list, not an error."""
    cli, execute = _cli()
    execute.return_value = (1, "", "Error: no repositories to show")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        repos = await cli.repo_list()
    assert repos == []


async def test_repo_list_other_failure_raises_helm_error() -> None:
    cli, execute = _cli()
    execute.return_value = (1, "", "Error: something broke")
    with (
        mock.patch("korvid.k8s.helmcli._execute", execute),
        pytest.raises(HelmError, match="something broke"),
    ):
        await cli.repo_list()


async def test_repo_list_skips_malformed_entries() -> None:
    cli, execute = _cli()
    execute.return_value = (0, json.dumps([{"url": "https://x"}, {"name": "ok", "url": "u"}]), "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        repos = await cli.repo_list()
    assert repos == [HelmRepo("ok", "u")]


async def test_repo_add_builds_argv() -> None:
    cli, execute = _cli()
    execute.return_value = (0, '"bitnami" has been added to your repositories\n', "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.repo_add("bitnami", "https://charts.bitnami.com/bitnami")
    assert "added" in out
    argv = execute.await_args_list[0].args[0]
    assert argv[1:5] == ["repo", "add", "bitnami", "https://charts.bitnami.com/bitnami"]


async def test_repo_update_builds_argv() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "Update Complete.\n", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.repo_update()
    argv = execute.await_args_list[0].args[0]
    assert argv[1:3] == ["repo", "update"]


# ---------------------------------------------------------------------------
# helm show family + chart schema (issue #151)
# ---------------------------------------------------------------------------


async def test_show_values_builds_argv_and_returns_stdout() -> None:
    cli, execute = _cli()
    execute.return_value = (0, '# Default values\nmode: ""\n', "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.show_values("open-telemetry/opentelemetry-collector", "0.165.0")
    assert out.startswith("# Default values")
    argv = execute.await_args_list[0].args[0]
    assert argv[1:4] == ["show", "values", "open-telemetry/opentelemetry-collector"]
    assert argv[4:6] == ["--version", "0.165.0"]


async def test_show_values_omits_empty_version() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "x: 1\n", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        await cli.show_values("repo/chart", "")
    argv = execute.await_args_list[0].args[0]
    assert "--version" not in argv


async def test_show_readme_builds_argv() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "# Chart README\n", "")
    with mock.patch("korvid.k8s.helmcli._execute", execute):
        out = await cli.show_readme("repo/chart", "1.2.3")
    assert out.startswith("# Chart README")
    argv = execute.await_args_list[0].args[0]
    assert argv[1:4] == ["show", "readme", "repo/chart"]


async def test_show_schema_pulls_chart_and_reads_schema() -> None:
    """`helm show` does not expose values.schema.json: the schema comes from
    `helm pull --untar` into a private temp dir, read and cleaned up."""
    cli, _execute = _cli()
    schema = {
        "required": ["apiVersion", "mode"],
        "properties": {"mode": {"type": "string", "enum": ["daemonset", "deployment"]}},
    }

    async def fake_execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
        assert argv[1] == "pull"
        assert argv[2] == "repo/chart"
        assert "--untar" in argv
        dest = argv[argv.index("--untardir") + 1]
        chart_dir = Path(dest) / "chart"
        chart_dir.mkdir(parents=True)
        (chart_dir / "values.schema.json").write_text(json.dumps(schema))
        return 0, "", ""

    with mock.patch("korvid.k8s.helmcli._execute", side_effect=fake_execute):
        result = await cli.show_schema("repo/chart", "1.2.3")
    assert result == schema


async def test_show_schema_returns_none_when_chart_has_no_schema() -> None:
    cli, _ = _cli()

    async def fake_execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
        dest = argv[argv.index("--untardir") + 1]
        (Path(dest) / "chart").mkdir(parents=True)
        return 0, "", ""

    with mock.patch("korvid.k8s.helmcli._execute", side_effect=fake_execute):
        result = await cli.show_schema("repo/chart", "1.2.3")
    assert result is None


async def test_show_schema_returns_none_on_malformed_schema_json() -> None:
    """A chart shipping broken JSON must not crash the wizard - the schema
    is advisory."""
    cli, _ = _cli()

    async def fake_execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
        dest = argv[argv.index("--untardir") + 1]
        chart_dir = Path(dest) / "chart"
        chart_dir.mkdir(parents=True)
        (chart_dir / "values.schema.json").write_text("{not json")
        return 0, "", ""

    with mock.patch("korvid.k8s.helmcli._execute", side_effect=fake_execute):
        result = await cli.show_schema("repo/chart", "1.2.3")
    assert result is None


async def test_show_schema_returns_none_on_pathologically_nested_json() -> None:
    """Chart schemas are untrusted: JSON nested deep enough to blow the
    parser's recursion limit must degrade to None, not crash."""
    cli, _ = _cli()
    bomb = "[" * 100_000 + "]" * 100_000

    async def fake_execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
        dest = argv[argv.index("--untardir") + 1]
        chart_dir = Path(dest) / "chart"
        chart_dir.mkdir(parents=True)
        (chart_dir / "values.schema.json").write_text(bomb)
        return 0, "", ""

    with mock.patch("korvid.k8s.helmcli._execute", side_effect=fake_execute):
        result = await cli.show_schema("repo/chart", "1.2.3")
    assert result is None


async def test_show_schema_rejects_oversized_schema_files() -> None:
    """values.schema.json is chart-controlled: a huge file must be rejected
    by size before it is ever read into memory - advisory metadata degrades
    to None, it never freezes or bloats the TUI."""
    cli, _ = _cli()

    async def fake_execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
        dest = argv[argv.index("--untardir") + 1]
        chart_dir = Path(dest) / "chart"
        chart_dir.mkdir(parents=True)
        big = '{"required": ["x"], "pad": "' + "a" * 2_000_000 + '"}'
        (chart_dir / "values.schema.json").write_text(big)
        return 0, "", ""

    with mock.patch("korvid.k8s.helmcli._execute", side_effect=fake_execute):
        result = await cli.show_schema("repo/chart", "1.2.3")
    assert result is None


async def test_field_summary_renders_non_string_enums_as_json() -> None:
    """Enum members like null/false must render as JSON (copy-pastable into
    YAML), not Python's None/False spelling."""
    from korvid.k8s.helmcli import required_values_from_schema

    schema = {
        "required": ["flag"],
        "properties": {"flag": {"enum": [None, False, "auto"]}},
    }
    rows = required_values_from_schema(schema)
    assert rows == [("flag", "null | false | auto")]


async def test_required_values_from_schema_extracts_paths_types_and_enums() -> None:
    """Schema -> display rows: top-level required fields with their type or
    enum; nested required objects recurse one level deep."""
    from korvid.k8s.helmcli import required_values_from_schema

    schema = {
        "required": ["mode", "image"],
        "properties": {
            "mode": {"type": "string", "enum": ["daemonset", "deployment", ""]},
            "image": {
                "type": "object",
                "required": ["repository"],
                "properties": {"repository": {"type": "string"}},
            },
            "optional": {"type": "string"},
        },
    }
    rows = required_values_from_schema(schema)
    assert ("mode", "daemonset | deployment") in rows
    assert ("image.repository", "string") in rows
    assert not any(path == "optional" for path, _ in rows)


async def test_required_values_from_schema_handles_missing_or_junk() -> None:
    from korvid.k8s.helmcli import required_values_from_schema

    assert required_values_from_schema(None) == []
    assert required_values_from_schema({}) == []
    assert required_values_from_schema({"required": "not-a-list"}) == []
