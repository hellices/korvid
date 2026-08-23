"""Tests for `AppAgentScreens.selected_identity`."""

from __future__ import annotations

import pytest
from textual.css.query import NoMatches

from korvid.ui.app import AppAgentScreens


class _FakeApp:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def query_one(self, selector: str, widget_type: object) -> object:
        raise self._exc


def test_selected_identity_returns_none_for_no_matches() -> None:
    screens = AppAgentScreens(_FakeApp(NoMatches("missing table")))

    assert screens.selected_identity("pane-0", "Pod") is None


def test_selected_identity_propagates_unrelated_errors() -> None:
    screens = AppAgentScreens(_FakeApp(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        screens.selected_identity("pane-0", "Pod")
