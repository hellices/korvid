"""Keep repeated keymap resolution equivalent to Textual's uncached resolver."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.binding import Binding, BindingsMap, Keymap, KeymapApplyResult

from tests.ui.test_app import make_app


def _bindings(entries: list[Binding]) -> BindingsMap:
    return type(make_app([])._bindings)(entries)


@pytest.mark.parametrize("filtered", [False, True])
def test_repeated_keymap_resolution_reuses_the_same_filtered_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    binding_id = str(tmp_path)
    bindings = _bindings(
        [Binding("colon", "open_command", id=binding_id), Binding("ctrl+b", "other")]
    )
    apply_keymap = BindingsMap.apply_keymap
    resolutions: list[Keymap] = []

    def record_resolution(current: BindingsMap, keymap: Keymap) -> KeymapApplyResult:
        resolutions.append(dict(keymap))
        return apply_keymap(current, keymap)

    monkeypatch.setattr(BindingsMap, "apply_keymap", record_resolution)
    resolved: list[BindingsMap] = []
    for _attempt in range(2):
        copied = bindings.copy()
        if filtered:
            del copied.key_to_bindings["colon"]
        copied.apply_keymap({binding_id: "f1"})
        resolved.append(copied)

    assert len(resolutions) == 1
    assert resolved[0].key_to_bindings == resolved[1].key_to_bindings
    assert ("f1" in resolved[1].key_to_bindings) is not filtered
    assert "colon" in bindings.key_to_bindings


@pytest.mark.parametrize(
    "keymap",
    [
        {},
        {"alpha": "a", "rollout": "r", "rollback": "r", "alternate": "G"},
        {"alpha": "r", "rollout": "r", "rollback": "r", "alternate": "shift+g,G"},
        {"alpha": "ctrl+k", "rollout": "a", "rollback": "r", "alternate": "f2"},
        {"alpha": "q"},
    ],
)
def test_resolution_matches_textual_bindings_order_metadata_and_clashes(keymap: Keymap) -> None:
    entries = [
        Binding("a", "alpha", "Alpha", key_display="Alpha key", id="alpha"),
        Binding("r", "rollout", "Restart", id="rollout"),
        Binding("r", "rollback", "Rollback", id="rollback"),
        Binding("G", "alternate", "Alternate", id="alternate"),
        Binding("q", "quit", "Quit", priority=True),
    ]
    expected = BindingsMap(entries)
    expected_result = expected.apply_keymap(keymap)
    bindings = _bindings(entries)

    for _attempt in range(2):
        actual = bindings.copy()
        result = actual.apply_keymap(keymap)
        assert list(actual.key_to_bindings.items()) == list(expected.key_to_bindings.items())
        assert result == expected_result


def test_changed_keymap_filter_and_binding_declarations_do_not_reuse_stale_results() -> None:
    bindings = _bindings([Binding("colon", "open_command", id="command")])
    first = bindings.copy()
    first.apply_keymap({"command": "f1"})
    assert first.key_to_bindings["f1"][0].action == "open_command"

    filtered = bindings.copy()
    del filtered.key_to_bindings["colon"]
    filtered.apply_keymap({"command": "f1"})
    assert "f1" not in filtered.key_to_bindings

    remapped = bindings.copy()
    remapped.apply_keymap({"command": "f2"})
    assert "f1" not in remapped.key_to_bindings
    assert remapped.key_to_bindings["f2"][0].action == "open_command"

    bindings.bind("ctrl+b", "other")
    changed = bindings.copy()
    changed.apply_keymap({"command": "f1"})
    assert changed.key_to_bindings["ctrl+b"][0].action == "other"
    assert changed.key_to_bindings["f1"][0].action == "open_command"


def test_cached_results_do_not_share_mutable_binding_lists_or_clash_sets() -> None:
    fixed = Binding("z", "fixed", "Fixed")
    bindings = _bindings([Binding("a", "alpha", id="alpha"), fixed])
    first = bindings.copy()
    first_result = first.apply_keymap({"alpha": "z"})
    assert first_result.clashed_bindings == {fixed}
    first_result.clashed_bindings.clear()
    first.key_to_bindings["z"].clear()
    first.key_to_bindings.clear()

    second = bindings.copy()
    second_result = second.apply_keymap({"alpha": "z"})
    assert second_result.clashed_bindings == {fixed}
    assert [binding.action for binding in second.key_to_bindings["z"]] == ["alpha"]
    assert bindings.key_to_bindings["z"] == [fixed]


def test_resolution_cache_does_not_retain_unbounded_binding_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding_id = str(tmp_path)
    bindings_type = type(_bindings([]))
    apply_keymap = BindingsMap.apply_keymap
    resolutions: list[Keymap] = []

    def record_resolution(current: BindingsMap, keymap: Keymap) -> KeymapApplyResult:
        resolutions.append(dict(keymap))
        return apply_keymap(current, keymap)

    monkeypatch.setattr(BindingsMap, "apply_keymap", record_resolution)
    for variant in range(65):
        bindings = bindings_type([Binding("a", "alpha", str(variant), id=binding_id)])
        bindings.copy().apply_keymap({binding_id: "z"})
    repeated = bindings_type([Binding("a", "alpha", "0", id=binding_id)])
    repeated.copy().apply_keymap({binding_id: "z"})

    assert len(resolutions) == 66
