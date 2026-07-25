"""Semantic color tokens shared by TUI widgets.

Maps Kubernetes state strings to Rich styles so every widget colors the
same state the same way (k9s / kubecolor conventions).
"""

from __future__ import annotations

_PHASE_STYLES: dict[str, str] = {
    "Running": "green",
    "Succeeded": "dim green",
    "Completed": "dim green",
    "Pending": "yellow",
    "ContainerCreating": "yellow",
    "PodInitializing": "yellow",
    "SchedulingGated": "yellow",
    "NotReady": "yellow",
    "Terminating": "magenta",
}

# Any phase containing one of these substrings is an error state.
_ERROR_MARKERS = (
    "BackOff",
    "Err",
    "Error",
    "Failed",
    "OOMKilled",
    "Evicted",
    "CrashLoop",
    "Signal:",
    "ExitCode:",
)

# Restart counts above this render bold red instead of yellow.
RESTARTS_RED_THRESHOLD = 5


def phase_style(phase: str) -> str:
    """Rich style for a pod phase / display status string."""
    style = _PHASE_STYLES.get(phase)
    if style is not None:
        return style
    if any(marker in phase for marker in _ERROR_MARKERS):
        return "bold red"
    if phase.startswith("Init:"):
        return "yellow"
    return "dim"


def ready_style(ready: str) -> str:
    """Rich style for a READY cell like '1/2' (green full, yellow partial, red none)."""
    ready_count, sep, total_count = ready.partition("/")
    if sep != "/" or not ready_count.isdigit() or not total_count.isdigit():
        return "dim"
    ready_n, total_n = int(ready_count), int(total_count)
    if total_n == 0:
        return "dim"
    if ready_n == total_n:
        return "green"
    if ready_n == 0:
        return "red"
    return "yellow"


def restarts_style(restarts: int) -> str:
    """Rich style for a RESTARTS cell (dim 0, yellow low, bold red high)."""
    if restarts == 0:
        return "dim"
    if restarts <= RESTARTS_RED_THRESHOLD:
        return "yellow"
    return "bold red"
