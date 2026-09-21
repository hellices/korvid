"""Derive editing rules and complete keymaps from the real dispatch catalog."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from textual.app import App
from textual.binding import BindingsMap, BindingType
from textual.dom import DOMNode
from textual.screen import Screen
from textual.widgets import DataTable, Input

from korvid.core.keybindings import canonical_key, shift_alias_keys
from korvid.core.keymap_edit import KeymapRules
from korvid.ui.action_policy import action_contexts
from korvid.ui.app_bindings import APP_HANDLER_KEY_HELP, as_binding
from korvid.ui.widgets.action_palette import ActionPaletteScreen


def _key_owners(bindings: Iterable[BindingType]) -> dict[str, str]:
    return {
        canonical_key(key.strip()): binding.action
        for raw in bindings
        for binding in (as_binding(raw),)
        for key in binding.key.split(",")
    }


class KeybindingCatalog:
    """One view of declared bindings, shared by startup and the editor."""

    def __init__(
        self, bindings: Iterable[BindingType], *, modal_keys: Collection[str] = ()
    ) -> None:
        self._bindings = tuple(as_binding(binding) for binding in bindings)
        actions: dict[str, tuple[str, ...]] = {}
        self.descriptions: dict[str, str] = {}
        for binding in self._bindings:
            if binding.id is not None:
                actions[binding.action] = (*actions.get(binding.action, ()), binding.key)
                self.descriptions.setdefault(binding.action, binding.description)
        self.rules = KeymapRules(
            actions=actions,
            priority_actions=frozenset(
                binding.action for binding in self._bindings if binding.priority
            ),
            reserved_keys=self._reserved_keys(),
            priority_reserved_keys=self._priority_keys(modal_keys),
            action_contexts=action_contexts(),
        )

    def _reserved_keys(self) -> dict[str, str]:
        fixed: dict[str, str] = {}
        for widget_type in (App, Screen, DataTable):
            for ancestor in reversed(widget_type.__mro__):
                if issubclass(ancestor, DOMNode):
                    fixed.update(_key_owners(ancestor.BINDINGS))
        fixed.update(_key_owners(binding for binding in self._bindings if binding.id is None))
        fixed.update(
            {
                key.split(" ", 1)[0]: description
                for _group, key, description, action in APP_HANDLER_KEY_HELP
                if not action
            }
        )
        return fixed

    def _priority_keys(self, modal_keys: Collection[str]) -> dict[str, str]:
        fixed = _key_owners(Input.BINDINGS)
        fixed.update(dict.fromkeys(modal_keys, "keybinding_editor"))
        fixed.update(dict.fromkeys(ActionPaletteScreen.CLOSE_KEYS, "open_action_palette"))
        for binding in self._bindings:
            if binding.priority and canonical_key(binding.key) in fixed:
                fixed[canonical_key(binding.key)] = binding.action
        return fixed

    def keymap(self, overrides: Mapping[str, str]) -> dict[str, str]:
        """Preflight a complete map, retaining every contextual and alternate ID."""
        plan = self.rules.plan(overrides)
        if plan.warnings:
            raise ValueError("; ".join(plan.warnings))
        keymap = {
            binding.id: shift_alias_keys(plan.overrides.get(binding.action, binding.key))
            for binding in self._bindings
            if binding.id is not None
        }
        BindingsMap(self._bindings).apply_keymap(keymap)
        return keymap
