"""Tests for the helm CLI wrapper (issue #31).

`_execute` is patched so no real helm binary is needed; one test runs the
real `_execute` against `/bin/echo`-style stand-ins to keep the subprocess
plumbing honest.
"""

from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

from korvid.k8s.helmcli import ChartHit, HelmCLI, HelmError, find_helm

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
