"""Text painted inside a widget's visible bounds."""

from textual.widget import Widget


def painted_text(widget: Widget) -> str:
    """Read styled screen lines, including clipping and column widths."""
    return "\n".join(strip.text for strip in widget.render_lines(widget.region.reset_offset))
