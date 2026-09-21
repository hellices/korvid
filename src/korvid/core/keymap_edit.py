"""Pure, reversible keymap editing with no persistence or live dispatch."""

from __future__ import annotations

import string
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import combinations

from korvid.core.keybindings import (
    ActionContexts,
    KeymapPlan,
    actions_overlap,
    canonical_key,
    plan_keybindings,
)


@dataclass(frozen=True)
class KeymapRules:
    """Binding metadata shared by startup validation and the editor."""

    actions: Mapping[str, tuple[str, ...]]
    priority_actions: frozenset[str] = frozenset()
    reserved_keys: Mapping[str, str] = field(default_factory=dict)
    priority_reserved_keys: Mapping[str, str] = field(default_factory=dict)
    action_contexts: ActionContexts = field(default_factory=dict)

    def plan(self, overrides: Mapping[str, object]) -> KeymapPlan:
        """Validate a complete proposal using the startup policy."""
        return plan_keybindings(
            overrides,
            self.actions,
            self.priority_actions,
            self.reserved_keys,
            self.priority_reserved_keys,
            action_contexts=self.action_contexts,
        )

    def assignment(self, action: str, key: str) -> KeymapPlan:
        """Check a staged key's syntax/protection without discarding conflicts."""
        return plan_keybindings(
            {action: key},
            {action: self.actions[action]} if action in self.actions else {},
            self.priority_actions,
            self.reserved_keys,
            self.priority_reserved_keys,
        )

    def keys(self, action: str, overrides: Mapping[str, str]) -> tuple[str, ...]:
        """Return distinct physical keys for an action in a proposal."""
        keys = (overrides[action],) if action in overrides else self.actions[action]
        return tuple(dict.fromkeys(canonical_key(key) for key in keys))


@dataclass(frozen=True)
class KeymapConflict:
    """Two action owners that would dispatch on the same physical key."""

    actions: tuple[str, str]
    key: str
    contexts: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class KeymapChange:
    """One action's effective before/after keys in a complete preview."""

    action: str
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True)
class _EditState:
    overrides: dict[str, str]
    swap_anchor: tuple[str, str] | None


