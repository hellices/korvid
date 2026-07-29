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

import string
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

#: Actions on the approval/confirm dialogs — remapping any of these from
#: config is rejected (the approval gate's keys stay fixed by design).
PROTECTED_ACTIONS = frozenset({"confirm", "cancel", "approve", "deny"})

#: Keys the approval dialogs listen for (y/n confirm-cancel, Enter submits
#: the name-typed variant, Escape cancels). A *priority* binding fires
#: before the dialog's own handlers, so priority actions may not take them.
APPROVAL_KEYS = frozenset({"y", "n", "enter", "escape"})


@dataclass(frozen=True)
class KeymapPlan:
    """Validated keybinding overrides plus warnings to surface at startup."""

    overrides: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def canonical_key(key: str) -> str:
    """One spelling per physical key: ``shift+g`` and ``G`` are the same.

    Real terminals deliver Shift+<letter> as the uppercase character, so
    both spellings must collide with each other during validation.
    """
    if key.startswith("shift+") and len(key) == 7 and key[6] in string.ascii_letters:
        return key[6].upper()
    return key


def shift_alias_keys(key: str) -> str:
    """The key plus its terminal alias, comma-joined for a Textual keymap.

    ``shift+g``/``G`` → ``shift+g,G`` so a remapped shifted letter works
    both under Pilot (which synthesizes ``shift+g``) and in real terminals
    (which emit ``G``). Other keys pass through unchanged.
    """
    if key.startswith("shift+") and len(key) == 7 and key[6] in string.ascii_letters:
        return f"{key},{key[6].upper()}"
    if len(key) == 1 and key in string.ascii_uppercase:
        return f"shift+{key.lower()},{key}"
    return key


def _validated_overrides(
    raw: Mapping[str, object],
    actions: Mapping[str, tuple[str, ...]],
    priority_actions: Collection[str],
    reserved_keys: Mapping[str, str],
    warnings: list[str],
) -> dict[str, str]:
    """First pass: per-entry checks (action known, key usable, no dup key)."""
    overrides: dict[str, str] = {}
    used_keys: dict[str, str] = {}
    reserved = {canonical_key(k): owner for k, owner in reserved_keys.items()}
    for action, raw_key in raw.items():
        if action in PROTECTED_ACTIONS:
            warnings.append(
                f"keybindings: '{action}' belongs to the approval dialog and cannot be remapped"
            )
            continue
        if action not in actions:
            known = ", ".join(sorted(actions))
            warnings.append(f"keybindings: unknown action '{action}' (known actions: {known})")
            continue
        if not isinstance(raw_key, str) or not raw_key.strip():
            warnings.append(f"keybindings: '{action}' needs a non-empty key string")
            continue
        key = raw_key.strip()
        marker = canonical_key(key)
        if action in priority_actions and marker in APPROVAL_KEYS:
            warnings.append(
                f"keybindings: '{action}' is a priority binding and may not take "
                f"'{key}' — the approval dialogs listen for that key"
            )
            continue
        if marker in reserved:
            warnings.append(
                f"keybindings: key '{key}' for '{action}' is reserved by "
                f"'{reserved[marker]}' and cannot be remapped over"
            )
            continue
        if marker in used_keys:
            warnings.append(
                f"keybindings: duplicate key '{key}' for '{action}' "
                f"(already used by '{used_keys[marker]}')"
            )
            continue
        used_keys[marker] = action
        overrides[action] = key
    return overrides


def _drop_default_collisions(
    overrides: dict[str, str],
    actions: Mapping[str, tuple[str, ...]],
    warnings: list[str],
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
                defaults = {canonical_key(k) for k in default_keys}
                if other != action and other not in overrides and marker in defaults:
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

    Returns:
        A plan whose `overrides` contains only safe, conflict-free entries;
        every rejected entry produces one entry in `warnings`.
    """
    warnings: list[str] = []
    overrides = _validated_overrides(raw, actions, priority_actions, reserved_keys or {}, warnings)
    _drop_default_collisions(overrides, actions, warnings)
    return KeymapPlan(overrides, tuple(warnings))
