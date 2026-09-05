"""Tests for the profile manager screen (Task 9)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


async def _no_edit(_profile: ModelConnectionConfig | None) -> ModelConnectionConfig | None:
    return None


class _Host(App["ProfileManagerResult | None"]):
    def __init__(
        self,
        profiles: ModelConnectionsConfig,
        open_editor: Callable[
            [ModelConnectionConfig | None], Awaitable[ModelConnectionConfig | None]
        ]
        | None = None,
        current_tier: str | None = None,
    ) -> None:
        super().__init__()
        self._profiles = profiles
        self._open_editor = open_editor or _no_edit
        self._current_tier = current_tier
        self.result: ProfileManagerResult | str | None = "unset"
        self.screen_ref: ProfileManagerScreen | None = None

    def on_mount(self) -> None:
        screen = ProfileManagerScreen(
            profiles=self._profiles,
            catalog=_FakeCatalog(),
            open_editor=self._open_editor,
            current_tier=self._current_tier,
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
        screen = app.screen_ref
        assert screen is not None
        await until(
            pilot,
            lambda: bool(str(screen.query_one("#profile-status", Static).render())),
            label="invalid profile status",
        )
        status_text = str(screen.query_one("#profile-status", Static).render())

    assert app.result == "unset"  # did not dismiss
    assert "cannot be activated" in status_text


async def test_add_returns_the_profile_from_the_editor() -> None:
    added = ModelConnectionConfig(model="openai/gpt-4o")

    async def open_editor(
        existing: ModelConnectionConfig | None,
    ) -> ModelConnectionConfig | None:
        assert existing is None
        return added

    app = _Host(ModelConnectionsConfig(), open_editor)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        await pilot.press("a")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert result.edited.profiles == {"gpt-4o": added}


async def test_edit_replaces_the_selected_profile_in_place() -> None:
    existing = ModelConnectionConfig(model="openai/gpt-4o-mini")
    edited = ModelConnectionConfig(model="openai/gpt-4o")

    async def open_editor(
        selected: ModelConnectionConfig | None,
    ) -> ModelConnectionConfig | None:
        assert selected is existing
        return edited

    app = _Host(ModelConnectionsConfig(profiles={"production": existing}), open_editor)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "production")
        await pilot.press("e")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert result.edited.profiles == {"production": edited}


async def test_a_repaired_profile_drops_the_raw_entry_it_replaces() -> None:
    """A rejected block lives in `unparsed`; the writer prefers it.

    So the repair only reaches the file if the manager also retires the
    raw entry. Leaving it behind would write the operator's answers to a
    profile set that then re-emits the broken text over them.
    """
    broken = ModelConnectionConfig(
        model="openai/gpt-4o",
        options={"api_key": "inline-secret-value"},
    )
    repaired = ModelConnectionConfig(model="openai/gpt-4o", options={"num_ctx": 8192})

    async def open_editor(
        selected: ModelConnectionConfig | None,
    ) -> ModelConnectionConfig | None:
        assert selected is broken
        return repaired

    profiles = ModelConnectionsConfig(
        profiles={"needs-fixing": broken},
        unparsed={
            "needs-fixing": {
                "model": "openai/gpt-4o",
                "options": {"api_key": "inline-secret-value"},
            },
            "untouched": {"no": "model"},
        },
    )
    app = _Host(profiles, open_editor)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "needs-fixing")
        await pilot.press("e")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert result.edited.profiles == {"needs-fixing": repaired}
    assert "needs-fixing" not in result.edited.unparsed
    # Another operator's unrepaired entry is not collateral damage.
    assert result.edited.unparsed["untouched"] == {"no": "model"}


async def test_editing_an_unparsed_only_entry_replaces_it_with_the_repair() -> None:
    """An entry korvid could not model at all is repaired the same way:
    the editor starts from nothing and the raw text retires."""
    repaired = ModelConnectionConfig(model="openai/gpt-4o")

    async def open_editor(
        selected: ModelConnectionConfig | None,
    ) -> ModelConnectionConfig | None:
        assert selected is None
        return repaired

    profiles = ModelConnectionsConfig(unparsed={"broken": {"no": "model"}})
    app = _Host(profiles, open_editor)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "broken")
        await pilot.press("e")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.edited is not None
    assert result.edited.profiles == {"broken": repaired}
    assert result.edited.unparsed == {}


async def test_a_cancelled_edit_keeps_the_raw_entry() -> None:
    """Cancelling repairs nothing, so it must retire nothing."""
    raw = {"model": "openai/gpt-4o", "options": {"api_key": "inline-secret-value"}}
    profiles = ModelConnectionsConfig(
        profiles={"needs-fixing": ModelConnectionConfig(model="openai/gpt-4o")},
        unparsed={"needs-fixing": raw},
    )
    app = _Host(profiles, _no_edit)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        _highlight(app, "needs-fixing")
        await pilot.press("e")
        await pilot.pause()

    assert app.result == "unset"
    assert profiles.unparsed["needs-fixing"] == raw


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


# ---------------------------------------------------------------------------
# The global capability tier
# ---------------------------------------------------------------------------


def _choose_tier(app: _Host, label: str) -> None:
    """Highlight the tier option whose prompt is `label`."""
    ol = app.screen.query_one("#tier-list", OptionList)
    for i in range(ol.option_count):
        if str(ol.get_option_at_index(i).prompt) == label:
            ol.highlighted = i
            return
    raise AssertionError(f"tier option {label!r} not found")


async def _pick_tier(app: _Host, label: str) -> None:
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        await pilot.press("t")
        await until(pilot, lambda: bool(app.screen.query("#tier-list")))
        _choose_tier(app, label)
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset")


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Low", "low"), ("High", "high")],
)
async def test_the_tier_choice_is_reachable_and_returns_the_override(
    label: str, expected: str
) -> None:
    """The global `agent.model_tier` has to be changeable somewhere.

    The wizard asks for it once, on a first run; after that the manager
    is the only screen an operator reaches, so the choice lives here.
    """
    app = _Host(_make_profiles(only="openai/gpt-4o"))
    await _pick_tier(app, label)

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.tier_changed is True
    assert result.model_tier == expected
    # A tier change is not a profile change.
    assert result.activated is None
    assert result.edited is None


async def test_choosing_automatic_is_a_change_and_not_a_missing_answer() -> None:
    """Automatic *is* `None`, so the result cannot encode the choice in
    the tier alone — a controller reading `model_tier is None` as "no
    answer" could never clear an override the operator wants gone."""
    app = _Host(_make_profiles(only="openai/gpt-4o"), current_tier="high")
    await _pick_tier(app, "Automatic")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.tier_changed is True
    assert result.model_tier is None
    assert result != ProfileManagerResult()