class KeymapEdit:
    """One session whose complete pending proposal is isolated from live state."""

    def __init__(self, rules: KeymapRules, overrides: Mapping[str, str]) -> None:
        plan = rules.plan(overrides)
        if plan.warnings:
            raise ValueError("; ".join(plan.warnings))
        self.rules = rules
        self._baseline = dict(plan.overrides)
        self._overrides = dict(plan.overrides)
        self._history: deque[_EditState] = deque(maxlen=128)
        self._swap_anchor: tuple[str, str] | None = None

    @property
    def overrides(self) -> dict[str, str]:
        """Return a detached copy of the staged override section."""
        return dict(self._overrides)

    @property
    def dirty(self) -> bool:
        """Whether confirmation would change the original override section."""
        return self._overrides != self._baseline

    @property
    def can_undo(self) -> bool:
        """Whether a prior staged state remains available."""
        return bool(self._history)

    @property
    def last_action(self) -> str | None:
        """The initiating action, for directing the next conflict decision."""
        return self._swap_anchor[0] if self._swap_anchor is not None else None

    @property
    def can_swap(self) -> bool:
        """Whether the last assignment permits a complete valid two-way swap."""
        return self._swap_proposal() is not None

    def assign(self, action: str, key: str) -> None:
        """Stage one valid key, retaining unresolved ownership conflicts."""
        plan = self.rules.assignment(action, key)
        if plan.warnings:
            raise ValueError(plan.warnings[0])
        previous_key = self.rules.keys(action, self._overrides)[0]
        proposal = self._with_key(self._overrides, action, plan.overrides[action])
        self._stage(proposal, (action, previous_key))

    def reset(self, action: str) -> None:
        """Remove one override, staging any conflict with its default key."""
        if action not in self.rules.actions:
            raise ValueError(f"unknown action '{action}'")
        previous_key = self.rules.keys(action, self._overrides)[0]
        proposal = dict(self._overrides)
        proposal.pop(action, None)
        self._stage(proposal, (action, previous_key))

    def reset_all(self) -> None:
        """Stage removal of every keybinding override, without touching settings."""
        self._stage({}, None)

    def undo(self) -> bool:
        """Restore one prior staged state and its swap decision."""
        if not self._history:
            return False
        state = self._history.pop()
        self._overrides = dict(state.overrides)
        self._swap_anchor = state.swap_anchor
        return True

    def conflicts(self) -> tuple[KeymapConflict, ...]:
        """Describe overlaps in the complete pending map, including defaults."""
        conflicts: list[KeymapConflict] = []
        contexts = self.rules.action_contexts
        for action, other in combinations(self.rules.actions, 2):
            if not actions_overlap(action, other, contexts):
                continue
            common_keys = set(self.rules.keys(action, self._overrides)).intersection(
                self.rules.keys(other, self._overrides)
            )
            scope = contexts.get(action) or contexts.get(other) or frozenset()
            if contexts.get(action) and contexts.get(other):
                scope = contexts[action].intersection(contexts[other])
            conflicts.extend(
                KeymapConflict((action, other), key, scope) for key in sorted(common_keys)
            )
        return tuple(conflicts)

    def changes(self) -> tuple[KeymapChange, ...]:
        """Describe the complete preview, including removal of explicit defaults."""
        return tuple(
            KeymapChange(
                action,
                self.rules.keys(action, self._baseline),
                self.rules.keys(action, self._overrides),
            )
            for action in self.rules.actions
            if self._baseline.get(action) != self._overrides.get(action)
        )

    def suggestions(self, action: str, *, limit: int = 3) -> tuple[str, ...]:
        """Offer at most five deterministic keys whose complete proposals validate."""
        if action not in self.rules.actions or limit <= 0:
            return ()
        candidates = dict.fromkeys(
            [
                *self.rules.actions[action],
                *string.ascii_letters,
                *(f"f{number}" for number in range(1, 25)),
                *(f"ctrl+{letter}" for letter in string.ascii_lowercase),
            ]
        )
        suggested: list[str] = []
        for candidate in candidates:
            marker = canonical_key(candidate)
            if marker in suggested or self.rules.assignment(action, marker).warnings:
                continue
            proposal = self._with_key(self._overrides, action, marker)
            if self.rules.plan(proposal).warnings:
                continue
            suggested.append(marker)
            if len(suggested) >= min(limit, 5):
                break
        return tuple(suggested)

    def swap(self) -> bool:
        """Stage the offered two-way swap only if the complete result is valid."""
        proposal = self._swap_proposal()
        if proposal is None:
            return False
        self._stage(proposal, None)
        return True

    def _swap_proposal(self) -> dict[str, str] | None:
        if self._swap_anchor is None:
            return None
        action, freed_key = self._swap_anchor
        owners = {
            owner
            for conflict in self.conflicts()
            if action in conflict.actions
            for owner in conflict.actions
            if owner != action
        }
        if len(owners) != 1:
            return None
        other = next(iter(owners))
        proposal = self._with_key(self._overrides, other, freed_key)
        return None if self.rules.plan(proposal).warnings else proposal

    def _with_key(self, overrides: Mapping[str, str], action: str, key: str) -> dict[str, str]:
        proposal = dict(overrides)
        defaults = self.rules.keys(action, {})
        if defaults == (canonical_key(key),):
            proposal.pop(action, None)
        else:
            proposal[action] = key
        return proposal

    def _stage(self, proposal: dict[str, str], anchor: tuple[str, str] | None) -> None:
        if proposal == self._overrides:
            return
        self._history.append(_EditState(dict(self._overrides), self._swap_anchor))
        self._overrides = proposal
        self._swap_anchor = anchor
