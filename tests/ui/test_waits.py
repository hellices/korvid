"""Minimal regression tests for the shared wait helper."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Select

from .waits import WaitTimeout, screen_ready, until


class _FakePilot:
    async def pause(self, delay: float) -> None:
        return None


class _ClockedPilot:
    """Adds up the time `until` asks to pause, without sleeping."""

    def __init__(self) -> None:
        self.paused = 0.0

    async def pause(self, delay: float) -> None:
        self.paused += delay


async def test_until_checks_condition_again_after_the_final_pause() -> None:
    pilot = _FakePilot()
    checks = 0

    def cond() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    await until(pilot, cond, timeout=0.05)
    assert checks == 2


async def test_until_default_outlasts_a_multi_second_durable_write_stall() -> None:
    """The default only keeps a broken test from hanging, so it has to outlast
    the `fsync` stall a loaded runner can add to a wait that spans an audit
    append. Five seconds did not (#464)."""
    pilot = _ClockedPilot()
    with pytest.raises(WaitTimeout, match=r"condition not met within 30\.0s"):
        await until(pilot, lambda: False)
    assert pilot.paused == pytest.approx(30.0)


async def test_until_timeout_is_not_an_assertion_error() -> None:
    pilot = _FakePilot()
    with pytest.raises(WaitTimeout, match=r"condition not met within 0\.05s") as caught:
        await until(pilot, lambda: False, timeout=0.05)
    assert not isinstance(caught.value, AssertionError)


class _Node:
    def __init__(self, *, mounted: bool = True, children: tuple[_Node, ...] = ()) -> None:
        self.is_mounted = mounted
        self.children = children

    def query(self, selector: str) -> tuple[_Node, ...]:
        assert selector == "*"
        return self.children


class _Prompt(_Node):
    pass


class _App:
    def __init__(self, screen: _Node) -> None:
        self.screen = screen


@pytest.mark.parametrize(
    ("screen", "ready"),
    [
        pytest.param(_Node(), False, id="another screen"),
        pytest.param(_Prompt(mounted=False), False, id="screen still mounting"),
        pytest.param(
            _Prompt(children=(_Node(), _Node(mounted=False))), False, id="a widget still mounting"
        ),
        pytest.param(_Prompt(children=(_Node(), _Node())), True, id="every widget mounted"),
    ],
)
def test_screen_ready_waits_for_the_screen_and_every_widget(screen: _Node, ready: bool) -> None:
    assert screen_ready(_App(screen), _Prompt) is ready


async def test_screen_ready_holds_only_once_a_select_has_mounted() -> None:
    class Prompt(ModalScreen[None]):
        def compose(self) -> ComposeResult:
            yield Select([("stable", "stable"), ("fast", "fast")], value="stable")

    app = App[None]()
    async with app.run_test() as pilot:
        app.push_screen(Prompt())
        await until(pilot, lambda: screen_ready(app, Prompt), label="prompt ready")

        assert app.screen.query_one(Select).is_mounted
