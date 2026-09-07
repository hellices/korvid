from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

import korvid.mcp.registry as registry
from korvid.mcp.registry import (
    EndpointRegistryError,
    TUIEndpoint,
    default_endpoint_path,
    open_private_file,
    read_endpoints,
    select_endpoint,
)

_CAPABILITY = "abcdefghijklmnopqrstuvwxyzABCDEF"


def _entry(
    *,
    pid: int | None = None,
    port: int = 7878,
    url: str | None = None,
    capability: object = _CAPABILITY,
) -> dict[str, object]:
    endpoint_pid = pid if pid is not None else os.getpid()
    return {
        "pid": endpoint_pid,
        "port": port,
        "url": url or f"http://127.0.0.1:{port}/mcp",
        "capability": capability,
    }


def _write_registry(path: Path, servers: dict[str, object]) -> None:
    _write_private_text(path, json.dumps({"servers": servers}))


def _write_private_text(path: Path, text: str) -> None:
    _write_private_bytes(path, text.encode())


def _write_private_bytes(path: Path, data: bytes) -> None:
    fd = open_private_file(path)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def test_default_endpoint_path_uses_xdg_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/private/state")
    assert default_endpoint_path() == Path("/private/state/korvid/mcp-endpoint.json")


def test_default_endpoint_path_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert (
        default_endpoint_path() == Path.home() / ".local" / "state" / "korvid" / "mcp-endpoint.json"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX creation semantics")
def test_open_private_file_creates_an_exclusive_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    fd = open_private_file(path)
    try:
        os.write(fd, b"secret")
    finally:
        os.close(fd)

    assert path.read_bytes() == b"secret"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError, match=r"private\.json"):
        open_private_file(path)


def test_open_private_file_uses_the_windows_security_api_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private.json"
    monkeypatch.setattr(registry, "_WINDOWS", True)
    monkeypatch.setattr(registry, "_windows_open_private_file", lambda candidate: 42)

    def forbidden_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("Windows private creation must not use os.open")

    monkeypatch.setattr(os, "open", forbidden_open)
    assert open_private_file(path) == 42


def test_open_private_file_propagates_windows_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private.json"
    monkeypatch.setattr(registry, "_WINDOWS", True)

    def fail_creation(candidate: Path) -> int:
        raise PermissionError("security descriptor refused")

    monkeypatch.setattr(registry, "_windows_open_private_file", fail_creation)
    with pytest.raises(PermissionError, match="security descriptor refused"):
        open_private_file(path)


def test_windows_private_creation_removes_file_when_acl_is_permissive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private.json"
    opened_fd: int | None = None

    def create_unprotected(candidate: Path) -> int:
        nonlocal opened_fd
        opened_fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return opened_fd

    monkeypatch.setattr(registry, "_WINDOWS", True)
    monkeypatch.setattr(
        registry,
        "_windows_create_private_fd",
        create_unprotected,
        raising=False,
    )
    monkeypatch.setattr(registry, "_windows_fd_is_private", lambda fd: False)

    with pytest.raises(PermissionError, match=r"Windows ACL.*not private"):
        open_private_file(path)
    assert not path.exists()
    assert opened_fd is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened_fd)


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL integration")
def test_windows_private_file_round_trip_has_an_accepted_acl(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    fd = open_private_file(path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"servers": {str(os.getpid()): _entry()}}, handle)

    assert read_endpoints(path)[0].pid == os.getpid()
    with pytest.raises(FileExistsError, match=r"mcp-endpoint\.json"):
        open_private_file(path)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS ACL integration")
