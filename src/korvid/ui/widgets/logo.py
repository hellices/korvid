"""Startup splash logo — a corvid, of course.

Shown centered in the table area from launch until the first store
notification renders rows (or the empty-state guidance takes over), so it
never blocks or delays startup.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

_RAVEN = r"""
    __
   ( o>   caw!
   /\\
  _\_v_
"""

_WORDMARK = r"""
 ██╗  ██╗ ██████╗ ██████╗ ██╗   ██╗██╗██████╗
 ██║ ██╔╝██╔═══██╗██╔══██╗██║   ██║██║██╔══██╗
 █████╔╝ ██║   ██║██████╔╝██║   ██║██║██║  ██║
 ██╔═██╗ ██║   ██║██╔══██╗╚██╗ ██╔╝██║██║  ██║
 ██║  ██╗╚██████╔╝██║  ██║ ╚████╔╝ ██║██████╔╝
 ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝  ╚═══╝  ╚═╝╚═════╝
"""

TAGLINE = "korvid — a corvid for your cluster"


def _pad_block(art: str) -> str:
    """Pad every line to equal width so per-line centering keeps the shape."""
    lines = art.strip("\n").split("\n")
    width = max(len(line) for line in lines)
    return "\n".join(line.ljust(width) for line in lines)


def build_logo() -> Text:
    """Assemble the splash as a Rich Text (no markup parsing surprises)."""
    logo = Text()
    logo.append(_pad_block(_RAVEN), style="bright_black")
    logo.append("\n\n")
    logo.append(_pad_block(_WORDMARK), style="bold cyan")
    logo.append(f"\n\n{TAGLINE}\n", style="dim")
    logo.append("connecting to cluster…", style="dim italic")
    return logo


class SplashLogo(Static):
    """Centered splash; the App hides it on the first table render."""

    DEFAULT_CSS = """
    SplashLogo {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    """

    def __init__(self) -> None:
        super().__init__(build_logo(), markup=False)
