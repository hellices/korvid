from collections.abc import Mapping
from os import chmod
from pathlib import Path
from stat import S_IMODE
from types import MappingProxyType

import pytest
import yaml

from korvid.core import config as config_module
from korvid.core import keybinding_config as keybinding_module
from korvid.core.config import ConfigError, KorvidConfig, load_config
from korvid.core.keybinding_config import save_keybindings
from tests.platforms import POSIX, posix_only


@pytest.mark.parametrize(
    "section",
    ["true", "1", "1.5", "false", "0", "''", "[]", "[help]", "[[help, f1]]", "'help: f1'"],
    ids=[
        "boolean",
        "integer",
        "float",
        "false",
        "zero",
        "empty-string",
        "empty-list",
        "list",
        "pairs",
        "string",
    ],
)
def test_malformed_section_loads_safe_defaults_and_remains_explicitly_cleanable(
    tmp_path: Path, section: str
) -> None:
    path = tmp_path / "config.yaml"
    settings = {"favorite_namespaces": ["prod"], "namespace": "default", "log_buffer_lines": 800}
    path.write_text(f"{yaml.safe_dump(settings)}keybindings: {section}\n", encoding="utf-8")
    original = path.read_bytes()

    config = load_config(path)

    assert config.keybindings == {}
    assert config.keybindings_section_rejected
    warnings = [warning for warning in config.warnings if warning.startswith("keybindings:")]
    assert len(warnings) == 1
    assert all(
        fragment in warnings[0] for fragment in ("mapping", "defaults", ":keys", "F8", "F9", "F10")
    )
    assert config.favorite_namespaces == ("prod",)
    assert config.namespace == "default"
    assert config.log_buffer_lines == 800
    assert path.read_bytes() == original

    save_keybindings(path, {})

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == settings
    restarted = load_config(path)
    assert restarted.keybindings == {}
    assert not restarted.keybindings_section_rejected
    assert restarted.warnings == ()


@pytest.mark.parametrize(
    "content", ["", "{}\n", "keybindings: {}\n", "keybindings:\n", "keybindings: null\n"]
)
def test_absent_or_empty_keybinding_sections_do_not_require_cleanup(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()

    config = load_config(path)

    assert config.keybindings == {}
    assert not config.keybindings_section_rejected
    assert config.warnings == ()
    assert path.read_bytes() == original


def test_missing_config_and_default_constructor_do_not_require_keybinding_cleanup(
    tmp_path: Path,
) -> None:
    default = KorvidConfig()
    loaded = load_config(tmp_path / "missing.yaml")

    assert loaded == default
    assert not loaded.keybindings_section_rejected
    assert loaded.keybindings == {}
    assert loaded.warnings == ()


@pytest.mark.parametrize("overrides", [{"help": "f1"}, {"help": 1}])
def test_mapping_entries_remain_available_for_startup_validation(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"keybindings": overrides}), encoding="utf-8")

    config = load_config(path)

    assert config.keybindings == overrides
    assert not config.keybindings_section_rejected
    assert config.warnings == ()


@pytest.fixture
def config_document() -> dict[str, object]:
    return {
        "favorite_namespaces": ["prod", "dev"],
        "namespace": "default",
        "automatic_namespace_state": {
            "enabled": True,
            "contexts": {"production": {"namespace": "prod", "fallback": None}},
            "history": ["prod", "dev"],
        },
        "agent": {
            "active": "main",
            "model_tier": "high",
            "profiles": {
                "main": {"model": "openai/gpt-4o", "options": {"temperature": 0.4}},
                "backup": {"model": "anthropic/claude-sonnet-4-5"},
            },
        },
        "keybindings": {"logs": "l", "quit": "ctrl+q"},
    }


@pytest.fixture
def config_path(tmp_path: Path, config_document: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_document, sort_keys=False), encoding="utf-8")
    return path


def test_save_replaces_only_keybindings(
    config_path: Path, config_document: dict[str, object]
) -> None:
    save_keybindings(config_path, {"logs": "ctrl+g"})

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["favorite_namespaces"] == ["prod", "dev"]
    assert saved["keybindings"] == {"logs": "ctrl+g"}
    assert saved == {**config_document, "keybindings": {"logs": "ctrl+g"}}


def test_reset_removes_only_keybindings(
    config_path: Path, config_document: dict[str, object]
) -> None:
    save_keybindings(config_path, {"logs": "ctrl+g"})
    save_keybindings(config_path, {})

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "keybindings" not in saved
    assert saved == {key: value for key, value in config_document.items() if key != "keybindings"}


def test_reset_without_keybindings_preserves_document(
    config_path: Path, config_document: dict[str, object]
) -> None:
    document = {key: value for key, value in config_document.items() if key != "keybindings"}
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    save_keybindings(config_path, {})

    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == document


@pytest.mark.parametrize("overrides", [{"logs": "ctrl+g"}, {}], ids=["save", "reset"])
def test_save_rereads_latest_document(config_path: Path, overrides: Mapping[str, str]) -> None:
    save_keybindings(config_path, {"logs": "ctrl+g"})
    latest = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    latest["favorite_namespaces"] = ["dev", "staging"]
    latest["automatic_namespace_state"]["contexts"]["production"]["namespace"] = "staging"
    latest["agent"]["profiles"]["main"]["options"]["temperature"] = 0.7
    latest["new_setting"] = {"enabled": False}
    config_path.write_text(yaml.safe_dump(latest), encoding="utf-8")

    save_keybindings(config_path, overrides)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved == {
        **{key: value for key, value in latest.items() if key != "keybindings"},
        **({"keybindings": dict(overrides)} if overrides else {}),
    }


