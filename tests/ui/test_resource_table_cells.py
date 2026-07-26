"""Pod table cell styling: STATUS/READY/RESTARTS colored by semantic tokens."""

from rich.text import Text

from korvid.k8s.metrics import PodMetrics
from korvid.k8s.models import PodSummary
from korvid.ui.widgets.resource_table import (
    _phase_cell,
    _ready_cell,
    _restarts_cell,
    _usage_cells,
)


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


class TestUsageCells:
    """CPU / %CPU/R / MEM / %MEM/R cells joined from PodMetrics (issue #12)."""

    def _pod(self, **kwargs: object) -> PodSummary:
        defaults: dict[str, object] = {
            "name": "web-1",
            "namespace": "default",
            "phase": "Running",
            "ready": "1/1",
            "restarts": 0,
            "node": "n1",
            "cpu_request": "200m",
            "mem_request": "256Mi",
        }
        defaults.update(kwargs)
        return PodSummary(**defaults)  # type: ignore[arg-type]

    def test_usage_and_percent_of_request(self) -> None:
        pod = self._pod()
        metrics = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.1, memory_bytes=128 * 2**20
        )
        cpu, cpu_pct, mem, mem_pct = _usage_cells(pod, metrics)
        assert cpu.plain == "100m"
        assert cpu_pct.plain == "50"
        assert _style_of(cpu_pct) == "green"
        assert mem.plain == "128Mi"
        assert mem_pct.plain == "50"
        assert _style_of(mem_pct) == "green"

    def test_thresholds_color_percent(self) -> None:
        pod = self._pod()
        hot = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.19, memory_bytes=250 * 2**20
        )
        _, cpu_pct, _, mem_pct = _usage_cells(pod, hot)
        assert cpu_pct.plain == "95"
        assert _style_of(cpu_pct) == "bold red"
        assert mem_pct.plain == "98"
        assert _style_of(mem_pct) == "bold red"

    def test_no_request_gives_dash_percent(self) -> None:
        pod = self._pod(cpu_request="-", mem_request="-")
        metrics = PodMetrics(name="web-1", namespace="default", cpu_cores=0.1, memory_bytes=2**20)
        cpu, cpu_pct, _mem, mem_pct = _usage_cells(pod, metrics)
        assert cpu.plain == "100m"
        assert cpu_pct.plain == "-"
        assert _style_of(cpu_pct) == "dim"
        assert mem_pct.plain == "-"

    def test_no_metrics_gives_all_dashes(self) -> None:
        cells = _usage_cells(self._pod(), None)
        assert [c.plain for c in cells] == ["-", "-", "-", "-"]
        assert all(_style_of(c) == "dim" for c in cells)
