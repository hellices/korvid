"""Keybinding overrides from config (issue #35).

Pure validation — no Textual imports. The `keybindings:` section of
`config.yaml` maps action names to replacement keys; this module turns the
raw mapping into a validated plan plus human-readable warnings so a typo
never crashes startup or silently does nothing.

Safety invariant: approval dialogs are confirmed only by fixed user
keystrokes, so their actions can never be remapped from config, and
priority actions (dispatched before any screen) can never take one of the
dialogs' keys.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

#: Actions on the approval/confirm dialogs — remapping any of these from
#: config is rejected (the approval gate's keys stay fixed by design).
PROTECTED_ACTIONS = frozenset({"confirm", "cancel", "approve", "deny"})

#: Keys the approval dialogs listen for (y/n confirm-cancel, Enter submits
#: the name-typed variant, Escape cancels). A *priority* binding fires
#: before the dialog's own handlers, so priority actions may not take them.
APPROVAL_KEYS = frozenset({"y", "n", "enter", "escape"})

ActionContexts = Mapping[str, frozenset[tuple[str, str]]]
_PUNCTUATION_NAMES = {
    "solidus": "slash",
    "reverse_solidus": "backslash",
    "commercial_at": "at",
    "hyphen_minus": "minus",
    "plus_sign": "plus",
    "low_line": "underscore",
}
_TERMINAL_KEYS = {
    "backspace": ("backspace", "ctrl+h"),
    "tab": ("tab", "ctrl+i"),
    "enter": ("enter", "ctrl+m"),
    "escape": ("escape", "ctrl+left_square_bracket", "ctrl+["),
    "ctrl+at": ("ctrl+@", "ctrl+space", "ctrl+at"),
}
_KEY_ALIASES = {
    **{alias: marker for marker, spellings in _TERMINAL_KEYS.items() for alias in spellings},
    "esc": "escape",
    "return": "enter",
    "backtab": "shift+tab",
    "ctrl+left_square_brace": "escape",
    "newline": "ctrl+j",
}
_MODIFIERS = frozenset({"ctrl", "alt", "shift", "super", "meta"})
_NAMED_KEYS = frozenset(
    {
        "enter",
        "escape",
        "tab",
        "backtab",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "left",
        "right",
        "up",
        "down",
        "space",
        "at",
        "minus",
        "plus",
        "less_than_sign",
        "greater_than_sign",
        "print_screen",
        "pause",
    }
)


@dataclass(frozen=True)
class KeymapPlan:
    """Validated keybinding overrides plus warnings to surface at startup."""

    overrides: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _character_name(key: str) -> str:
    if len(key) != 1 or key.isalnum():
        return key
    name = unicodedata.name(key, key).lower().replace("-", "_").replace(" ", "_")
    return _PUNCTUATION_NAMES.get(name, name)


def _sorted_spelling(key: str) -> str:
    parts = key.split("+")
    return "+".join([*sorted(parts[:-1]), parts[-1]])


def canonical_key(key: str) -> str:
    """Return one physical identity, which need not be the sole emitted spelling."""
    if len(key) == 1:
        normalized = _character_name(key)
    else:
        modifiers, separator, name = _sorted_spelling(key).rpartition("+")
        normalized = f"{modifiers}{separator}{_character_name(name)}"
    if (
        normalized.startswith("shift+")
        and len(normalized) == 7
        and normalized[6] in string.ascii_letters
    ):
        return normalized[6].upper()
    return _KEY_ALIASES.get(normalized, normalized)


def _usable_key(key: str) -> bool:
    parts = canonical_key(key).split("+")
    modifiers, name = parts[:-1], parts[-1]
    if len(set(modifiers)) != len(modifiers) or any(part not in _MODIFIERS for part in modifiers):
        return False
    if name in _NAMED_KEYS or name in _PUNCTUATION_NAMES.values():
        return True
    if len(name) == 1 and name.isprintable() and not name.isspace():
        return True
    if re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", name):
        return True
    try:
        character = unicodedata.lookup(name.replace("_", " ").upper())
    except KeyError:
        return False
    return len(character) == 1 and character.isprintable() and not character.isspace()


def actions_overlap(action: str, other: str, contexts: ActionContexts) -> bool:
    """Whether two actions can dispatch in the same static resource view.

    Missing or empty scope metadata is conservative, never evidence that an
    unavailable action or pane cannot coexist with another action.
    """
    own_scope = contexts.get(action)
    other_scope = contexts.get(other)
    return not own_scope or not other_scope or not own_scope.isdisjoint(other_scope)


def shift_alias_keys(key: str) -> str:
    """Supported terminal spellings, comma-joined for a Textual keymap.

    ``shift+g``/``G`` → ``shift+g,G`` so a remapped shifted letter works
    both under Pilot (which synthesizes ``shift+g``) and in real terminals
    (which emit ``G``). Control aliases retain both legacy and extended
    protocol spellings, preferring the requested one when it can be emitted.
    """
    marker = canonical_key(key)
    if len(marker) == 1 and marker in string.ascii_uppercase:
        return f"shift+{marker.lower()},{marker}"
    spellings = _TERMINAL_KEYS.get(marker, (marker,))
    preferred = _sorted_spelling(key)
    if preferred in spellings:
        spellings = (preferred, *(spelling for spelling in spellings if spelling != preferred))
    return ",".join(spellings)


def _entry_problem(
    action: str,
    raw_key: object,
    actions: Mapping[str, tuple[str, ...]],
    priority_actions: Collection[str],
    reserved: Mapping[str, str],
    priority_reserved: Mapping[str, str],
) -> str | None:
    if action in PROTECTED_ACTIONS:
        return f"'{action}' belongs to the approval dialog and cannot be remapped"
    if action not in actions:
        return f"unknown action '{action}' (known actions: {', '.join(sorted(actions))})"
    if not isinstance(raw_key, str) or not raw_key.strip():
        return f"'{action}' needs a non-empty key string"
    key = raw_key.strip()
    if not _usable_key(key):
        return f"'{action}' must map to exactly one key supported by the terminal, got '{key}'"
    marker = canonical_key(key)
    if action in priority_actions and marker in APPROVAL_KEYS:
        return f"'{action}' is a priority binding and may not take '{key}' — the approval dialogs listen for that key"
    priority_owner = priority_reserved.get(marker)
    if action in priority_actions and priority_owner is not None and priority_owner != action:
        return f"priority action '{action}' may not take fixed modal key '{key}' owned by '{priority_owner}'"
    if marker in reserved:
        return f"key '{key}' for '{action}' is reserved by '{reserved[marker]}' and cannot be remapped over"
    return None


def _validated_overrides(
    raw: Mapping[str, object],
    actions: Mapping[str, tuple[str, ...]],
    priority_actions: Collection[str],
    reserved_keys: Mapping[str, str],
    priority_reserved_keys: Mapping[str, str],
    warnings: list[str],
    action_contexts: ActionContexts,
) -> dict[str, str]:
    """First pass: per-entry checks (action known, key usable, no dup key)."""
    overrides: dict[str, str] = {}
    used_keys: dict[str, list[str]] = {}
    reserved = {canonical_key(key): owner for key, owner in reserved_keys.items()}
    priority_reserved = {canonical_key(key): owner for key, owner in priority_reserved_keys.items()}
    for action, raw_key in raw.items():
        problem = _entry_problem(
            action, raw_key, actions, priority_actions, reserved, priority_reserved
        )
        if problem is not None:
            warnings.append(f"keybindings: {problem}")
            continue
        key = str(raw_key).strip()
        marker = canonical_key(key)
        conflict = next(
            (
                owner
                for owner in used_keys.get(marker, ())
                if actions_overlap(action, owner, action_contexts)
            ),
            None,
        )
        if conflict is not None:
            warnings.append(
                f"keybindings: duplicate key '{key}' for '{action}' (already used by '{conflict}')"
            )
            continue
        used_keys.setdefault(marker, []).append(action)
        overrides[action] = key
    return overrides


def _drop_default_collisions(
    overrides: dict[str, str],
    actions: Mapping[str, tuple[str, ...]],
    warnings: list[str],
    action_contexts: ActionContexts,
) -> None:
    """A new key must not shadow a default key another action still holds.

    Runs to a fixpoint: rejecting an override restores that action's
    defaults, which can expose a collision for an override accepted
    earlier (e.g. `aa→2, bb→3` where `3` clashes — dropping `bb` restores
    its default `2`, which `aa` now shadows).
    """
    changed = True
    while changed:
        changed = False
        for action, key in list(overrides.items()):
            marker = canonical_key(key)
            for other, default_keys in actions.items():
                defaults = {canonical_key(default) for default in default_keys}
                if (
                    other != action
                    and other not in overrides
                    and marker in defaults
                    and actions_overlap(action, other, action_contexts)
                ):
                    warnings.append(
                        f"keybindings: key '{key}' for '{action}' is already "
                        f"the default key of '{other}'"
                    )
                    del overrides[action]
                    changed = True
                    break


def plan_keybindings(
    raw: Mapping[str, object],
    actions: Mapping[str, tuple[str, ...]],
    priority_actions: Collection[str] = frozenset(),
    reserved_keys: Mapping[str, str] | None = None,
    priority_reserved_keys: Mapping[str, str] | None = None,
    *,
    action_contexts: ActionContexts | None = None,
) -> KeymapPlan:
    """Validate config keybinding overrides against the app's actions.

    Args:
        raw: The parsed `keybindings:` mapping (action name → key). Values
            are `object` because YAML may supply non-strings.
        actions: Every remappable action mapped to its default keys.
        priority_actions: Actions whose bindings fire before any screen;
            these may not take an approval-dialog key (`APPROVAL_KEYS`).
        reserved_keys: Keys owned by non-remappable bindings (key → owning
            action, e.g. the 1-9 favorites); an override may not take one.
        priority_reserved_keys: Fixed modal keys (key → owning action) that
            another priority action must not intercept before the modal.
        action_contexts: Static resource-view scopes from the dispatch policy.
            Unlisted actions overlap every view, including split-pane states.

    Returns:
        A plan whose `overrides` contains only safe, conflict-free entries;
        every rejected entry produces one entry in `warnings`.
    """
    warnings: list[str] = []
    overrides = _validated_overrides(
        raw,
        actions,
        priority_actions,
        reserved_keys or {},
        priority_reserved_keys or {},
        warnings,
        action_contexts or {},
    )
    _drop_default_collisions(overrides, actions, warnings, action_contexts or {})
    return KeymapPlan(overrides, tuple(warnings))
