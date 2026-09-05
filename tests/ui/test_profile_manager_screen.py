"""Tests for the profile manager screen (Task 9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static

from korvid.agent.model_profiles import ModelCatalog, ModelEntry
from korvid.core.config import ModelConnectionConfig, ModelConnectionsConfig
from korvid.ui.widgets.profile_manager_screen import (
    ProfileManagerResult,
    ProfileManagerScreen,
)

from .waits import until


class _FakeCatalog(ModelCatalog):
    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        return ()

    def entry(self, reference: str) -> ModelEntry | None:
        return None

    def auth_methods(self, reference: str, *, endpoint: str | None = None) -> tuple[Any, ...]:
        return ()

    def option_fields(self, reference: str) -> tuple[Any, ...]:
        return ()

    def endpoint_requirement(self, reference: str) -> Any:
        from korvid.agent.model_profiles import EndpointRequirement

        return EndpointRequirement.OPTIONAL

    async def discover(self, profile: ModelConnectionConfig) -> tuple[ModelEntry, ...]:
        return ()

    async def test(self, profile: ModelConnectionConfig) -> str:
        return "ok"

    async def begin_auth(self, profile: ModelConnectionConfig) -> None:
        return None

    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None:
        return None


def _make_profiles(**kwargs: str) -> ModelConnectionsConfig:
    """Build a `ModelConnectionsConfig` with the given name→model map."""
    return ModelConnectionsConfig(
        profiles={name: ModelConnectionConfig(model=model) for name, model in kwargs.items()}
    )


class _Host(App["ProfileManagerResult | None"]):
    def __init__(
        self,
        profiles: ModelConnectionsConfig,
        open_editor: Any | None = None,
    ) -> None:
        super().__init__()
        self._profiles = profiles
        self._open_editor = open_editor or (lambda p: None)
        self.result: ProfileManagerResult | str | None = "unset"
        self.screen_ref: ProfileManagerScreen | None = None

    def on_mount(self) -> None:
        screen = ProfileManagerScreen(
            profiles=self._profiles,
            catalog=_FakeCatalog(),
            open_editor=self._open_editor,
        )
        self.screen_ref = screen

        def _done(res: ProfileManagerResult | None) -> None:
            self.result = res

        self.push_screen(screen, callback=_done)

    def compose(self) -> ComposeResult:
        yield Static("")


def _highlight(app: _Host, name: str) -> None:
    """Highlight the option whose prompt starts with `name`."""
    ol = app.screen.query_one("#profile-list", OptionList)
    for i in range(ol.option_count):
        opt = ol.get_option_at_index(i)
        if str(opt.prompt).startswith(name):
            ol.highlighted = i
            return
    raise AssertionError(f"option starting with {name!r} not found")


@pytest.mark.asyncio
async def test_enter_activates_without_rewriting_the_profile_set() -> None:
    """Activation is write-only on `active`. A switch must never
    re-serialize profiles the operator did not edit."""
    profiles = ModelConnectionsConfig(
        profiles={
            "staging": ModelConnectionConfig(model="openai/gpt-4o"),
        },
        active="staging",
    )
    app = _Host(profiles)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "staging")
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result == ProfileManagerResult(activated="staging")
    assert result.edited is None


@pytest.mark.asyncio
async def test_a_profile_that_failed_to_parse_is_listed_and_deletable() -> None:
    """A rejected profile must stay visible and removable."""
    profiles = ModelConnectionsConfig(
        profiles={"good": ModelConnectionConfig(model="openai/gpt-4o")},
        unparsed={"broken": {"model": ["not", "a", "string"]}},
    )
    app = _Host(profiles)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        screen = app.screen_ref
        assert screen is not None
        listing = screen.query_one("#profile-list", OptionList)
        assert any("broken" in str(option.prompt) for option in listing._options)

        # delete it
        _highlight(app, "broken")
        await pilot.press("d")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert "broken" not in result.edited.unparsed
    assert "broken" not in result.edited.profiles


@pytest.mark.asyncio
async def test_a_profile_that_failed_to_parse_cannot_be_activated() -> None:
    """`config_error` means korvid cannot build a provider from it."""
    profiles = ModelConnectionsConfig(
        profiles={"good": ModelConnectionConfig(model="openai/gpt-4o")},
        unparsed={"broken": {"model": ["not", "a", "string"]}},
    )
    app = _Host(profiles)
    status_text: str = ""
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "broken")
        await pilot.press("enter")
        await pilot.pause(0.15)
        screen = app.screen_ref
        assert screen is not None
        status_text = str(screen.query_one("#profile-status", Static).render())

    assert app.result == "unset"  # did not dismiss
    assert "cannot be activated" in status_text


@pytest.mark.asyncio
async def test_deleting_the_active_profile_clears_the_pointer() -> None:
    profiles = ModelConnectionsConfig(
        profiles={"solo": ModelConnectionConfig(model="openai/gpt-4o")},
        active="solo",
    )
    app = _Host(profiles)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "solo")
        await pilot.press("d")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert result.edited.active is None


@pytest.mark.asyncio
async def test_deleting_the_last_profile_is_allowed() -> None:
    """An empty profile set is a valid state."""
    profiles = _make_profiles(only="openai/gpt-4o")
    app = _Host(profiles)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "only")
        await pilot.press("d")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert result.edited.profiles == {}


@pytest.mark.asyncio
async def test_profiles_render_in_insertion_order_not_sorted() -> None:
    profiles = _make_profiles(
        zeta="openai/gpt-4o",
        alpha="openai/gpt-4o",
        mid="openai/gpt-4o",
    )
    app = _Host(profiles)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        screen = app.screen_ref
        assert screen is not None
        listing = screen.query_one("#profile-list", OptionList)
        assert [str(o.prompt).split()[0] for o in listing._options][:3] == ["zeta", "alpha", "mid"]


@pytest.mark.asyncio
async def test_esc_dismisses_with_none() -> None:
    profiles = _make_profiles(p="openai/gpt-4o")
    app = _Host(profiles)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        await pilot.press("escape")
        await until(pilot, lambda: app.result != "unset")

    assert app.result is None


def test_no_vendor_appears_anywhere_in_the_screen_source() -> None:
    source = Path("src/korvid/ui/widgets/profile_manager_screen.py").read_text(encoding="utf-8")
    for vendor in ("openai", "anthropic", "azure", "bedrock", "gemini", "ollama", "copilot"):
        assert vendor not in source.lower()
