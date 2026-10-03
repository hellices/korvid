"""The config.yaml writers re-read the latest file and refuse one they
could not write back without losing what the operator put in it."""

from pathlib import Path

import pytest
import yaml

from korvid.core.config import ConfigError
from korvid.core.config_store import read_config_document, save_topbar_state


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b"- prod\n- dev\n", "must be a mapping", id="list-root"),
        pytest.param(b"namespace: [prod\n", "malformed YAML", id="malformed"),
        pytest.param(b"namespace: \xff\xfe\n", "not UTF-8", id="not-utf8"),
    ],
)
def test_topbar_save_refuses_a_document_it_cannot_round_trip(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    """The toggle used to replace a list root with `{ui: ...}`, dropping the
    operator's content, and let YAML errors (which quote the offending
    line) reach the notification."""
    path = tmp_path / "config.yaml"
    path.write_bytes(content)

    with pytest.raises(ConfigError, match=reason):
        save_topbar_state(path, expanded=True)

    assert path.read_bytes() == content


def test_topbar_save_keeps_non_ascii_content_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("namespace: café\n", encoding="utf-8")

    save_topbar_state(path, expanded=True)

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved == {"namespace": "café", "ui": {"topbar": "expanded"}}


@pytest.mark.parametrize("content", ["", "# only a comment\n"])
def test_an_empty_document_reads_as_an_empty_mapping(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    assert read_config_document(path, action="save the top bar state") == {}


def test_a_missing_file_reads_as_an_empty_mapping(tmp_path: Path) -> None:
    assert read_config_document(tmp_path / "absent.yaml", action="save keybindings") == {}
