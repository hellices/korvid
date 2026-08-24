"""Tests for `AppAgentScreens.selected_identity`."""

from __future__ import annotations

from typing import cast

import pytest
from textual.css.query import NoMatches

from korvid.ui.app import AppAgentScreens, KorvidApp


class _FakeApp:
    """The one method `AppAgentScreens` calls, wired to fail.

    Only `query_one` is exercised here, and building a real `KorvidApp`
    would drag a whole Textual runtime into a two-line lookup test, so the
    fake is cast at the single point it crosses the boundary.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def query_one(self, selector: str, widget_type: object) -> object:
        raise self._exc


def _screens(exc: Exception) -> AppAgentScreens:
    return AppAgentScreens(cast("KorvidApp", _FakeApp(exc)))


def test_selected_identity_returns_none_for_no_matches() -> None:
    screens = _screens(NoMatches("missing table"))

    assert screens.selected_identity("pane-0", "Pod") is None


def test_selected_identity_propagates_unrelated_errors() -> None:
    screens = _screens(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        screens.selected_identity("pane-0", "Pod")
