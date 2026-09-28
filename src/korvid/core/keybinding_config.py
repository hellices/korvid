"""Atomic persistence for keybinding overrides in the shared configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from korvid.core.config import ConfigError, _atomic_write_text


def save_keybindings(path: Path, overrides: Mapping[str, str]) -> None:
    """Save overrides while preserving unrelated settings from the latest file.

    Args:
        path: The shared configuration file to update.
        overrides: Action-to-key overrides, or an empty mapping to reset them.

    Raises:
        ConfigError: The current YAML is malformed or its root is not a mapping.
        OSError: Reading or atomically writing the configuration fails.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    try:
        document = yaml.safe_load(content)
        if document is None:
            node = yaml.compose(content, Loader=yaml.SafeLoader)
            if node is None or node.start_mark.index == node.end_mark.index:
                document = {}
    except yaml.YAMLError as exc:
        raise ConfigError("Cannot save keybindings: config contains malformed YAML") from exc
    if not isinstance(document, dict):
        raise ConfigError("Cannot save keybindings: config must be a mapping")
    if overrides:
        document["keybindings"] = dict(overrides)
    else:
        document.pop("keybindings", None)
    serialized = yaml.safe_dump(document, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, serialized)
