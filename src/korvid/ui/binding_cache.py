"""Bounded reuse of Textual keymap resolution after native input filtering."""

from __future__ import annotations

from functools import lru_cache

from textual.binding import Binding, BindingsMap, Keymap, KeymapApplyResult

_BindingSnapshot = tuple[tuple[str, tuple[Binding, ...]], ...]


def _snapshot(bindings: BindingsMap) -> _BindingSnapshot:
    return tuple((key, tuple(values)) for key, values in bindings.key_to_bindings.items())


@lru_cache(maxsize=64)
def _resolve_keymap(
    bindings: _BindingSnapshot, keymap: tuple[tuple[str, str], ...]
) -> tuple[_BindingSnapshot, frozenset[Binding]]:
    resolved = BindingsMap.from_keys({key: list(values) for key, values in bindings})
    result = resolved.apply_keymap(dict(keymap))
    return _snapshot(resolved), frozenset(result.clashed_bindings)


class CachedBindingsMap(BindingsMap):
    """Reuse immutable resolution inputs, never focus or action availability."""

    def copy(self) -> BindingsMap:
        """Keep the cache-aware type when Textual copies a binding namespace."""
        return CachedBindingsMap.from_keys(self.key_to_bindings.copy())

    def apply_keymap(self, keymap: Keymap) -> KeymapApplyResult:
        """Resolve the filtered namespace without sharing mutable results."""
        bindings, clashes = _resolve_keymap(_snapshot(self), tuple(keymap.items()))
        self.key_to_bindings = {key: list(values) for key, values in bindings}
        return KeymapApplyResult(set(clashes))
