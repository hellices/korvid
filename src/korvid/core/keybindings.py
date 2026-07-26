"""Keybinding overrides from config (issue #35).

Pure validation — no Textual imports. The `keybindings:` section of
`config.yaml` maps action names to replacement keys; this module turns the
raw mapping into a validated plan plus human-readable warnings so a typo
never crashes startup or silently does nothing.

Safety invariant: approval dialogs are confirmed only by fixed user
keystrokes, so their actions can never be remapped from config.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

#: Actions on the approval/confirm dialogs — remapping any of these from
#: config is rejected (the approval gate's keys stay fixed by design).
PROTECTED_ACTIONS = frozenset({"confirm", "cancel", "approve", "deny"})


@dataclass(frozen=True)
class KeymapPlan:
    """Validated keybinding overrides plus warnings to surface at startup."""

    overrides: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _validated_overrides(
    raw: Mapping[str, object],
    actions: Mapping[str, tuple[str, ...]],
    warnings: list[str],
) -> dict[str, str]:
    """First pass: per-entry checks (action known, key usable, no dup key)."""
    overrides: dict[str, str] = {}
    used_keys: dict[str, str] = {}
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
        if key in used_keys:
            warnings.append(
                f"keybindings: duplicate key '{key}' for '{action}' "
                f"(already used by '{used_keys[key]}')"
            )
            continue
        used_keys[key] = action
        overrides[action] = key
    return overrides


def plan_keybindings(
    raw: Mapping[str, object],
    actions: Mapping[str, tuple[str, ...]],
) -> KeymapPlan:
    """Validate config keybinding overrides against the app's actions.

    Args:
        raw: The parsed `keybindings:` mapping (action name → key). Values
            are `object` because YAML may supply non-strings.
        actions: Every remappable action mapped to its default keys.

    Returns:
        A plan whose `overrides` contains only safe, conflict-free entries;
        every rejected entry produces one entry in `warnings`.
    """
    warnings: list[str] = []
    overrides = _validated_overrides(raw, actions, warnings)
    # Second pass: a new key must not shadow a default key that another,
    # un-remapped action still holds (order-independent: an override frees
    # its old defaults regardless of config order).
    for action, key in list(overrides.items()):
        for other, default_keys in actions.items():
            if other != action and other not in overrides and key in default_keys:
                warnings.append(
                    f"keybindings: key '{key}' for '{action}' is already "
                    f"the default key of '{other}'"
                )
                del overrides[action]
                break
    return KeymapPlan(overrides, tuple(warnings))