async def test_the_tier_chooser_opens_on_the_persisted_tier() -> None:
    """Injected, not guessed: opening on the wrong entry turns a glance
    at the chooser into a silent tier change."""
    app = _Host(_make_profiles(only="openai/gpt-4o"), current_tier="low")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        await pilot.press("t")
        await until(pilot, lambda: bool(app.screen.query("#tier-list")))
        tier_list = app.screen.query_one("#tier-list", OptionList)
        highlighted = tier_list.highlighted
        assert highlighted is not None
        assert str(tier_list.get_option_at_index(highlighted).prompt) == "Low"
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset")

    result = app.result
    assert isinstance(result, ProfileManagerResult)
    assert result.model_tier == "low"


async def test_escaping_the_tier_chooser_returns_to_the_profile_list() -> None:
    """Backing out of the tier is not backing out of the manager."""
    app = _Host(_make_profiles(only="openai/gpt-4o"))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        await pilot.press("t")
        await until(pilot, lambda: bool(app.screen.query("#tier-list")))
        # The heading says which question is on screen.
        assert "tier" in str(app.screen.query_one(".pm-title", Static).render()).lower()
        await pilot.press("escape")
        await until(pilot, lambda: app.screen.query_one("#profile-list", OptionList).display)
        assert app.result == "unset"
        assert app.screen.query_one("#tier-list", OptionList).display is False
        assert "profiles" in str(app.screen.query_one(".pm-title", Static).render()).lower()
        await pilot.press("escape")
        await until(pilot, lambda: app.result != "unset")

    assert app.result is None


async def test_a_profile_key_does_nothing_while_the_tier_chooser_is_open() -> None:
    """`d` is destructive. It must not reach the hidden profile list."""
    app = _Host(_make_profiles(only="openai/gpt-4o"))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None and app.screen_ref.is_attached)
        await pilot.press("t")
        await until(pilot, lambda: bool(app.screen.query("#tier-list")))
        await pilot.press("d")
        await pilot.pause()
        assert app.result == "unset"

    assert app.result == "unset"


def test_no_vendor_appears_anywhere_in_the_screen_source() -> None:
    source = Path("src/korvid/ui/widgets/profile_manager_screen.py").read_text(encoding="utf-8")
    for vendor in ("openai", "anthropic", "azure", "bedrock", "gemini", "ollama", "copilot"):
        assert vendor not in source.lower()
