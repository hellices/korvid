"""`KubectlPresence` (issue #388 round 13): the session's one PATH lookup
for `kubectl`, and the guarantee that every later question is answered from
memory rather than from the filesystem."""

from __future__ import annotations

import shutil
from typing import Any
from unittest import mock

from korvid.k8s.kubectl import KubectlPresence, kubectl_on_path


def test_presence_resolves_exactly_once_however_often_it_is_asked() -> None:
    """The palette derives its catalog on every open, and two owners ask
    about `kubectl` each time: a live `shutil.which` there is a PATH scan
    (a stat per directory) per row, per render. One lookup per session,
    then memory."""
    calls: list[str] = []

    def detect() -> bool:
        calls.append("kubectl")
        return True

    presence = KubectlPresence(detect)
    assert [presence() for _ in range(5)] == [True] * 5
    assert calls == ["kubectl"]


def test_presence_reports_a_missing_binary_and_keeps_reporting_it() -> None:
    """The negative snapshot is just as sticky: a session that started
    without `kubectl` keeps saying so."""
    calls: list[str] = []

    def detect() -> bool:
        calls.append("kubectl")
        return False

    presence = KubectlPresence(detect)
    assert presence() is False
    assert presence() is False
    assert len(calls) == 1


def test_presence_does_not_touch_the_filesystem_before_it_is_asked() -> None:
    """Constructing the capability is not the lookup: the composition root
    builds one per session before the app is running, and the snapshot is
    taken by the first question (startup, in a real TUI)."""
    with mock.patch("shutil.which") as which:
        presence = KubectlPresence()
        assert which.call_count == 0
        assert presence() is bool(which.return_value)
        assert which.call_args_list == [mock.call("kubectl")]


def test_presence_snapshot_ignores_a_path_that_changes_afterwards() -> None:
    """Documented behaviour, pinned: a `kubectl` installed while korvid is
    running is not observed until the next start. The alternative - asking
    the filesystem again - is what the snapshot exists to avoid."""
    answers = iter([False, True])

    presence = KubectlPresence(lambda: next(answers))
    assert presence() is False
    assert presence() is False


def test_kubectl_on_path_asks_shutil_for_the_binary() -> None:
    """The default detection is one `shutil.which`, so a test (and the real
    session) can patch exactly one place."""
    with mock.patch("shutil.which", return_value="/usr/bin/kubectl") as which:
        assert kubectl_on_path() is True
    assert which.call_args_list == [mock.call("kubectl")]
    with mock.patch("shutil.which", return_value=None):
        assert kubectl_on_path() is False


def test_presence_is_callable_where_a_capability_flag_is_expected() -> None:
    """The controllers are injected with the object itself, not a bound
    method, so the seam stays `Callable[[], bool]`."""
    probe: Any = KubectlPresence(lambda: True)
    assert callable(probe)
    assert probe() is True
    assert shutil.which is not None  # the module under test never shadows it
