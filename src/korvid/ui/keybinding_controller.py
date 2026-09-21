"""Shared startup and confirmed-application flow for editable keybindings."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import yaml

from korvid.core.config import ConfigError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.keybinding_catalog import KeybindingCatalog
from korvid.ui.keybinding_surface import KeybindingSurface
from korvid.ui.ui_surface import UiSurface


class KeybindingController:
    """Validate once, persist first, then synchronously activate a whole proposal."""

    def __init__(
        self,
        *,
        catalog: KeybindingCatalog,
        surface: KeybindingSurface,
        ui: UiSurface,
        save: Callable[[Mapping[str, str]], None] | None,
        can_open: Callable[[], UnavailableReason | None],
    ) -> None:
        self.catalog = catalog
        self._surface = surface
        self._ui = ui
        self._save = save
        self._can_open = can_open
        self._cleanup_required = False

    def load(self, raw: Mapping[str, object], *, cleanup_required: bool = False) -> None:
        """Install safe overrides, retaining any rejected-section cleanup metadata."""
        plan = self.catalog.rules.plan(raw)
        self._surface.install(plan.overrides, self.catalog.keymap(plan.overrides))
        self._cleanup_required = cleanup_required or bool(plan.warnings)
        for warning in plan.warnings:
            self._ui.notify(warning, title="Keybindings", severity="warning", markup=False)

    def unavailable_reason(self) -> UnavailableReason | None:
        """Refuse entry over protected UI or without a persistence collaborator."""
        reason = self._can_open()
        if reason is not None:
            return reason
        if self._save is None:
            return UnavailableReason(
                AvailabilityCode.MISSING_CAPABILITY,
                "Keybinding persistence is unavailable in this session",
            )
        return None

    def open_editor(self) -> None:
        """Start one staged session without changing active or saved bindings."""
        reason = self.unavailable_reason()
        if reason is not None:
            self._ui.notify(reason.message, severity=reason.severity, markup=False)
            return
        from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen

        self._ui.push_screen(
            KeybindingEditorScreen(
                self.catalog.rules,
                self._surface.overrides(),
                self.catalog.descriptions,
                apply=self.apply,
                cleanup_required=self._cleanup_required,
            )
        )

    def apply(self, overrides: Mapping[str, str]) -> str | None:
        """Apply only a valid complete proposal after its atomic save succeeds."""
        plan = self.catalog.rules.plan(overrides)
        if plan.warnings:
            return "; ".join(plan.warnings)
        if self._save is None:
            return "Keybinding persistence is unavailable in this session"
        keymap = self.catalog.keymap(plan.overrides)
        try:
            self._save(dict(plan.overrides))
        except (OSError, ConfigError, UnicodeError, yaml.YAMLError) as exc:
            return f"Could not save keybindings: {exc}"
        self._surface.install(plan.overrides, keymap)
        self._cleanup_required = False
        return None
