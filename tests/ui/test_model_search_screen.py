"""Tests for the model search screen (Task 10).

Search-first: the first focused widget is the query Input, not a provider
list. A provider name is a label and a search term, never a gate.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from korvid.agent.model_profiles import ModelCatalog, ModelEntry, ModelEntrySource
from korvid.ui.widgets.model_search_screen import ModelSearchScreen

from .waits import until

# ---------------------------------------------------------------------------
# Fake catalog
# ---------------------------------------------------------------------------


def _entry(reference: str, display: str | None = None) -> ModelEntry:
    provider, _ = reference.split("/", 1)
    return ModelEntry(
        reference=reference,
        provider_id=provider,
        display_name=display,
        source=ModelEntrySource.LITELLM,
    )


class _FakeCatalog(ModelCatalog):
    """In-memory catalog: search returns entries whose reference contains query."""

    _ENTRIES: tuple[ModelEntry, ...] = (
        _entry("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5"),
        _entry("anthropic/claude-opus-4", "Claude Opus 4"),
        _entry("openai/gpt-4o", "GPT-4o"),
        _entry("openai/gpt-4o-mini", "GPT-4o mini"),
        _entry("openai/o3", "OpenAI o3"),
        _entry("google/gemini-1.5-pro", "Gemini 1.5 Pro"),
        _entry("ollama/qwen3:8b", "Qwen3 8B"),
        _entry("ollama/llama3", "Llama 3"),
        # Extra entries to test the 50-row cap
        *[_entry(f"manyco/model-{i}") for i in range(60)],
    )

    async def _boom(self) -> None:  # pragma: no cover
        raise AssertionError("network must not be called during search")

    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        q = query.strip().lower()
        if not q:
            return ()
        matched = [e for e in self._ENTRIES if q in e.reference.lower()]
        return tuple(matched[:limit])

    def entry(self, reference: str) -> ModelEntry | None:
        return next((e for e in self._ENTRIES if e.reference == reference), None)

    def auth_methods(self, reference: str, *, endpoint: str | None = None) -> tuple[()]:
        return ()

    def option_fields(self, reference: str) -> tuple[()]:
        return ()

    def endpoint_requirement(self, reference: str) -> object:
        from korvid.agent.model_profiles import EndpointRequirement

        return EndpointRequirement.OPTIONAL

    async def discover(self, profile: object) -> tuple[ModelEntry, ...]:
        raise AssertionError("network must not be called during search")

    async def test(self, profile: object) -> str:
        raise AssertionError("network must not be called during search")

    async def begin_auth(self, profile: object) -> None:
        raise AssertionError("network must not be called during search")

    async def finish_auth(self, profile: object) -> str | None:
        raise AssertionError("network must not be called during search")


# ---------------------------------------------------------------------------
# Host app
# ---------------------------------------------------------------------------


class _Host(App[str | None]):
    def __init__(
        self,
        catalog: ModelCatalog | None = None,
        initial_query: str = "",
        discovered: tuple[ModelEntry, ...] = (),
    ) -> None:
        super().__init__()
        self._catalog = catalog or _FakeCatalog()
        self._initial_query = initial_query
        self._discovered = discovered
        self.result: str | object | None = "unset"
        self.screen_ref: ModelSearchScreen | None = None

    def on_mount(self) -> None:
        screen = ModelSearchScreen(
            catalog=self._catalog,
            initial_query=self._initial_query,
            discovered=self._discovered,
        )
        self.screen_ref = screen

        def _done(res: str | None) -> None:
            self.result = res

        self.push_screen(screen, callback=_done)

    def compose(self) -> ComposeResult:
        yield Static("")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_filters_and_selecting_returns_a_reference() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        await pilot.press("s", "o", "n", "n", "e", "t")
        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="results populated",
        )
        results = screen.query_one("#model-results", OptionList)
        results.highlighted = 0
        results.focus()
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="dismiss")

    assert app.result == "anthropic/claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_the_screen_opens_on_search_not_on_a_provider_list() -> None:
    """The first focused widget is the query box. There is no provider
    OptionList to focus, because there is no provider step."""
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        await until(
            pilot,
            lambda: isinstance(screen.focused, Input),
            label="query input focused",
        )

        assert isinstance(screen.focused, Input)
        assert list(screen.query("#provider-list")) == []


@pytest.mark.asyncio
async def test_results_group_by_provider_for_reading_but_do_not_filter_by_it() -> None:
    """A provider name is a label and a search term. It is never a gate:
    a query matching models across providers shows all of them."""
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        # "o" matches openai/* and ollama/* and google (no), also anthropic (no), manyco (no)
        await pilot.press("o")
        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="results for 'o'",
        )
        # introspect stored entries
        shown = screen._shown_entries  # type: ignore[attr-defined]
        providers = {entry.provider_id for entry in shown}
        assert len(providers) > 1


@pytest.mark.asyncio
async def test_an_unmatched_query_still_offers_the_typed_reference() -> None:
    """Manual entry is a first-class path, not an error state - a private
    or brand-new model is exactly what the catalog will not know."""
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        for ch in "company/internal-v2":
            await pilot.press(ch)

        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="manual option present",
        )
        results = screen.query_one("#model-results", OptionList)
        # get text of the last option
        last_opt = results.get_option_at_index(results.option_count - 1)
        rendered = str(last_opt.prompt)
        assert 'use "company/internal-v2"' in rendered.lower()

        # select it
        results.highlighted = results.option_count - 1
        results.focus()
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="dismiss")

    assert app.result == "company/internal-v2"


@pytest.mark.asyncio
async def test_a_manual_reference_without_a_slash_is_refused_with_the_reason() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        q_input = screen.query_one("#model-query", Input)
        q_input.value = "nodomain"
        q_input.focus()
        await pilot.press("enter")

        status = screen.query_one("#search-status", Static)
        await until(
            pilot,
            lambda: "provider/model" in str(status.render()),
            label="manual reference validation",
        )
        assert "provider/model" in str(status.render())


@pytest.mark.asyncio
async def test_search_is_bounded_so_a_broad_query_cannot_stall_the_ui() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        # "model" matches all manyco/model-* (60 entries), should be capped at 50
        for ch in "model":
            await pilot.press(ch)

        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="results for 'model'",
        )
        shown = screen._shown_entries  # type: ignore[attr-defined]
        assert len(shown) <= 50


@pytest.mark.asyncio
async def test_editing_prefills_the_current_model() -> None:
    app = _Host(initial_query="ollama/qwen3:8b")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        await until(
            pilot,
            lambda: screen.query_one("#model-query", Input).value == "ollama/qwen3:8b",
            label="current model prefilled",
        )

        assert screen.query_one("#model-query", Input).value == "ollama/qwen3:8b"


@pytest.mark.asyncio
async def test_a_colon_in_a_model_tag_survives_search_and_selection() -> None:
    """`ollama/qwen3:8b` is the shape colon separators could not express;
    it must round-trip through the UI unchanged."""
    app = _Host(initial_query="ollama/qwen3:8b")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="results for qwen3:8b",
        )
        results = screen.query_one("#model-results", OptionList)
        results.highlighted = 0
        results.focus()
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="dismiss")

    assert app.result == "ollama/qwen3:8b"


@pytest.mark.asyncio
async def test_the_screen_never_calls_the_network() -> None:
    """Search reads in-memory tables. A catalog whose `discover` fails
    the test if awaited proves the screen does not reach for it."""
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        for ch in "sonnet":
            await pilot.press(ch)

        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="results without network",
        )
        # If we reach here the screen never called discover/test/begin_auth/finish_auth
        assert screen.query_one("#model-results", OptionList).option_count > 0
