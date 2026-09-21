"""Narrow live-keymap boundary and its Textual application adapter."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from korvid.ui.app import KorvidApp


class KeybindingSurface(ABC):
    """Effective overrides and the one synchronous installation operation."""

    @abstractmethod
    def overrides(self) -> dict[str, str]:
        """Return a detached snapshot of the effective overrides."""

    @abstractmethod
    def install(self, overrides: Mapping[str, str], keymap: Mapping[str, str]) -> None:
        """Install a preflighted complete keymap and update all label consumers."""


class AppKeybindingSurface(KeybindingSurface):
    """Keep application configuration, dispatch, and displayed keys together."""

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def overrides(self) -> dict[str, str]:
        return dict(self._app._keybinding_overrides)

    def install(self, overrides: Mapping[str, str], keymap: Mapping[str, str]) -> None:
        config = dataclasses.replace(self._app.config, keybindings=dict(overrides))
        self._app.config = config
        self._app._keybinding_overrides = dict(overrides)
        self._app.set_keymap(dict(keymap))
