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
