"""Pod table cell styling: STATUS/READY/RESTARTS colored by semantic tokens."""

from rich.text import Text

from korvid.ui.widgets.resource_table import _phase_cell, _ready_cell, _restarts_cell


def _style_of(cell: Text) -> str:
    return str(cell.style)


class TestPhaseCell:
    def test_running_green(self) -> None:
        cell = _phase_cell("Running")
        assert cell.plain == "Running"
        assert _style_of(cell) == "green"

    def test_crashloopbackoff_bold_red(self) -> None:
        cell = _phase_cell("CrashLoopBackOff")
        assert cell.plain == "CrashLoopBackOff"
        assert _style_of(cell) == "bold red"

    def test_terminating_magenta(self) -> None:
        assert _style_of(_phase_cell("Terminating")) == "magenta"


class TestReadyCell:
    def test_full_green(self) -> None:
        cell = _ready_cell("2/2")
        assert cell.plain == "2/2"
        assert _style_of(cell) == "green"

    def test_none_ready_red(self) -> None:
        assert _style_of(_ready_cell("0/2")) == "red"

    def test_partial_yellow(self) -> None:
        assert _style_of(_ready_cell("1/2")) == "yellow"


class TestRestartsCell:
    def test_zero_dim(self) -> None:
        cell = _restarts_cell(0)
        assert cell.plain == "0"
        assert _style_of(cell) == "dim"

    def test_low_yellow(self) -> None:
        assert _style_of(_restarts_cell(3)) == "yellow"

    def test_high_bold_red(self) -> None:
        cell = _restarts_cell(12)
        assert cell.plain == "12"
        assert _style_of(cell) == "bold red"
