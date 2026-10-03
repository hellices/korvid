"""Numeric namespace slots number the namespaces you visit and keep them (issue #406)."""

from __future__ import annotations

from korvid.core.namespace_slots import (
    SlotEntry,
    SlotMap,
    SlotOrigin,
    build,
    place,
    preview,
    reallocate,
)

PINNED = SlotOrigin.PINNED
AUTO = SlotOrigin.AUTO


def _layout(slots: SlotMap) -> dict[int, tuple[str, str, bool]]:
    return {
        slot: (entry.namespace, entry.origin.value, entry.available)
        for slot, entry in slots.items()
    }


def _auto(namespace: str, *, available: bool = True) -> SlotEntry:
    return SlotEntry(namespace, AUTO, available=available)


def test_pins_take_the_leading_slots_and_a_listing_assigns_nothing() -> None:
    slots = build(["prod", "dev"], {}, frozenset({"zeta", "dev", "alpha", "prod"}))

    assert _layout(slots) == {1: ("prod", "pinned", True), 2: ("dev", "pinned", True)}


def test_a_visited_namespace_takes_the_lowest_free_slot() -> None:
    slots = build(["dev"], {3: _auto("staging")}, None)

    slots = place(slots, "payments")

    assert _layout(slots) == {
        1: ("dev", "pinned", True),
        2: ("payments", "auto", True),
        3: ("staging", "auto", True),
    }


def test_visits_number_namespaces_in_the_order_they_were_first_visited() -> None:
    slots = build([], {}, None)

    for namespace in ["payments", "checkout", "payments", "audit"]:
        slots = place(slots, namespace)

    assert _layout(slots) == {
        1: ("payments", "auto", True),
        2: ("checkout", "auto", True),
        3: ("audit", "auto", True),
    }


def test_visiting_a_mapped_namespace_changes_nothing() -> None:
    slots = build(["dev"], {2: _auto("gone", available=False)}, None)

    assert place(slots, "dev") == slots
    assert place(slots, "gone") == slots


def test_a_visit_never_takes_an_unavailable_slot_and_stops_at_nine() -> None:
    saved = {slot: _auto(f"ns-{slot}") for slot in range(1, 10)}
    saved[4] = _auto("gone", available=False)
    slots = build([], saved, None)

    assert place(slots, "extra") == slots
    assert slots.get(10) is None


def test_saved_automatic_slots_keep_their_numbers() -> None:
    saved = {5: _auto("staging"), 2: _auto("prod")}

    slots = build(["dev"], saved, frozenset({"dev", "prod", "staging", "alpha"}))

    assert _layout(slots) == {
        1: ("dev", "pinned", True),
        2: ("prod", "auto", True),
        5: ("staging", "auto", True),
    }


def test_a_confirmed_missing_namespace_keeps_its_slot_as_unavailable() -> None:
    saved = {1: _auto("dev"), 2: _auto("prod"), 3: _auto("staging")}

    after = build([], saved, frozenset({"dev", "staging", "beta"}))

    assert _layout(after) == {
        1: ("dev", "auto", True),
        2: ("prod", "auto", False),
        3: ("staging", "auto", True),
    }


def test_a_reappearing_namespace_becomes_available_in_its_old_slot() -> None:
    saved = {2: _auto("prod", available=False)}

    slots = build([], saved, frozenset({"prod"}))

    assert _layout(slots) == {2: ("prod", "auto", True)}


def test_unknown_inventory_restores_the_saved_map_without_inferring_deletions() -> None:
    saved = {3: _auto("staging"), 4: _auto("gone", available=False)}

    slots = build(["dev"], saved, None)

    assert _layout(slots) == {
        1: ("dev", "pinned", True),
        3: ("staging", "auto", True),
        4: ("gone", "auto", False),
    }


def test_pins_win_over_saved_slots_and_saved_names() -> None:
    saved = {1: _auto("staging"), 3: _auto("dev"), 4: _auto("qa")}

    slots = build(["dev", "prod"], saved, frozenset({"dev", "prod", "staging", "qa"}))

    assert _layout(slots) == {
        1: ("dev", "pinned", True),
        2: ("prod", "pinned", True),
        4: ("qa", "auto", True),
    }


def test_a_saved_namespace_is_never_assigned_twice() -> None:
    saved = {3: _auto("dev"), 6: _auto("dev")}

    slots = build([], saved, frozenset({"dev"}))

    assert _layout(slots) == {3: ("dev", "auto", True)}


def test_pins_are_never_marked_unavailable_by_discovery() -> None:
    slots = build(["private"], {}, frozenset({"dev"}))

    assert _layout(slots) == {1: ("private", "pinned", True)}


def test_only_nine_slots_exist_and_extra_visits_stay_unmapped() -> None:
    slots = build([], {}, None)
    for index in range(12):
        slots = place(slots, f"ns-{index:02d}")

    assert [slot for slot, _ in slots.items()] == list(range(1, 10))
    assert slots.get(9) == _auto("ns-08")
    assert slots.get(10) is None
    assert slots.get(0) is None


def test_out_of_range_saved_slots_are_ignored() -> None:
    slots = build([], {0: _auto("zero"), 10: _auto("ten")}, None)

    assert _layout(slots) == {}


def test_reallocation_drops_missing_namespaces_and_packs_the_rest_in_slot_order() -> None:
    saved = {2: _auto("zeta"), 4: _auto("gone"), 6: _auto("alpha"), 7: _auto("prod")}

    reallocated = reallocate(["prod"], saved, frozenset({"prod", "zeta", "alpha", "unvisited"}))

    assert _layout(reallocated) == {
        1: ("prod", "pinned", True),
        2: ("zeta", "auto", True),
        3: ("alpha", "auto", True),
    }


def test_preview_lists_only_changed_slots() -> None:
    saved = {3: _auto("old", available=False), 4: _auto("dev")}
    current = build(["prod"], saved, None)
    proposed = reallocate(["prod"], saved, frozenset({"prod", "dev"}))

    changes = preview(current, proposed)

    assert [(c.slot, c.before, c.after) for c in changes] == [
        (2, None, _auto("dev")),
        (3, _auto("old", available=False), None),
        (4, _auto("dev"), None),
    ]


def test_entries_describe_their_origin_and_availability() -> None:
    assert SlotEntry("dev", PINNED).describe() == "dev (pinned)"
    assert _auto("qa", available=False).describe() == "qa (auto, unavailable)"


def test_automatic_entries_exclude_pins() -> None:
    slots = place(build(["prod"], {}, None), "dev")

    assert slots.automatic() == {2: _auto("dev")}
