"""Atomic persistence for keybinding overrides in the shared configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from korvid.core.config import _atomic_write_text
from korvid.core.config_store import read_config_document


def save_keybindings(path: Path, overrides: Mapping[str, str]) -> None:
    """Save overrides while preserving unrelated settings from the latest file.

    Args:
        path: The shared configuration file to update.
        overrides: Action-to-key overrides, or an empty mapping to reset them.

    Raises:
        ConfigError: The current file is not UTF-8 text, is malformed YAML,
            or its root is not a mapping.
        OSError: Reading or atomically writing the configuration fails.
    """
    document = read_config_document(path, action="save keybindings")
    if overrides:
        document["keybindings"] = dict(overrides)
    else:
        document.pop("keybindings", None)
    serialized = yaml.safe_dump(document, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, serialized)
