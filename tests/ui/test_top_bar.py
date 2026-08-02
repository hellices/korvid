"""Grouped, collapsible top bar (issue #142): logo mark, per-view dynamic
members inside fixed groups, priority keys in collapsed mode, width
degradation, and config persistence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import yaml

from korvid.core.config import KorvidConfig, load_config, save_topbar_state
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.top_bar import (
    COLLAPSED_KEYS,
    LOGO_MARK,
    MIN_EXPANDED_WIDTH,
    KeyEntry,
    TopBar,
    build_collapsed,
    build_expanded,
    build_legend,
    collapsed_entries,
    group_of,
)

from .waits import until

# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _entry(action: str, key: str = "x", description: str = "desc") -> KeyEntry:
    return KeyEntry(key=key, action=action, description=description)


def test_group_classification_covers_the_fixed_groups() -> None:
    assert group_of("open_command") == "Nav"
    assert group_of("sort_picker") == "Sort"
    assert group_of("describe") == "Actions"  # unlisted -> Actions
    assert group_of("helm_install") == "Actions"
    assert group_of("log_format") == "Logs"
    assert group_of("toggle_agent") == "Agent"


def test_collapsed_entries_follow_the_fixed_priority() -> None:
    entries = [
        _entry("open_command"),
        _entry("shell"),
        _entry("describe"),
        _entry("logs"),
        _entry("cordon_node"),
        _entry("port_forward"),
    ]
    picked = [e.action for e in collapsed_entries(entries)]
    # describe > logs > shell > cordon_node per PRIORITY_ACTIONS order
    assert picked == ["describe", "logs", "shell", "cordon_node"]
    assert len(picked) <= COLLAPSED_KEYS


def test_collapsed_entries_skip_actions_outside_the_priority_list() -> None:
    picked = collapsed_entries([_entry("quit"), _entry("describe")])
    assert [e.action for e in picked] == ["describe"]


def test_collapsed_line_has_logo_view_keys_and_more_hint() -> None:
    text = build_collapsed("pods", [_entry("describe", "d", "Describe")], "~").plain
    assert LOGO_MARK in text
    assert "pods" in text
    assert "Describe" in text
    assert "more" in text
    assert "~" in text  # the toggle key is advertised


def test_expanded_legend_groups_and_orders_members() -> None:
    entries = [
        _entry("toggle_agent", "ctrl+a", "AI"),
        _entry("describe", "d", "Describe"),
        _entry("sort_picker", "o", "Sort by column"),
        _entry("open_filter", "/", "Filter"),
    ]
    text = build_expanded("pods", entries).plain
    # fixed group order: Nav before Sort before Actions before Agent
    assert text.index("Nav") < text.index("Sort") < text.index("Actions") < text.index("Agent")
    assert "Describe" in text
    # static handler keys advertised in their groups
    assert "drill" in text  # enter under Nav
    assert "ctrl+w" in text  # pane chord under Panes


def test_expanded_legend_omits_empty_groups() -> None:
    text = build_expanded("pods", [_entry("describe", "d", "Describe")]).plain
    assert "Sort" not in text
    assert "Agent" not in text


def test_narrow_terminal_forces_the_collapsed_form() -> None:
    entries = [_entry("describe", "d", "Describe"), _entry("sort_picker", "o", "Sort")]
    wide = build_legend("pods", entries, expanded=True, width=MIN_EXPANDED_WIDTH, toggle_key="~")
    narrow = build_legend(
        "pods", entries, expanded=True, width=MIN_EXPANDED_WIDTH - 1, toggle_key="~"
    )
    assert "Sort" in wide.plain  # group titles = expanded
    assert "Sort" not in narrow.plain
    assert "more" in narrow.plain  # collapsed form


# ---------------------------------------------------------------------------
# app wiring
# ---------------------------------------------------------------------------

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False)

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "nodes": _NODES_META,
    "helm": HELM_RELEASES_META,
    "helmreleases": HELM_RELEASES_META,
}


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name, namespace="default", phase="Running", ready="1/1", restarts=0, node=None
    )


def make_app(
    *,
    config: KorvidConfig | None = None,
    save_topbar: object = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "pods":
            yield ("ADDED", _pod("api-1"))
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=config or KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        save_topbar=save_topbar,  # type: ignore[arg-type]  # test seam
    )


def _bar_text(app: KorvidApp) -> str:
    return str(app.query_one(TopBar).render())


async def test_top_bar_replaces_the_footer_and_shows_the_logo() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await until(pilot, lambda: LOGO_MARK in _bar_text(app), label="logo in the bar")
        assert "pods" in _bar_text(app)  # the current view is named


async def test_collapsed_by_default_with_priority_keys_and_more_hint() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await until(pilot, lambda: "Describe" in _bar_text(app), label="priority key shown")
        text = _bar_text(app)
        assert "more" in text  # collapsed marker
        assert "Sort" not in text  # no group titles while collapsed


async def test_toggle_key_expands_to_the_grouped_legend() -> None:
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: "more" in _bar_text(app), label="collapsed first")
        await pilot.press("tilde")
        await until(pilot, lambda: "Sort" in _bar_text(app), label="groups visible")
        text = _bar_text(app)
        assert "Nav" in text
        assert "Agent" in text
        await pilot.press("tilde")  # toggles back
        await until(pilot, lambda: "more" in _bar_text(app), label="collapsed again")


async def test_group_members_follow_the_view() -> None:
    """Fixed groups, dynamic members (issue #142 decision): the same bar
    swaps Actions members when the view changes."""
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("tilde")
        await until(pilot, lambda: "Logs" in _bar_text(app), label="pod actions visible")
        assert "Cordon" not in _bar_text(app)
        await pilot.press("colon")
        for ch in "nodes":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: "Cordon" in _bar_text(app), label="node actions visible")
        assert "Install chart" not in _bar_text(app)


async def test_config_expanded_starts_expanded() -> None:
    app = make_app(config=KorvidConfig(namespace="default", ui_topbar_expanded=True))
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: "Sort" in _bar_text(app), label="expanded from config")


async def test_toggle_persists_through_the_injected_callback() -> None:
    saved: list[bool] = []
    app = make_app(save_topbar=saved.append)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: LOGO_MARK in _bar_text(app), label="bar ready")
        await pilot.press("tilde")
        await until(pilot, lambda: saved == [True], label="expanded persisted")
        await pilot.press("tilde")
        await until(pilot, lambda: saved == [True, False], label="collapsed persisted")


# ---------------------------------------------------------------------------
# config parsing + persistence round trip
# ---------------------------------------------------------------------------


def test_ui_topbar_expanded_parses_from_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("ui:\n  topbar: expanded\n")
    assert load_config(path).ui_topbar_expanded is True
    path.write_text("ui:\n  topbar: collapsed\n")
    assert load_config(path).ui_topbar_expanded is False
    path.write_text("namespace: default\n")
    assert load_config(path).ui_topbar_expanded is False  # unset -> collapsed


def test_save_topbar_state_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("namespace: prod\nui:\n  other: keep\n")
    save_topbar_state(path, expanded=True)
    raw = yaml.safe_load(path.read_text())
    assert raw["ui"]["topbar"] == "expanded"
    assert raw["ui"]["other"] == "keep"  # read-modify-write
    assert raw["namespace"] == "prod"
    save_topbar_state(path, expanded=False)
    assert yaml.safe_load(path.read_text())["ui"]["topbar"] == "collapsed"


def test_save_topbar_state_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.yaml"
    save_topbar_state(path, expanded=True)
    assert yaml.safe_load(path.read_text())["ui"]["topbar"] == "expanded"
