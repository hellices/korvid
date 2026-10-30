"""Tests for bounding a ConfigMap's or Secret's change by two sightings (#410 P2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from korvid.core.config_changes import (
    SKEW,
    ChangeLog,
    ObjectKey,
    ObservedChange,
    Ordering,
    Sighting,
    order,
)

START = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
KEY = ObjectKey(1, 0, "ConfigMap", "shop", "settings", "uid-1")


def _at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def _sighting(version: str, sent: float, arrived: float | None = None) -> Sighting:
    return Sighting(version, _at(sent), _at(sent if arrived is None else arrived))


def test_a_change_is_bounded_by_the_old_send_and_the_new_arrival() -> None:
    log = ChangeLog()

    assert log.record(KEY, _sighting("1", -60, -59)) is None
    change = log.record(KEY, _sighting("2", 60, 61))

    assert change == ObservedChange(_at(-60), _at(61))


def test_a_repeated_version_moves_the_bound_forward_and_keeps_the_change() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))
    log.record(KEY, _sighting("1", -30))
    first = log.record(KEY, _sighting("2", 30, 31))

    again = log.record(KEY, _sighting("2", 90, 91))

    assert first == ObservedChange(_at(-30), _at(31))
    assert again == first


def test_the_latest_change_is_the_one_reported() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))
    log.record(KEY, _sighting("2", 30))

    change = log.record(KEY, _sighting("3", 90, 92))

    assert change == ObservedChange(_at(30), _at(92))


def test_a_sighting_sent_before_the_last_one_is_ignored() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))
    log.record(KEY, _sighting("2", 30))

    late = log.record(KEY, _sighting("1", 0, 40))

    assert late == ObservedChange(_at(-60), _at(30))
    assert log.record(KEY, _sighting("2", 50)) == ObservedChange(_at(-60), _at(30))


@pytest.mark.parametrize(
    "key",
    [
        ObjectKey(1, 0, "ConfigMap", "shop", "settings", "uid-2"),
        ObjectKey(2, 0, "ConfigMap", "shop", "settings", "uid-1"),
        ObjectKey(1, 1, "ConfigMap", "shop", "settings", "uid-1"),
        ObjectKey(1, 0, "Secret", "shop", "settings", "uid-1"),
        ObjectKey(1, 0, "ConfigMap", "other", "settings", "uid-1"),
        ObjectKey(1, 0, "ConfigMap", "shop", "other", "uid-1"),
    ],
)
def test_another_incarnation_or_scope_starts_without_a_sighting(key: ObjectKey) -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))

    assert log.record(key, _sighting("2", 60)) is None


def test_the_least_recently_recorded_object_is_forgotten_past_capacity() -> None:
    log = ChangeLog(capacity=2)
    other = ObjectKey(1, 0, "ConfigMap", "shop", "other", "uid-2")
    third = ObjectKey(1, 0, "ConfigMap", "shop", "third", "uid-3")
    log.record(KEY, _sighting("1", -60))
    log.record(other, _sighting("1", -50))
    log.record(KEY, _sighting("1", -40))

    log.record(third, _sighting("1", -30))

    assert log.record(KEY, _sighting("2", 60)) == ObservedChange(_at(-40), _at(60))
    assert log.record(other, _sighting("2", 60)) is None


@pytest.mark.parametrize(
    ("after", "before", "expected"),
    [
        (1, 3, Ordering.AFTER_START),
        (5, 10, Ordering.AFTER_START),
        (-10, -1, Ordering.BEFORE_START),
        (-10, -5, Ordering.BEFORE_START),
        (-60, 60, Ordering.NOT_ESTABLISHED),
        (0.5, 3, Ordering.NOT_ESTABLISHED),
        (-3, -0.5, Ordering.NOT_ESTABLISHED),
    ],
    ids=[
        "after",
        "well-after",
        "before",
        "well-before",
        "straddles",
        "within-skew-after",
        "within-skew-before",
    ],
)
def test_a_change_is_ordered_against_a_start_only_outside_the_skew_allowance(
    after: float, before: float, expected: Ordering
) -> None:
    assert order(ObservedChange(_at(after), _at(before)), START) is expected


def test_a_delayed_old_response_does_not_place_the_change_after_the_start() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -5, 30))
    change = log.record(KEY, _sighting("2", 60))

    assert change is not None
    assert order(change, START) is Ordering.NOT_ESTABLISHED


def test_without_a_start_the_order_is_not_established() -> None:
    assert order(ObservedChange(_at(5), _at(10)), None) is Ordering.NOT_ESTABLISHED


def test_the_skew_allowance_is_one_minute() -> None:
    assert timedelta(minutes=1) == SKEW


@pytest.mark.parametrize(
    ("version", "sent", "arrived", "message"),
    [
        ("", START, START, "resource version"),
        ("1", _at(1), START, "arrives after it is sent"),
        ("1", START.replace(tzinfo=None), START.replace(tzinfo=None), "aware"),
    ],
)
def test_a_sighting_is_checked(
    version: str, sent: datetime, arrived: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Sighting(version, sent, arrived)


def test_a_capacity_is_positive() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ChangeLog(capacity=0)


def test_a_sighting_sent_at_the_same_moment_still_counts() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", 0, 1))

    assert log.record(KEY, _sighting("2", 0, 2)) == ObservedChange(_at(0), _at(2))
