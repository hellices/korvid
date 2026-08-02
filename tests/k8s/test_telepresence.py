"""TelepresenceCLI (issue #159 phase 1): detection + status/intercept
parsing over the optional `telepresence` binary, HelmCLI-style (fixed argv,
no shell, timeout). JSON shapes verified against OSS v2.30 source
(pkg/client/cli/cmd/status.go, list.go) and a live v2.30.1 binary."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

from korvid.k8s.telepresence import (
    ActiveIntercept,
    TelepresenceCLI,
    TelepresenceError,
    TelepresenceStatus,
    find_telepresence,
    parse_intercepts,
    parse_status,
)

#: Live capture from `telepresence status --format json`, daemons stopped.
_DISCONNECTED = (
    '{"user_daemon":{"in_docker":false,"daemon_port":0,"running":false},'
    '"root_daemon":{"managed":false,"running":false,"api_version":0},'
    '"traffic_manager":{"name":"","traffic_agent":"","version":""}}'
)

_CONNECTED = json.dumps(
    {
        "user_daemon": {
            "running": True,
            "name": "OSS User Daemon",
            "version": "2.30.1",
            "status": "Connected",
            "kubernetes_context": "minikube",
            "namespace": "default",
            "manager_namespace": "ambassador",
            "intercepts": [{"name": "web", "client": "alice@laptop"}],
        },
        "root_daemon": {"running": True, "version": "2.30.1"},
        "traffic_manager": {
            "name": "traffic-manager",
            "version": "2.30.1",
            "traffic_agent": "ghcr.io/telepresenceio/tel2:2.30.1",
        },
    }
)

_LIST = json.dumps(
    [
        {
            "name": "web",
            "namespace": "default",
            "workload_resource_type": "Deployment",
            "agent_version": "2.30.1",
            "intercept_info": [
                {
                    "spec": {
                        "name": "web-intercept",
                        "client": "alice@laptop",
                        "service_port": 8080,
                        "target_port": 8080,
                    },
                    "disposition": 1,
                }
            ],
        },
        {"name": "api", "namespace": "default", "workload_resource_type": "Deployment"},
    ]
)


# ---------------------------------------------------------------------------
# pure parsers
# ---------------------------------------------------------------------------


def test_parse_status_disconnected() -> None:
    s = parse_status(json.loads(_DISCONNECTED))
    assert isinstance(s, TelepresenceStatus)
    assert s.connected is False
    assert s.user_running is False
    assert s.root_running is False
    assert s.traffic_manager_version == ""
    assert s.error == ""


def test_parse_status_connected() -> None:
    s = parse_status(json.loads(_CONNECTED))
    assert s.connected is True
    assert s.user_running is True
    assert s.root_running is True
    assert s.version == "2.30.1"
    assert s.kubernetes_context == "minikube"
    assert s.traffic_manager_version == "2.30.1"


def test_parse_status_cli_error_shapes() -> None:
    """The CLI reports failures as {'error': …} or {'cmd': …, 'err': …}
    depending on the path (both observed live on v2.30.1)."""
    a = parse_status({"error": "rootd/daemon.json: file does not exist"})
    assert a.connected is False
    assert "daemon.json" in a.error
    b = parse_status({"cmd": "status", "err": "root daemon: not running"})
    assert b.connected is False
    assert "not running" in b.error


def test_parse_status_hostile_shapes_never_raise() -> None:
    payloads: list[Any] = [{}, {"user_daemon": "nope"}, {"user_daemon": {"running": "yes"}}, []]
    for payload in payloads:
        s = parse_status(payload)
        assert s.connected is False


def test_parse_intercepts_extracts_active_rows() -> None:
    rows = parse_intercepts(json.loads(_LIST))
    assert rows == [
        ActiveIntercept(
            workload="web",
            namespace="default",
            kind="Deployment",
            name="web-intercept",
            client="alice@laptop",
            port="8080",
        )
    ]


def test_parse_intercepts_tolerates_junk() -> None:
    assert parse_intercepts([]) == []
    assert parse_intercepts({"error": "not connected"}) == []
    assert parse_intercepts([{"intercept_info": "nope"}, "junk"]) == []


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------


def _cli() -> tuple[TelepresenceCLI, mock.AsyncMock]:
    execute = mock.AsyncMock(return_value=(0, "", ""))
    return TelepresenceCLI("/opt/homebrew/bin/telepresence"), execute


async def test_status_builds_argv_and_parses() -> None:
    cli, execute = _cli()
    execute.return_value = (0, _CONNECTED, "")
    with mock.patch("korvid.k8s.telepresence._execute", execute):
        s = await cli.status()
    assert s.connected is True
    argv = execute.await_args_list[0].args[0]
    assert argv == ["/opt/homebrew/bin/telepresence", "status", "--format", "json"]


async def test_list_intercepts_builds_argv_and_parses() -> None:
    cli, execute = _cli()
    execute.return_value = (0, _LIST, "")
    with mock.patch("korvid.k8s.telepresence._execute", execute):
        rows = await cli.list_intercepts()
    assert len(rows) == 1
    argv = execute.await_args_list[0].args[0]
    assert argv == ["/opt/homebrew/bin/telepresence", "list", "--intercepts", "--format", "json"]


async def test_nonzero_exit_raises_telepresence_error() -> None:
    cli, execute = _cli()
    execute.return_value = (1, "", "connector: connection refused")
    with (
        mock.patch("korvid.k8s.telepresence._execute", execute),
        pytest.raises(TelepresenceError, match="connection refused"),
    ):
        await cli.status()


async def test_invalid_json_raises_telepresence_error() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "not json at all", "")
    with (
        mock.patch("korvid.k8s.telepresence._execute", execute),
        pytest.raises(TelepresenceError, match="unexpected telepresence output"),
    ):
        await cli.status()


def test_find_telepresence_uses_which() -> None:
    with mock.patch("korvid.k8s.telepresence.shutil.which", return_value="/x/telepresence"):
        assert find_telepresence() == "/x/telepresence"
    with mock.patch("korvid.k8s.telepresence.shutil.which", return_value=None):
        assert find_telepresence() is None


def test_parse_status_containerized_daemon_shape() -> None:
    """Docker-hosted daemons report one combined top-level 'daemon' object
    instead of user_daemon/root_daemon (status.go's InDocker branch)."""
    s = parse_status(
        {
            "daemon": {
                "running": True,
                "status": "Connected",
                "version": "2.30.1",
                "kubernetes_context": "prod",
            },
            "traffic_manager": {"name": "traffic-manager", "version": "2.30.1"},
        }
    )
    assert s.connected is True
    assert s.user_running is True
    assert s.root_running is True  # one combined daemon serves both roles
    assert s.kubernetes_context == "prod"
    assert s.traffic_manager_version == "2.30.1"


def test_parse_status_multi_daemon_connections_shape() -> None:
    """--use / multi-daemon mode wraps everything in {'connections': […]};
    the connected entry wins over a disconnected one."""
    disconnected = {
        "user_daemon": {"running": False},
        "root_daemon": {"running": False},
        "traffic_manager": {"name": "", "version": "", "traffic_agent": ""},
    }
    connected = {
        "user_daemon": {
            "running": True,
            "status": "Connected",
            "version": "2.30.1",
            "kubernetes_context": "prod",
        },
        "root_daemon": {"running": True},
        "traffic_manager": {"name": "traffic-manager", "version": "2.30.1"},
    }
    s = parse_status({"connections": [disconnected, connected]})
    assert s.connected is True
    assert s.kubernetes_context == "prod"
    empty = parse_status({"connections": []})
    assert empty.connected is False
    junk = parse_status({"connections": "nope"})
    assert junk.connected is False


def test_nested_connections_bomb_does_not_recurse() -> None:
    """A connections entry embedding another connections wrapper must not
    recurse (the 'Never raises' contract covers RecursionError): nested
    wrappers are ignored, one level is the source's real shape."""
    bomb: dict[str, Any] = {"connections": []}
    inner = bomb
    for _ in range(2000):
        nxt: dict[str, Any] = {"connections": []}
        inner["connections"] = [nxt]
        inner = nxt
    s = parse_status(bomb)
    assert s.connected is False


def test_multi_daemon_status_carries_the_daemon_name() -> None:
    """list needs --use <name> when several daemons run: the selected
    connection's daemon name must survive parsing."""
    s = parse_status(
        {
            "connections": [
                {
                    "user_daemon": {
                        "running": True,
                        "status": "Connected",
                        "name": "prod-conn",
                    },
                    "root_daemon": {"running": True},
                    "traffic_manager": {"version": "2.30.1"},
                }
            ]
        }
    )
    assert s.daemon_name == "prod-conn"


async def test_list_intercepts_scopes_to_the_daemon_when_named() -> None:
    cli, execute = _cli()
    execute.return_value = (0, "[]", "")
    with mock.patch("korvid.k8s.telepresence._execute", execute):
        await cli.list_intercepts(daemon="prod-conn")
    argv = execute.await_args_list[0].args[0]
    assert argv == [
        "/opt/homebrew/bin/telepresence",
        "list",
        "--intercepts",  # unfiltered list can reach 20MB on large clusters
        "--use",
        "^prod\\-conn$",  # escaped + anchored: --use is a Go regexp match
        "--format",
        "json",
    ]


def test_parse_intercepts_requires_active_disposition_and_spec() -> None:
    """WAITING/errored intercepts and spec-less entries are not 'Active
    intercepts': disposition 1 (ACTIVE) with a real spec is the bar."""
    payload = [
        {
            "name": "web",
            "namespace": "d",
            "intercept_info": [
                {"spec": {"name": "waiting"}, "disposition": 2},
                {"disposition": 1},  # no spec: a blank row helps nobody
                {"spec": {"name": "live", "client": "a@b"}, "disposition": 1},
            ],
        }
    ]
    rows = parse_intercepts(payload)
    assert [r.name for r in rows] == ["live"]


async def test_daemon_selector_is_escaped_and_anchored() -> None:
    """--use is a Go regexp match: 'prod' would also match 'prod2', and
    dots match anything - the name must be escaped and anchored."""
    cli, execute = _cli()
    execute.return_value = (0, "[]", "")
    with mock.patch("korvid.k8s.telepresence._execute", execute):
        await cli.list_intercepts(daemon="prod.us-1")
    argv = execute.await_args_list[0].args[0]
    use = argv[argv.index("--use") + 1]
    assert use.startswith("^")
    assert use.endswith("$")
    assert "\\." in use  # the dot matches a literal dot only


async def test_formatted_failure_error_comes_from_stdout() -> None:
    """--format json writes failures as {'error': …} on stdout with a
    silent stderr: the exit-code fallback must not eat the real reason."""
    cli, execute = _cli()
    execute.return_value = (1, '{"error": "connector: no running daemon"}', "")
    with (
        mock.patch("korvid.k8s.telepresence._execute", execute),
        pytest.raises(TelepresenceError, match="no running daemon"),
    ):
        await cli.status()
