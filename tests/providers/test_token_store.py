import os
import stat
import sys
import types
from pathlib import Path

import pytest

from korvid.providers.token_store import TokenStore


def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # import keyring -> ImportError


def test_file_fallback_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_keyring(monkeypatch)
    store = TokenStore(fallback_path=tmp_path / "creds.json")
    store.save("github-oauth", "gho_x")
    assert store.load("github-oauth") == "gho_x"
    store.delete("github-oauth")
    assert store.load("github-oauth") is None


def test_file_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_keyring(monkeypatch)
    p = tmp_path / "creds.json"
    TokenStore(fallback_path=p).save("k", "v")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_keyring_preferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, str] = {}
    fake = types.SimpleNamespace(
        set_password=lambda svc, k, v: calls.__setitem__(f"{svc}/{k}", v),
        get_password=lambda svc, k: calls.get(f"{svc}/{k}"),
        delete_password=lambda svc, k: calls.pop(f"{svc}/{k}", None),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    p = tmp_path / "creds.json"
    store = TokenStore(fallback_path=p)
    store.save("k", "v")
    assert store.load("k") == "v"
    assert not p.exists()  # keyring used, no file written


def test_keyring_error_falls_back_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object) -> None:
        raise RuntimeError("no backend")

    fake = types.SimpleNamespace(set_password=boom, get_password=boom, delete_password=boom)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = TokenStore(fallback_path=tmp_path / "creds.json")
    store.save("k", "v")
    assert store.load("k") == "v"


def test_non_object_json_treated_as_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_keyring(monkeypatch)
    p = tmp_path / "creds.json"
    p.write_text("[]")  # valid JSON but not an object
    store = TokenStore(fallback_path=p)
    assert store.load("k") is None
    store.save("k", "v")  # must not crash
    assert store.load("k") == "v"


def test_keyring_save_clears_stale_file_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, str] = {}
    fake = types.SimpleNamespace(
        set_password=lambda svc, k, v: calls.__setitem__(f"{svc}/{k}", v),
        get_password=lambda svc, k: calls.get(f"{svc}/{k}"),
        delete_password=lambda svc, k: calls.pop(f"{svc}/{k}", None),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    p = tmp_path / "creds.json"
    p.write_text('{"k": "old-file-token"}')  # left over from a keyring outage
    store = TokenStore(fallback_path=p)
    store.save("k", "new")
    # The stale file copy must not survive a successful keyring save.
    import json

    assert "k" not in json.loads(p.read_text())
    assert store.load("k") == "new"


def test_failed_keyring_save_removes_stale_keyring_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, str] = {"korvid/k": "old-keyring-token"}

    def boom_set(*a: object) -> None:
        raise RuntimeError("set failed")

    fake = types.SimpleNamespace(
        set_password=boom_set,
        get_password=lambda svc, k: calls.get(f"{svc}/{k}"),
        delete_password=lambda svc, k: calls.pop(f"{svc}/{k}", None),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = TokenStore(fallback_path=tmp_path / "creds.json")
    store.save("k", "new")
    # keyring recovered later must not resurrect the old token via load().
    assert store.load("k") == "new"


def test_failed_file_write_keeps_stale_keyring_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both keyring save and the file write fail, the old keyring copy is
    the only surviving token and must not be deleted."""
    calls: dict[str, str] = {"korvid/k": "old-keyring-token"}

    def boom_set(*a: object) -> None:
        raise RuntimeError("set failed")

    fake = types.SimpleNamespace(
        set_password=boom_set,
        get_password=lambda svc, k: calls.get(f"{svc}/{k}"),
        delete_password=lambda svc, k: calls.pop(f"{svc}/{k}", None),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = TokenStore(fallback_path=tmp_path / "creds.json")

    def boom_write(data: dict[str, str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_file", boom_write)
    with pytest.raises(OSError, match="disk full"):
        store.save("k", "new")
    assert calls["korvid/k"] == "old-keyring-token"  # last copy preserved


def test_interrupted_write_preserves_existing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure mid-write (disk full / crash) must not corrupt the fallback
    file: the previously stored credential has to survive intact."""
    import korvid.providers.token_store as ts_mod

    _no_keyring(monkeypatch)
    p = tmp_path / "creds.json"
    store = TokenStore(fallback_path=p)
    store.save("old", "keep-me")

    def failing_fsync(fd: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(ts_mod, "os_fsync", failing_fsync)
        with pytest.raises(OSError, match="disk full"):
            store.save("new", "value")
    assert store.load("old") == "keep-me"


def test_write_fsyncs_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Data must reach disk before the rename, or a power loss can leave an
    empty/old credentials file behind."""
    import korvid.providers.token_store as ts_mod

    _no_keyring(monkeypatch)
    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        # Windows' fsync (_commit) requires a writable handle: syncing an
        # O_RDONLY fd raises there, so the implementation must sync the fd
        # it wrote through. fcntl itself is POSIX-only, so guard the check.
        if os.name != "nt":
            import fcntl

            assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        calls.append("fsync")
        real_fsync(fd)

    def spy_replace(src: object, dst: object) -> None:
        calls.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]  # spy forwards Path args

    with monkeypatch.context() as m:
        m.setattr(ts_mod, "os_fsync", spy_fsync)
        m.setattr(ts_mod, "os_replace", spy_replace)
        TokenStore(fallback_path=tmp_path / "creds.json").save("k", "v")
    assert calls == ["fsync", "replace"]


def test_write_does_not_clobber_foreign_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file name must be unique so two concurrent processes cannot
    race on (and delete) each other's temp file."""
    _no_keyring(monkeypatch)
    foreign = tmp_path / "creds.json.tmp"
    foreign.write_text("owned by another process")
    TokenStore(fallback_path=tmp_path / "creds.json").save("k", "v")
    assert foreign.read_text() == "owned by another process"
