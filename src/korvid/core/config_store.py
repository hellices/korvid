"""Read-modify-write persistence for the user's config.yaml.

`load_config` parses the file once, at startup. A writer here re-reads the
latest file at save time and changes only its own keys, so whatever the
operator edited by hand while korvid was running survives the save.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from korvid.core.config import ConfigError, _atomic_write_text


def read_config_document(path: Path, *, action: str) -> dict[str, Any]:
    """The latest config.yaml as a mapping a writer may update and save.

    A missing, empty or comment-only file is an empty mapping. A document
    that could not be written back without losing the operator's content
    is refused, and the refusal never quotes the file: a YAML error repeats
    the offending line, which may hold a secret.

    Args:
        path: The shared configuration file.
        action: What the caller saves, for the refusal ("save keybindings").

    Returns:
        The parsed document, which the caller may modify.

    Raises:
        ConfigError: The file is not UTF-8 text, is malformed YAML, or its
            root is not a mapping (an explicit `null` included).
        OSError: Reading the file failed for a reason other than absence.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Cannot {action}: config is not UTF-8 text") from exc
    try:
        document = yaml.safe_load(content)
        if document is None:
            node = yaml.compose(content, Loader=yaml.SafeLoader)
            if node is None or node.start_mark.index == node.end_mark.index:
                document = {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot {action}: config contains malformed YAML") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"Cannot {action}: config must be a mapping")
    return document


def save_topbar_state(path: Path, *, expanded: bool) -> None:
    """Persist the top bar collapse/expand choice (issue #142), preserving
    every other key.

    Raises:
        ConfigError: The current file cannot be updated safely; see
            `read_config_document`.
        OSError: Reading or atomically writing the configuration failed.
    """
    document = read_config_document(path, action="save the top bar state")
    existing = document.get("ui")
    ui: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    ui["topbar"] = "expanded" if expanded else "collapsed"
    document["ui"] = ui
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(document, sort_keys=False))