@pytest.mark.parametrize(
    "content",
    [
        "",
        " \n",
        "# No overrides yet.\n",
        "# null\n# !!null null\n",
        "---\n",
        "---\n# No overrides yet.\n...\n",
        "%YAML 1.1\n---\n",
        "{}\n",
    ],
)
@pytest.mark.parametrize("overrides", [{"logs": "ctrl+g"}, {}], ids=["save", "reset"])
def test_save_accepts_empty_config(
    tmp_path: Path, content: str, overrides: Mapping[str, str]
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    save_keybindings(path, overrides)

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == (
        {"keybindings": dict(overrides)} if overrides else {}
    )


@pytest.mark.parametrize(
    "content", ["favorite_namespaces: [prod, dev\n", "config: !unsafe value\n"]
)
def test_save_rejects_malformed_yaml_without_changing_bytes(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(ConfigError, match="YAML") as error:
        save_keybindings(path, {"logs": "ctrl+g"})

    assert "\n" not in str(error.value)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content", ["[]\n", "- prod\n", "plain text\n", "0\n", "false\n", "''\n"])
def test_save_rejects_non_mapping_yaml_without_changing_bytes(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(ConfigError, match="mapping"):
        save_keybindings(path, {"logs": "ctrl+g"})

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "content",
    [
        "null\n",
        "Null\n",
        "NULL\n",
        "~\n",
        "!!null null\n",
        "!!null\n",
        "!!null ''\n",
        "!<tag:yaml.org,2002:null>\n",
        "---\n!!null ~\n...\n",
        "&root\n",
    ],
)
@pytest.mark.parametrize("overrides", [{"logs": "ctrl+g"}, {}], ids=["save", "reset"])
def test_explicit_null_roots_never_reach_writer_or_change_bytes(
    tmp_path: Path,
    content: str,
    overrides: Mapping[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    writes: list[Path] = []
    original_writer = config_module._atomic_write_text

    def record_write(destination: Path, serialized: str) -> None:
        writes.append(destination)
        original_writer(destination, serialized)

    monkeypatch.setattr(keybinding_module, "_atomic_write_text", record_write)

    with pytest.raises(ConfigError, match="mapping"):
        save_keybindings(path, overrides)

    assert writes == []
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_save_propagates_read_failure_without_changing_bytes(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_path.read_bytes()

    def fail_read(path: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("config read denied")

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(PermissionError, match="config read denied"):
        save_keybindings(config_path, {"logs": "ctrl+g"})

    assert config_path.read_bytes() == original
    assert list(config_path.parent.iterdir()) == [config_path]


@pytest.mark.parametrize("overrides", [{"logs": "ctrl+g"}, {}], ids=["save", "reset"])
def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(
    config_path: Path, overrides: Mapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent
        assert destination == config_path
        assert destination.read_bytes() == original
        assert source.is_file()
        raise OSError("atomic replace failed")

    monkeypatch.setattr(config_module, "os_replace", fail_replace)

    with pytest.raises(OSError, match="atomic replace failed"):
        save_keybindings(config_path, overrides)

    assert config_path.read_bytes() == original
    assert list(config_path.parent.iterdir()) == [config_path]


def test_failed_fsync_preserves_original_and_cleans_temporary_file(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_path.read_bytes()

    def fail_fsync(descriptor: int) -> None:
        raise OSError("config sync failed")

    monkeypatch.setattr(config_module, "os_fsync", fail_fsync)

    with pytest.raises(OSError, match="config sync failed"):
        save_keybindings(config_path, {"logs": "ctrl+g"})

    assert config_path.read_bytes() == original
    assert list(config_path.parent.iterdir()) == [config_path]


def test_failed_serialization_preserves_original_without_creating_temporary_file(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_path.read_bytes()

    def fail_dump(document: object, **kwargs: object) -> str:
        raise yaml.representer.RepresenterError("config serialization failed")

    monkeypatch.setattr(yaml, "safe_dump", fail_dump)

    with pytest.raises(yaml.representer.RepresenterError, match="config serialization failed"):
        save_keybindings(config_path, {"logs": "ctrl+g"})

    assert config_path.read_bytes() == original
    assert list(config_path.parent.iterdir()) == [config_path]


@pytest.mark.parametrize("overrides", [{"logs": "ctrl+g"}, {}], ids=["save", "reset"])
def test_missing_config_creates_usable_restrictive_file(
    tmp_path: Path, overrides: Mapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "new" / "nested" / "config.yaml"
    requested_modes: list[int] = []

    def record_chmod(temporary_path: Path, mode: int) -> None:
        requested_modes.append(mode)
        chmod(temporary_path, mode)

    monkeypatch.setattr(config_module, "os_chmod", record_chmod)

    save_keybindings(path, overrides)

    assert load_config(path).keybindings == dict(overrides)
    assert requested_modes == [0o600]
    assert list(path.parent.iterdir()) == [path]
    if POSIX:
        assert S_IMODE(path.stat().st_mode) == 0o600


@posix_only(reason="POSIX permission bits are not represented on Windows")
def test_save_preserves_existing_restrictive_file_mode(config_path: Path) -> None:
    config_path.chmod(0o600)

    save_keybindings(config_path, {"logs": "ctrl+g"})

    assert S_IMODE(config_path.stat().st_mode) == 0o600


def test_save_accepts_read_only_mapping(config_path: Path) -> None:
    overrides = MappingProxyType({"logs": "ctrl+g"})

    save_keybindings(config_path, overrides)

    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["keybindings"] == dict(overrides)
