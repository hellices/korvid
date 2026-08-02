"""Status bar + the corvid busy indicator (issue #143).

Long operations publish transient labels through the app's
``_progress_labels`` machinery; while any label is live the bar animates a
small ASCII corvid next to it so a 20s helm dry-run visibly *moves*
instead of reading as frozen. Frame cycling is a pure function
(`bird_frame`) so tests never assert on wall-clock timing.
"""

from __future__ import annotations

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

#: Pecking corvid, pure ASCII (no emoji dependency), equal-width frames so
#: the swap never jitters the line. The mascot from widgets/logo.py, at work.
BIRD_FRAMES: tuple[str, ...] = (
    "( o>",
    "( o-",
    "(_o>",
)

#: Seconds between frame swaps: calm, and cheap on the event loop.
BIRD_INTERVAL = 0.5


def bird_frame(tick: int) -> str:
    """The frame for one animation tick; cycles through `BIRD_FRAMES`."""
    return BIRD_FRAMES[tick % len(BIRD_FRAMES)]


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar.protected {
        background: $error 25%;
    }
    """

    _anim_timer: Timer | None = None
    _tick: int = 0

    def update_status(
        self,
        context: str | None,
        namespace: str,
        agent_label: str,
        breadcrumb: str = "",
        mcp_label: str = "",
        filter_label: str = "",
        progress_label: str = "",
        proposals_label: str = "",
        protected: bool = False,
    ) -> None:
        self._last_status = (
            context,
            namespace,
            agent_label,
            breadcrumb,
            mcp_label,
            filter_label,
            progress_label,
            proposals_label,
            protected,
        )
        self._sync_animation(progress_label)
        self._render_line()

    def _sync_animation(self, progress_label: str) -> None:
        """Run the bird exactly while a progress label is live: started on
        the first label, stopped (and reset to frame 0) with the last."""
        if progress_label and self._anim_timer is None:
            self._tick = 0
            self._anim_timer = self.set_interval(BIRD_INTERVAL, self._advance_bird)
        elif not progress_label and self._anim_timer is not None:
            self._anim_timer.stop()
            self._anim_timer = None
            self._tick = 0

    def _advance_bird(self) -> None:
        """One timer tick: swap the frame in place (no layout change)."""
        self._tick += 1
        self._render_line()

    def _render_line(self) -> None:
        (
            context,
            namespace,
            agent_label,
            breadcrumb,
            mcp_label,
            filter_label,
            progress_label,
            proposals_label,
            protected,
        ) = self._last_status
        ctx = context or "(current)"
        trail = f"  {breadcrumb}" if breadcrumb else ""
        mcp = f"  ⇄{mcp_label}" if mcp_label else ""
        flt = f"  ▼{filter_label}" if filter_label else ""
        prog = f"  {bird_frame(self._tick)} {progress_label}" if progress_label else ""
        props = f"  ⚑{proposals_label}" if proposals_label else ""
        # Text keeps user-entered filter text literal (never Rich markup).
        line = Text()
        if protected:
            # Protected contexts (issue #83): loud red marker + tinted bar.
            line.append(" ⛨ PROTECTED ", style="bold white on red")
            line.append("  ")
        line.append(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}{mcp}{flt}{prog}{props}{trail}")
        self.set_class(protected, "protected")
        self.update(line)