def test_macos_inherited_acl_is_rejected_and_private_creation_clears_it(
    tmp_path: Path,
) -> None:
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    result = subprocess.run(
        ["chmod", "+a", "everyone allow read,write,file_inherit", str(inherited)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip("test filesystem does not support macOS inherited ACLs")

    unsafe = inherited / "unsafe.json"
    unsafe.write_text(json.dumps({"servers": {}}))
    unsafe.chmod(0o600)
    unsafe_fd = os.open(unsafe, os.O_RDONLY)
    try:
        if registry._darwin_fd_is_private(unsafe_fd):
            pytest.skip("test filesystem did not inherit the parent ACL")
    finally:
        os.close(unsafe_fd)
    with pytest.raises(EndpointRegistryError, match=r"macOS ACL.*not private"):
        read_endpoints(unsafe)

    private = inherited / "private.json"
    _write_registry(private, {str(os.getpid()): _entry()})
    assert read_endpoints(private)[0].pid == os.getpid()


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS ACL integration")
def test_macos_private_creation_never_clears_acl_on_a_public_file_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    result = subprocess.run(
        ["chmod", "+a", "everyone allow read,write,file_inherit", str(inherited)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip("test filesystem does not support macOS inherited ACLs")

    def reject_post_creation_acl_mutation(fd: int) -> None:
        raise AssertionError("credential privacy must be supplied atomically at creation")

    monkeypatch.setattr(
        registry,
        "_darwin_clear_acl",
        reject_post_creation_acl_mutation,
        raising=False,
    )
    path = inherited / "private.json"
    fd = open_private_file(path)
    try:
        assert os.fstat(fd).st_mode & 0o077 == 0
        assert registry._darwin_fd_is_private(fd)
    finally:
        os.close(fd)


def test_endpoint_is_frozen_and_hides_capability_from_repr() -> None:
    endpoint = TUIEndpoint(
        pid=123,
        port=7878,
        url="http://127.0.0.1:7878/mcp",
        capability=_CAPABILITY,
    )
    assert _CAPABILITY not in repr(endpoint)
    with pytest.raises(dataclasses.FrozenInstanceError, match="cannot assign"):
        endpoint.port = 9999  # type: ignore[misc]  # verifies the public value is immutable


def test_read_endpoints_reads_a_live_private_registry(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {str(os.getpid()): _entry()})
    assert read_endpoints(path) == [
        TUIEndpoint(
            pid=os.getpid(),
            port=7878,
            url="http://127.0.0.1:7878/mcp",
            capability=_CAPABILITY,
        )
    ]


def test_read_endpoints_accepts_the_optional_trailing_slash(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(
        path,
        {
            str(os.getpid()): _entry(
                url="http://127.0.0.1:7878/mcp/",
            )
        },
    )
    assert read_endpoints(path)[0].url == "http://127.0.0.1:7878/mcp/"


def test_read_endpoints_reports_a_missing_registry_actionably(tmp_path: Path) -> None:
    with pytest.raises(EndpointRegistryError, match="start the korvid TUI"):
        read_endpoints(tmp_path / "missing.json")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file type and symlink semantics")
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_read_endpoints_rejects_non_regular_paths_before_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        _write_registry(target, {})
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, 0o600)

    original_open = os.open

    def guarded_open(file: Any, flags: int, mode: int = 0o600) -> int:
        if Path(os.fsdecode(file)) == path:
            raise AssertionError("unsafe path reached os.open")
        return int(original_open(file, flags, mode))

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(EndpointRegistryError, match="unsafe"):
        read_endpoints(path)


def test_read_endpoints_rejects_files_over_256_kib(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_private_bytes(path, b"x" * (256 * 1024 + 1))
    with pytest.raises(EndpointRegistryError, match="too large"):
        read_endpoints(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_read_endpoints_rejects_group_or_world_permissions(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {})
    path.chmod(0o640)
    with pytest.raises(EndpointRegistryError, match="private"):
        read_endpoints(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX effective ownership")
def test_read_endpoints_rejects_a_foreign_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {})
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(EndpointRegistryError, match="owner"):
        read_endpoints(path)


def test_read_endpoints_rejects_a_permissive_windows_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {})
    monkeypatch.setattr(registry, "_WINDOWS", True)
    monkeypatch.setattr(registry, "_windows_fd_is_private", lambda fd: False)
    with pytest.raises(EndpointRegistryError, match=r"Windows ACL.*not private"):
        read_endpoints(path)


def test_read_endpoints_fails_closed_when_windows_acl_cannot_be_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {})
    monkeypatch.setattr(registry, "_WINDOWS", True)

    def fail_validation(fd: int) -> bool:
        raise OSError("security API unavailable")

    monkeypatch.setattr(registry, "_windows_fd_is_private", fail_validation)
    with pytest.raises(EndpointRegistryError, match=r"Cannot verify.*Windows ACL"):
        read_endpoints(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX inode snapshot")
def test_read_endpoints_rejects_a_file_replaced_between_check_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    replacement = tmp_path / "replacement.json"
    _write_registry(path, {})
    _write_registry(replacement, {})
    original_open = os.open
    replaced = False

    def replacing_open(file: Any, flags: int, mode: int = 0o600) -> int:
        nonlocal replaced
        if Path(os.fsdecode(file)) == path and not replaced:
            replacement.replace(path)
            replaced = True
        return int(original_open(file, flags, mode))

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(EndpointRegistryError, match="changed"):
        read_endpoints(path)


@pytest.mark.parametrize(
    "text",
    [
        '{"servers": {',
        '{"servers": {}, "servers": {}}',
        '{"servers": NaN}',
        '{"servers": {}} trailing',
    ],
)
def test_read_endpoints_rejects_non_strict_json(tmp_path: Path, text: str) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_private_text(path, text)
    with pytest.raises(EndpointRegistryError, match="invalid"):
        read_endpoints(path)


def test_read_endpoints_rejects_non_utf8_json_without_echoing_content(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_private_bytes(path, b'{"servers":{"secret-\xff":{}}}')
    with pytest.raises(EndpointRegistryError, match="invalid") as error:
        read_endpoints(path)
    assert "secret" not in str(error.value)


def test_read_endpoints_rejects_deep_json_with_a_fixed_error(tmp_path: Path) -> None:
    path = tmp_path / "mcp-endpoint.json"
    secret = "do-not-echo-deep-content"
    _write_private_text(path, "[" * 2_000 + f'"{secret}"' + "]" * 2_000)
    with pytest.raises(EndpointRegistryError, match="invalid") as error:
        read_endpoints(path)
    assert secret not in str(error.value)


def test_parse_json_converts_recursion_errors_to_a_fixed_registry_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_deep_json(*args: object, **kwargs: object) -> object:
        raise RecursionError("content-specific parser detail")

    monkeypatch.setattr(json, "loads", fail_deep_json)
    with pytest.raises(EndpointRegistryError, match="invalid") as error:
        registry._parse_json(b"{}")
    assert "content-specific" not in str(error.value)


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"servers": {}, "extra": True},
        {"servers": []},
        {"servers": {"123": {**_entry(pid=123), "extra": True}}},
        {"servers": {"0123": _entry(pid=123)}},
        {"servers": {"124": _entry(pid=123)}},
    ],
)
def test_read_endpoints_rejects_an_invalid_registry_shape(
    tmp_path: Path,
    document: object,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_private_text(path, json.dumps(document))
    with pytest.raises(EndpointRegistryError, match="invalid"):
        read_endpoints(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", True),
        ("pid", 0),
        ("pid", -1),
        ("pid", "123"),
        ("port", True),
        ("port", 0),
        ("port", 65536),
        ("port", "7878"),
        ("url", 123),
    ],
)
def test_read_endpoints_rejects_invalid_endpoint_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    entry = _entry(pid=123)
    entry[field] = value
    _write_registry(path, {"123": entry})
    with pytest.raises(EndpointRegistryError, match="registry"):
        read_endpoints(path)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:7878/mcp",
        "http://localhost:7878/mcp",
        "http://0.0.0.0:7878/mcp",
        "http://127.0.0.1/mcp",
        "http://127.0.0.1:9999/mcp",
        "http://user@127.0.0.1:7878/mcp",
        "http://127.0.0.1:7878/mcp?next=foreign",
        "http://127.0.0.1:7878/mcp#fragment",
        "http://127.0.0.1:7878/",
        "http://127.0.0.1:7878/%6dcp",
        "http://127.0.0.1:7878/mcp//",
    ],
)
def test_read_endpoints_rejects_noncanonical_or_foreign_urls(
    tmp_path: Path,
    url: str,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {"123": _entry(pid=123, url=url)})
    with pytest.raises(EndpointRegistryError, match="loopback"):
        read_endpoints(path)


@pytest.mark.parametrize(
    "capability",
    [
        None,
        True,
        123,
        "",
        " " * 32,
        "a" * 31,
        "a" * 31 + "\n",
        "é" * 32,
    ],
)
def test_read_endpoints_rejects_weak_or_non_ascii_capabilities(
    tmp_path: Path,
    capability: object,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {"123": _entry(pid=123, capability=capability)})
    with pytest.raises(EndpointRegistryError, match="authenticated") as error:
        read_endpoints(path)
    if capability:
        assert str(capability) not in str(error.value)


def test_read_endpoints_tells_users_to_restart_live_pre_capability_tuis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    old_entry = _entry(pid=123)
    del old_entry["capability"]
    _write_registry(path, {"123": old_entry})
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)
    with pytest.raises(EndpointRegistryError, match=r"restart.*authenticated"):
        read_endpoints(path)


