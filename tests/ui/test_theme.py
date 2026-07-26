"""Semantic color tokens: phase/ready/restarts style mapping."""

from korvid.ui.theme import phase_style, ready_style, restarts_style, usage_style


class TestPhaseStyle:
    def test_running_is_green(self) -> None:
        assert phase_style("Running") == "green"

    def test_succeeded_and_completed_are_dim_green(self) -> None:
        assert phase_style("Succeeded") == "dim green"
        assert phase_style("Completed") == "dim green"

    def test_pending_states_are_yellow(self) -> None:
        assert phase_style("Pending") == "yellow"
        assert phase_style("ContainerCreating") == "yellow"
        assert phase_style("PodInitializing") == "yellow"

    def test_error_states_are_bold_red(self) -> None:
        for phase in (
            "CrashLoopBackOff",
            "ImagePullBackOff",
            "ErrImagePull",
            "Error",
            "OOMKilled",
            "Failed",
            "Evicted",
            "CreateContainerConfigError",
        ):
            assert phase_style(phase) == "bold red", phase

    def test_terminating_is_magenta(self) -> None:
        assert phase_style("Terminating") == "magenta"

    def test_unknown_phase_falls_back_to_dim(self) -> None:
        assert phase_style("SomethingNew") == "dim"
        assert phase_style("Unknown") == "dim"


class TestReadyStyle:
    def test_all_ready_is_green(self) -> None:
        assert ready_style("1/1") == "green"
        assert ready_style("3/3") == "green"

    def test_none_ready_is_red(self) -> None:
        assert ready_style("0/1") == "red"
        assert ready_style("0/3") == "red"

    def test_partially_ready_is_yellow(self) -> None:
        assert ready_style("1/2") == "yellow"
        assert ready_style("2/3") == "yellow"

    def test_zero_of_zero_is_dim(self) -> None:
        assert ready_style("0/0") == "dim"

    def test_malformed_is_dim(self) -> None:
        assert ready_style("-") == "dim"
        assert ready_style("garbage") == "dim"


class TestRestartsStyle:
    def test_zero_is_dim(self) -> None:
        assert restarts_style(0) == "dim"

    def test_low_count_is_yellow(self) -> None:
        assert restarts_style(1) == "yellow"
        assert restarts_style(5) == "yellow"

    def test_high_count_is_bold_red(self) -> None:
        assert restarts_style(6) == "bold red"
        assert restarts_style(42) == "bold red"


class TestInitPhaseStyle:
    def test_init_error_states_are_bold_red(self) -> None:
        assert phase_style("Init:CrashLoopBackOff") == "bold red"
        assert phase_style("Init:Error") == "bold red"

    def test_init_progress_is_yellow(self) -> None:
        assert phase_style("Init:0/2") == "yellow"

    def test_signal_and_exitcode_are_bold_red(self) -> None:
        assert phase_style("Signal:9") == "bold red"
        assert phase_style("ExitCode:2") == "bold red"
        assert phase_style("Init:ExitCode:3") == "bold red"

    def test_scheduling_gated_is_yellow(self) -> None:
        assert phase_style("SchedulingGated") == "yellow"

    def test_not_ready_is_yellow(self) -> None:
        assert phase_style("NotReady") == "yellow"


class TestUsageStyle:
    def test_none_dim(self) -> None:
        assert usage_style(None) == "dim"

    def test_low_green(self) -> None:
        assert usage_style(0) == "green"
        assert usage_style(69.9) == "green"

    def test_warn_yellow(self) -> None:
        assert usage_style(70) == "yellow"
        assert usage_style(89.9) == "yellow"

    def test_high_bold_red(self) -> None:
        assert usage_style(90) == "bold red"
        assert usage_style(250) == "bold red"
