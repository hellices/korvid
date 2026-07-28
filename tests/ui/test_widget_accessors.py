"""U2 typed accessors for the lifecycle-stable root widgets (issue #91).

The accessors replace repeated raw `query_one(Class)` calls at the hot
sites. They must return the same mounted instance a raw query would, and
must keep raising `NoMatches` when the widget is not composed so the
intentional startup/shutdown guards stay meaningful.
"""

from pathlib import Path

import pytest
from textual.css.query import NoMatches

from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.describe_screen import DescribePane
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.hint_strip import HintStrip
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.status_bar import StatusBar

from .test_write_ops import Recorder, make_app


async def test_accessors_return_the_mounted_widgets(tmp_path: Path) -> None:
    app = make_app(Recorder(), tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._log_pane is app.query_one(LogPane)
        assert app._describe_pane is app.query_one(DescribePane)
        assert app._command_bar is app.query_one(CommandBar)
        assert app._filter_bar is app.query_one(FilterBar)
        assert app._namespace_picker is app.query_one(NamespacePicker)
        assert app._hint_strip is app.query_one(HintStrip)
        assert app._status_bar is app.query_one(StatusBar)
        assert app._agent_panel is app.query_one(AgentPanel)


async def test_agent_panel_accessor_raises_when_agent_unavailable(tmp_path: Path) -> None:
    """AgentPanel is composed only when an agent is wired; the accessor
    must keep the `NoMatches` contract for the guarded call sites."""
    app = make_app(Recorder(), tmp_path / "audit.jsonl")
    app._agent_available = False  # compose() reads this at mount
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches, match="AgentPanel"):
            _ = app._agent_panel
