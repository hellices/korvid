"""Tests for `AppAgentScreens` workspace identity reads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.css.query import NoMatches

from korvid.agent.interaction import ResourceIdentity
from korvid.ui.app import AppAgentScreens, KorvidApp
from korvid.ui.widgets.describe_screen import DescribeScreen


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


def _display_screens(**app_fields: Any) -> AppAgentScreens:
    return AppAgentScreens(cast("KorvidApp", SimpleNamespace(**app_fields)))


def test_displayed_pane_context_reads_the_open_describe_target() -> None:
    screen = DescribeScreen(
        "Pod prod/api-2",
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "namespace": "prod",
                "name": "api-2",
                "uid": "uid-2",
            },
        },
        [],
    )
    screens = _display_screens(
        screen=screen,
        _describe_pane=SimpleNamespace(display=False, resource_identity=None),
        _logs=SimpleNamespace(mode="", current_triples=[], owner=None),
    )

    displayed = screens.displayed_pane_context()

    assert displayed is not None
    pane = displayed.context
    assert pane.kind == "Pod"
    assert pane.scope == "prod"
    assert pane.selected is not None
    assert pane.selected.name == "api-2"
    assert pane.selected.uid == "uid-2"


def test_unidentified_modal_describe_never_exposes_hidden_logs() -> None:
    screens = _display_screens(
        screen=DescribeScreen("Unknown resource", {"metadata": {}}, []),
        _describe_pane=SimpleNamespace(display=False, resource_identity=None),
        _logs=SimpleNamespace(
            mode="l",
            current_triples=[("prod", "hidden-log-pod", "main")],
            owner=None,
        ),
    )

    displayed = screens.displayed_pane_context()

    assert displayed is not None
    assert displayed.owner is None
    assert displayed.context.kind == "unknown"
    assert displayed.context.selected is None


def test_displayed_pane_context_reads_the_open_log_target() -> None:
    screens = _display_screens(
        screen=object(),
        _describe_pane=SimpleNamespace(display=False, resource_identity=None),
        _logs=SimpleNamespace(
            mode="l",
            current_triples=[("prod", "api-2", "main"), ("prod", "api-2", "sidecar")],
            owner=None,
        ),
    )

    displayed = screens.displayed_pane_context()

    assert displayed is not None
    pane = displayed.context
    assert pane.kind == "pods"
    assert pane.scope == "prod"
    assert pane.selected is not None
    assert pane.selected.name == "api-2"


def test_displayed_pane_context_does_not_reuse_table_identity_for_multi_pod_logs() -> None:
    screens = _display_screens(
        screen=object(),
        _describe_pane=SimpleNamespace(display=False, resource_identity=None),
        _logs=SimpleNamespace(
            mode="L",
            current_triples=[
                ("prod", "api-1", "main"),
                ("prod", "api-2", "main"),
            ],
            owner=None,
        ),
    )

    displayed = screens.displayed_pane_context()

    assert displayed is not None
    pane = displayed.context
    assert pane.kind == "pods"
    assert pane.scope == "prod"
    assert pane.selected is None


def test_open_logs_take_precedence_over_an_inline_describe_pane() -> None:
    screens = _display_screens(
        screen=object(),
        _describe_pane=SimpleNamespace(
            display=True,
            resource_identity=ResourceIdentity(
                kind="Pod",
                namespace="prod",
                name="old-describe",
                uid="old",
            ),
        ),
        _logs=SimpleNamespace(
            mode="l",
            current_triples=[("prod", "live-pod", "main")],
            owner=None,
        ),
    )

    displayed = screens.displayed_pane_context()

    assert displayed is not None
    assert displayed.context.selected is not None
    assert displayed.context.selected.name == "live-pod"