def test_read_endpoints_ignores_dead_pre_capability_tuis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    old_entry = _entry(pid=123)
    del old_entry["capability"]
    _write_registry(
        path,
        {
            "123": old_entry,
            "456": _entry(pid=456, port=7879),
        },
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 456)
    assert read_endpoints(path) == [
        TUIEndpoint(456, 7879, "http://127.0.0.1:7879/mcp", _CAPABILITY)
    ]


def test_read_endpoints_validates_every_entry_before_filtering_dead_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    unsafe = _entry(pid=999_999, url="http://attacker.example/mcp")
    _write_registry(path, {str(os.getpid()): _entry(), "999999": unsafe})
    checked: list[int] = []

    def record_liveness(pid: int) -> bool:
        checked.append(pid)
        return pid == os.getpid()

    monkeypatch.setattr(registry, "_pid_alive", record_liveness)
    with pytest.raises(EndpointRegistryError, match="loopback"):
        read_endpoints(path)
    assert checked == []


def test_read_endpoints_rejects_a_pid_too_large_for_os_process_apis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    huge_pid = 2**63
    _write_registry(path, {str(huge_pid): _entry(pid=huge_pid)})

    def forbidden_liveness(pid: int) -> bool:
        raise AssertionError("out-of-range PID reached an OS process API")

    monkeypatch.setattr(registry, "_pid_alive", forbidden_liveness)
    with pytest.raises(EndpointRegistryError, match="invalid"):
        read_endpoints(path)


