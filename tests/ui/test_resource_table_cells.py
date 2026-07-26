"""Pod table cell styling: STATUS/READY/RESTARTS colored by semantic tokens."""

from rich.text import Text

from korvid.k8s.metrics import ContainerUsage, PodMetrics
from korvid.k8s.models import ContainerLimits, PodSummary
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
            "cpu_request_cores": 0.2,
            "mem_request_bytes": 256 * 2**20,
        }
        defaults.update(kwargs)
        # type ignore: heterogeneous defaults dict widens every field to object
        return PodSummary(**defaults)  # type: ignore[arg-type]  # kwargs typed object; fields validated by the dataclass

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

    def test_thresholds_cap_at_yellow_without_limit(self) -> None:
        """Issue #50: without a limit, bursting above request is expected
        Burstable behavior - alarm color is capped at yellow, never red."""
        pod = self._pod()
        hot = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.19, memory_bytes=250 * 2**20
        )
        _, cpu_pct, _, mem_pct = _usage_cells(pod, hot)
        assert cpu_pct.plain == "95"
        assert _style_of(cpu_pct) == "yellow"
        assert mem_pct.plain == "98"
        assert _style_of(mem_pct) == "yellow"

    def test_no_request_gives_dash_percent(self) -> None:
        pod = self._pod(
            cpu_request="-", mem_request="-", cpu_request_cores=None, mem_request_bytes=None
        )
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


class TestUsagePercentPrecision:
    """Review round 1: percentages must come from exact request values, not
    the display-rounded strings (1500Ki renders as 1Mi but is not 1Mi)."""

    def _pod(self) -> PodSummary:
        return PodSummary(
            name="web-1",
            namespace="default",
            phase="Running",
            ready="1/1",
            restarts=0,
            node="n1",
            cpu_request="200m",
            mem_request="1Mi",  # display string rounds 1500Ki down
            cpu_request_cores=0.2,
            mem_request_bytes=1500 * 2**10,
        )

    def test_memory_percent_from_exact_request(self) -> None:
        metrics = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.1, memory_bytes=750 * 2**10
        )
        _, _, _, mem_pct = _usage_cells(self._pod(), metrics)
        assert mem_pct.plain == "50"

    def test_boundary_style_matches_displayed_value(self) -> None:
        """69.9% displays as 70 - it must be yellow like 70, not green."""
        metrics = PodMetrics(name="web-1", namespace="default", cpu_cores=0.1398, memory_bytes=0)
        _, cpu_pct, _, _ = _usage_cells(self._pod(), metrics)
        assert cpu_pct.plain == "70"
        assert _style_of(cpu_pct) == "yellow"


class TestLimitBasedSeverity:
    """Issue #50: the displayed number stays usage-vs-request, but the color
    keys off proximity to the *limit* (OOMKill / throttle territory) when one
    is declared. Requests are scheduling guarantees, not caps: AKS addons
    routinely burst to 300% of a tiny request while sitting far below limit."""

    def _pod(self, **kwargs: object) -> PodSummary:
        defaults: dict[str, object] = {
            "name": "web-1",
            "namespace": "default",
            "phase": "Running",
            "ready": "1/1",
            "restarts": 0,
            "node": "n1",
            "cpu_request": "100m",
            "mem_request": "32Mi",
            "cpu_request_cores": 0.1,
            "mem_request_bytes": 32 * 2**20,
            "cpu_limit_cores": 1.0,
            "mem_limit_bytes": 200 * 2**20,
        }
        defaults.update(kwargs)
        return PodSummary(**defaults)  # type: ignore[arg-type]  # kwargs typed object; fields validated by the dataclass

    def test_burst_above_request_far_below_limit_is_green(self) -> None:
        """defender-publisher case: 91Mi used, 32Mi request (283%), 200Mi
        limit (45%) - healthy, must render green despite the big number."""
        metrics = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.15, memory_bytes=91 * 2**20
        )
        _, cpu_pct, _, mem_pct = _usage_cells(self._pod(), metrics)
        assert mem_pct.plain == "284"
        assert _style_of(mem_pct) == "green"
        assert cpu_pct.plain == "150"
        assert _style_of(cpu_pct) == "green"

    def test_near_limit_is_bold_red(self) -> None:
        """95% of the 200Mi limit is OOMKill territory regardless of the
        request-based number shown."""
        metrics = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.95, memory_bytes=190 * 2**20
        )
        _, cpu_pct, _, mem_pct = _usage_cells(self._pod(), metrics)
        assert _style_of(mem_pct) == "bold red"
        assert _style_of(cpu_pct) == "bold red"

    def test_warn_band_of_limit_is_yellow(self) -> None:
        metrics = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.75, memory_bytes=150 * 2**20
        )
        _, cpu_pct, _, mem_pct = _usage_cells(self._pod(), metrics)
        assert _style_of(mem_pct) == "yellow"
        assert _style_of(cpu_pct) == "yellow"

    def test_limit_severity_uses_rounded_percent(self) -> None:
        """89.9% of limit rounds to 90 - must be red like 90, not yellow."""
        metrics = PodMetrics(
            name="web-1",
            namespace="default",
            cpu_cores=0.1,
            memory_bytes=int(200 * 2**20 * 0.899),
        )
        _, _, _, mem_pct = _usage_cells(self._pod(), metrics)
        assert _style_of(mem_pct) == "bold red"


