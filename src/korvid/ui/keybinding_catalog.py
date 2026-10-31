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
from korvid.ui.app_bindings import (
    APP_HANDLER_KEY_HELP,
    MAIN_SCREEN_PRIORITY_ACTIONS,
    as_binding,
)
from korvid.ui.modal_bindings import modal_bindings
from korvid.ui.widgets.describe_screen import DescribeScreen

#: Screens that bind an app binding's id too, so a remap moves their key
#: with the app's (Textual applies a keymap by id). Their own fixed keys
#: are then reserved for that action.
SHARED_BINDING_SCREENS: tuple[type[DOMNode], ...] = (DescribeScreen,)


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
        self,
        bindings: Iterable[BindingType],
        *,
        modal_keys: Collection[str] = (),
        screens: Iterable[type[DOMNode]] = SHARED_BINDING_SCREENS,
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
                binding.action
                for binding in self._bindings
                if binding.priority and binding.action not in MAIN_SCREEN_PRIORITY_ACTIONS
            ),
            reserved_keys=self._reserved_keys(),
            priority_reserved_keys=self._priority_keys(modal_keys),
            action_contexts=action_contexts(),
            action_reserved_keys=self._screen_reserved_keys(screens),
        )

    def _screen_reserved_keys(self, screens: Iterable[type[DOMNode]]) -> dict[str, dict[str, str]]:
        """Per shared action, the fixed keys of each screen that binds it too."""
        by_id = {b.id: b.action for b in self._bindings if b.id is not None}
        reserved: dict[str, dict[str, str]] = {}
        for screen in screens:
            own = [as_binding(b) for b in screen.__dict__.get("BINDINGS", [])]
            fixed = _key_owners(b for b in own if b.id is None)
            for binding in own:
                if binding.id in by_id:
                    reserved.setdefault(by_id[binding.id], {}).update(fixed)
        return reserved

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
        fixed.update(_key_owners(modal_bindings()))
        fixed.update(dict.fromkeys(modal_keys, "keybinding_editor"))
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