def test_read_endpoints_ignores_well_formed_dead_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(
        path,
        {
            "123": _entry(pid=123),
            "456": _entry(pid=456, port=7879),
        },
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 456)
    assert read_endpoints(path) == [
        TUIEndpoint(456, 7879, "http://127.0.0.1:7879/mcp", _CAPABILITY)
    ]


def test_windows_liveness_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_WINDOWS", True)
    monkeypatch.setattr(registry, "_windows_pid_alive", lambda pid: pid == 123)

    def forbidden_kill(pid: int, signal: int) -> None:
        raise AssertionError("os.kill(pid, 0) is unsafe on Windows")

    monkeypatch.setattr(os, "kill", forbidden_kill)
    assert registry._pid_alive(123)
    assert not registry._pid_alive(456)


def test_windows_api_configures_process_handles_for_64_bit_safety() -> None:
    class FakeFunction:
        argtypes: object = None
        restype: object = None

        def __call__(self, *args: object) -> int:
            return 0

    class FakeLibrary:
        def __getattr__(self, name: str) -> FakeFunction:
            function = FakeFunction()
            setattr(self, name, function)
            return function

    kernel32 = FakeLibrary()
    advapi32 = FakeLibrary()
    registry._configure_windows_api(kernel32, advapi32)

    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.OpenProcess.argtypes == [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    assert kernel32.GetExitCodeProcess.argtypes == [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission mutation")
def test_read_endpoints_revalidates_privacy_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mcp-endpoint.json"
    _write_registry(path, {})
    original_read_bounded = registry._read_bounded

    def expose_after_read(fd: int) -> bytes:
        data = original_read_bounded(fd)
        path.chmod(0o644)
        return data

    monkeypatch.setattr(registry, "_read_bounded", expose_after_read)
    with pytest.raises(EndpointRegistryError, match="private"):
        read_endpoints(path)


def test_select_endpoint_automatically_selects_the_only_instance() -> None:
    endpoint = TUIEndpoint(123, 7878, "http://127.0.0.1:7878/mcp", _CAPABILITY)
    assert select_endpoint([endpoint]) is endpoint


def test_select_endpoint_requires_a_running_tui() -> None:
    with pytest.raises(EndpointRegistryError, match=r"start the TUI.*enable MCP"):
        select_endpoint([])


def test_select_endpoint_requires_instance_for_multiple_tuis() -> None:
    endpoints = [
        TUIEndpoint(123, 7878, "http://127.0.0.1:7878/mcp", _CAPABILITY),
        TUIEndpoint(456, 7879, "http://127.0.0.1:7879/mcp", _CAPABILITY),
    ]
    with pytest.raises(EndpointRegistryError, match=r"--instance PID"):
        select_endpoint(endpoints)


def test_select_endpoint_selects_the_requested_pid() -> None:
    endpoints = [
        TUIEndpoint(123, 7878, "http://127.0.0.1:7878/mcp", _CAPABILITY),
        TUIEndpoint(456, 7879, "http://127.0.0.1:7879/mcp", _CAPABILITY),
    ]
    assert select_endpoint(endpoints, instance=456) is endpoints[1]


def test_select_endpoint_rejects_a_missing_requested_pid() -> None:
    endpoint = TUIEndpoint(123, 7878, "http://127.0.0.1:7878/mcp", _CAPABILITY)
    with pytest.raises(EndpointRegistryError, match=r"No running.*--instance PID"):
        select_endpoint([endpoint], instance=456)


@pytest.mark.parametrize("instance", [0, -1, True])
def test_select_endpoint_rejects_an_invalid_requested_pid(instance: int) -> None:
    endpoint = TUIEndpoint(1, 7878, "http://127.0.0.1:7878/mcp", _CAPABILITY)
    with pytest.raises(EndpointRegistryError, match=r"valid positive PID.*--instance"):
        select_endpoint([endpoint], instance=instance)