class TestPerContainerSeverity:
    """Review fix (PR #51 r4): limits are enforced per container - a 100Mi
    sidecar at 95Mi is at OOM risk even when the pod aggregate looks idle."""

    def _pod(self, container_limits: tuple[ContainerLimits, ...], **kwargs: object) -> PodSummary:
        defaults: dict[str, object] = {
            "name": "web-1",
            "namespace": "default",
            "phase": "Running",
            "ready": "1/1",
            "restarts": 0,
            "node": "n1",
            "mem_request_bytes": 100 * 2**20,
            "container_limits": container_limits,
        }
        defaults.update(kwargs)
        return PodSummary(**defaults)  # type: ignore[arg-type]  # kwargs typed object; fields validated by the dataclass

    def test_sidecar_near_its_own_limit_is_red_despite_idle_aggregate(self) -> None:
        pod = self._pod(
            (
                ContainerLimits(name="app", cpu_cores=None, mem_bytes=900 * 2**20),
                ContainerLimits(name="sidecar", cpu_cores=None, mem_bytes=100 * 2**20),
            )
        )
        metrics = PodMetrics(
            name="web-1",
            namespace="default",
            cpu_cores=0.0,
            memory_bytes=100 * 2**20,
            containers=(
                ContainerUsage(name="app", cpu_cores=0.0, memory_bytes=5 * 2**20),
                ContainerUsage(name="sidecar", cpu_cores=0.0, memory_bytes=95 * 2**20),
            ),
        )
        _, _, _, mem_pct = _usage_cells(pod, metrics)
        assert _style_of(mem_pct) == "bold red"  # sidecar at 95% of its limit

    def test_all_containers_far_below_their_limits_is_green(self) -> None:
        pod = self._pod(
            (
                ContainerLimits(name="app", cpu_cores=None, mem_bytes=900 * 2**20),
                ContainerLimits(name="sidecar", cpu_cores=None, mem_bytes=200 * 2**20),
            ),
            mem_request_bytes=32 * 2**20,
        )
        metrics = PodMetrics(
            name="web-1",
            namespace="default",
            cpu_cores=0.0,
            memory_bytes=100 * 2**20,
            containers=(
                ContainerUsage(name="app", cpu_cores=0.0, memory_bytes=50 * 2**20),
                ContainerUsage(name="sidecar", cpu_cores=0.0, memory_bytes=50 * 2**20),
            ),
        )
        _, _, _, mem_pct = _usage_cells(pod, metrics)
        assert mem_pct.plain == "312"  # number stays request-based
        assert _style_of(mem_pct) == "green"

    def test_unlimited_container_bursting_caps_at_yellow(self) -> None:
        pod = self._pod(
            (
                ContainerLimits(name="app", cpu_cores=None, mem_bytes=None),
                ContainerLimits(name="sidecar", cpu_cores=None, mem_bytes=200 * 2**20),
            )
        )
        metrics = PodMetrics(
            name="web-1",
            namespace="default",
            cpu_cores=0.0,
            memory_bytes=200 * 2**20,
            containers=(
                ContainerUsage(name="app", cpu_cores=0.0, memory_bytes=190 * 2**20),
                ContainerUsage(name="sidecar", cpu_cores=0.0, memory_bytes=10 * 2**20),
            ),
        )
        _, _, _, mem_pct = _usage_cells(pod, metrics)
        assert _style_of(mem_pct) == "yellow"  # 200% of request, no ceiling known

    def test_limited_container_red_wins_over_unlimited_yellow(self) -> None:
        pod = self._pod(
            (
                ContainerLimits(name="app", cpu_cores=None, mem_bytes=None),
                ContainerLimits(name="sidecar", cpu_cores=None, mem_bytes=100 * 2**20),
            )
        )
        metrics = PodMetrics(
            name="web-1",
            namespace="default",
            cpu_cores=0.0,
            memory_bytes=285 * 2**20,
            containers=(
                ContainerUsage(name="app", cpu_cores=0.0, memory_bytes=190 * 2**20),
                ContainerUsage(name="sidecar", cpu_cores=0.0, memory_bytes=95 * 2**20),
            ),
        )
        _, _, _, mem_pct = _usage_cells(pod, metrics)
        assert _style_of(mem_pct) == "bold red"

    def test_pod_level_limit_does_not_mask_container_near_own_limit(self) -> None:
        """Review fix (PR #51 r5): the pod cgroup caps the aggregate, but each
        container cgroup still enforces its own limit - both ceilings count."""
        pod = self._pod(
            (
                ContainerLimits(name="app", cpu_cores=None, mem_bytes=None),
                ContainerLimits(name="sidecar", cpu_cores=None, mem_bytes=100 * 2**20),
            ),
            mem_limit_bytes=1024 * 2**20,  # whole-pod limit: aggregate is only ~10%
        )
        metrics = PodMetrics(
            name="web-1",
            namespace="default",
            cpu_cores=0.0,
            memory_bytes=100 * 2**20,
            containers=(
                ContainerUsage(name="app", cpu_cores=0.0, memory_bytes=5 * 2**20),
                ContainerUsage(name="sidecar", cpu_cores=0.0, memory_bytes=95 * 2**20),
            ),
        )
        _, _, _, mem_pct = _usage_cells(pod, metrics)
        assert _style_of(mem_pct) == "bold red"  # sidecar at 95% of its own limit

    def test_pod_level_limit_still_colors_when_no_container_samples(self) -> None:
        pod = self._pod((), mem_limit_bytes=100 * 2**20)
        metrics = PodMetrics(
            name="web-1", namespace="default", cpu_cores=0.0, memory_bytes=95 * 2**20
        )
        _, _, _, mem_pct = _usage_cells(pod, metrics)
        assert _style_of(mem_pct) == "bold red"  # 95% of the pod-level limit
