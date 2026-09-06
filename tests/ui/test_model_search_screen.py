"""Tests for the model search screen (Task 10).

Search-first: the first focused widget is the query Input, not a provider
list. A provider name is a label and a search term, never a gate.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from korvid.agent.model_profiles import (
    EndpointRequirement,
    MetadataRefresh,
    ModelCatalog,
    ModelEntry,
    ModelEntrySource,
)
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

    def __init__(
        self,
        *,
        refresh_outcome: MetadataRefresh = MetadataRefresh.UPDATED,
        refresh_gate: asyncio.Event | None = None,
    ) -> None:
        #: How many searches the screen has asked for. A refresh that
        #: invalidated nothing must not silently re-run one.
        self.search_calls = 0
        #: How many times the screen asked for an explicit metadata refresh.
        self.refresh_calls = 0
        self._refresh_outcome = refresh_outcome
        #: When set, `refresh_metadata` blocks until the test releases it,
        #: which is how an in-flight second press is observed at all.
        self._refresh_gate = refresh_gate
        #: Display names the entries take on after a successful refresh.
        self._refreshed_names: dict[str, str] = {}

    async def _boom(self) -> None:  # pragma: no cover
        raise AssertionError("network must not be called during search")

    def _visible(self) -> tuple[ModelEntry, ...]:
        if not self._refreshed_names:
            return self._ENTRIES
        return tuple(
            e
            if e.reference not in self._refreshed_names
            else _entry(e.reference, self._refreshed_names[e.reference])
            for e in self._ENTRIES
        )

    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        self.search_calls += 1
        q = query.strip().lower()
        if not q:
            return ()
        matched = [e for e in self._visible() if q in e.reference.lower()]
        return tuple(matched[:limit])

    def entry(self, reference: str) -> ModelEntry | None:
        return next((e for e in self._visible() if e.reference == reference), None)

    def auth_methods(self, reference: str, *, endpoint: str | None = None) -> tuple[()]:
        return ()

    def option_fields(self, reference: str) -> tuple[()]:
        return ()

    def endpoint_requirement(self, reference: str) -> EndpointRequirement:
        return EndpointRequirement.OPTIONAL

    async def discover(self, profile: object) -> tuple[ModelEntry, ...]:
        raise AssertionError("network must not be called during search")

    async def test(self, profile: object) -> str:
        raise AssertionError("network must not be called during search")

    async def begin_auth(self, profile: object) -> None:
        raise AssertionError("network must not be called during search")

    async def finish_auth(self, profile: object) -> str | None:
        raise AssertionError("network must not be called during search")

    async def refresh_metadata(self) -> MetadataRefresh:
        self.refresh_calls += 1
        if self._refresh_gate is not None:
            await self._refresh_gate.wait()
        if self._refresh_outcome is MetadataRefresh.UPDATED:
            self._refreshed_names["openai/gpt-4o"] = "GPT-4o (refreshed)"
        return self._refresh_outcome


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
        shown = screen._shown_entries
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


async def test_selecting_a_manual_option_still_validates_the_reference() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        query = screen.query_one("#model-query", Input)
        query.value = "company/bad model"
        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="invalid manual option shown",
        )
        results = screen.query_one("#model-results", OptionList)
        results.highlighted = results.option_count - 1
        results.focus()
        await pilot.press("enter")

        status = screen.query_one("#search-status", Static)
        await until(
            pilot,
            lambda: "whitespace" in str(status.render()),
            label="manual option validation",
        )

    assert app.result == "unset"


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
        shown = screen._shown_entries
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


# ---------------------------------------------------------------------------
# The explicit metadata refresh (Task 7's contract: never at startup)
# ---------------------------------------------------------------------------


def _status_text(screen: ModelSearchScreen) -> str:
    return str(screen.query_one("#search-status", Static).render())


async def test_opening_the_screen_refreshes_nothing() -> None:
    """ "Never fetched at startup" includes mounting the screen.

    The docs claimed opening this screen triggered the fetch. It must not:
    an air-gapped operator opening model search would sit through a
    ten-second timeout for metadata they never asked to revalidate.
    """
    catalog = _FakeCatalog()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        await until(pilot, lambda: isinstance(screen.focused, Input), label="mounted")

        for ch in "gpt":
            await pilot.press(ch)
        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="results",
        )

    assert catalog.refresh_calls == 0


async def test_prefilled_editing_still_refreshes_nothing() -> None:
    """The prefilled search on mount is a search, not a revalidation."""
    catalog = _FakeCatalog()
    app = _Host(catalog, initial_query="openai/gpt-4o")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="prefilled results",
        )

    assert catalog.refresh_calls == 0


async def test_the_refresh_key_is_bound_and_visible() -> None:
    """An action nothing announces is an action nobody finds."""
    keys = {
        binding.key: binding
        for binding in ModelSearchScreen.BINDINGS
        if not isinstance(binding, tuple)
    }
    assert "ctrl+r" in keys
    assert keys["ctrl+r"].action == "refresh_metadata"
    assert keys["ctrl+r"].show is True

    app = _Host()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        await until(pilot, lambda: "Ctrl-R" in _status_text(screen), label="hint rendered")


async def test_the_refresh_key_runs_the_refresh_exactly_once() -> None:
    catalog = _FakeCatalog(refresh_outcome=MetadataRefresh.CACHED)
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        await pilot.press("ctrl+r")
        await until(pilot, lambda: catalog.refresh_calls == 1, label="refresh started")
        await until(pilot, lambda: "cache" in _status_text(screen).lower(), label="outcome shown")

    assert catalog.refresh_calls == 1


async def test_a_second_press_while_one_is_in_flight_starts_nothing() -> None:
    """The action is a bounded network call. Holding the key must not queue
    a fetch per keypress, and an exclusive worker that cancels its
    predecessor would abandon a refresh that was nearly done."""
    gate = asyncio.Event()
    catalog = _FakeCatalog(refresh_gate=gate)
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        await pilot.press("ctrl+r")
        await until(pilot, lambda: catalog.refresh_calls == 1, label="first refresh in flight")
        await pilot.press("ctrl+r")
        await pilot.press("ctrl+r")
        await until(
            pilot,
            lambda: "already" in _status_text(screen).lower(),
            label="in-flight notice",
        )
        assert catalog.refresh_calls == 1

        gate.set()
        await until(pilot, lambda: "updated" in _status_text(screen).lower(), label="finished")

        # The guard releases once the refresh completes.
        await pilot.press("ctrl+r")
        await until(pilot, lambda: catalog.refresh_calls == 2, label="second refresh allowed")


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (MetadataRefresh.UPDATED, "updated"),
        (MetadataRefresh.UNCHANGED, "already up to date"),
        (MetadataRefresh.CACHED, "cache"),
        (MetadataRefresh.UNAVAILABLE, "unavailable"),
        (MetadataRefresh.DISABLED, "disabled"),
    ],
)
async def test_every_outcome_is_reported_to_the_operator(
    outcome: MetadataRefresh, expected: str
) -> None:
    """Silence after a keypress reads as a broken key."""
    catalog = _FakeCatalog(refresh_outcome=outcome)
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        await pilot.press("ctrl+r")
        await until(
            pilot,
            lambda: expected in _status_text(screen).lower(),
            label=f"{outcome.value} reported",
        )

    assert catalog.refresh_calls == 1


async def test_refreshed_metadata_appears_in_the_current_query() -> None:
    """Refreshing while a query is on screen must re-render it.

    Otherwise the operator refreshes, is told it worked, and sees the rows
    they already had until they retype the query.
    """
    catalog = _FakeCatalog(refresh_outcome=MetadataRefresh.UPDATED)
    app = _Host(catalog, initial_query="openai/gpt-4o")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        results = screen.query_one("#model-results", OptionList)
        await until(pilot, lambda: results.option_count > 0, label="initial results")
        assert "refreshed" not in str(results.get_option_at_index(0).prompt).lower()

        await pilot.press("ctrl+r")
        await until(
            pilot,
            lambda: "refreshed" in str(results.get_option_at_index(0).prompt).lower(),
            label="rerendered with refreshed metadata",
        )


async def test_a_disabled_source_leaves_the_results_alone() -> None:
    """Nothing changed, so nothing is re-rendered — and the query survives."""
    catalog = _FakeCatalog(refresh_outcome=MetadataRefresh.DISABLED)
    app = _Host(catalog, initial_query="openai/gpt-4o")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        results = screen.query_one("#model-results", OptionList)
        await until(pilot, lambda: results.option_count > 0, label="initial results")
        before = results.option_count

        await pilot.press("ctrl+r")
        await until(
            pilot,
            lambda: "disabled" in _status_text(screen).lower(),
            label="disabled reported",
        )

        assert results.option_count == before
        assert screen.query_one("#model-query", Input).value == "openai/gpt-4o"


async def test_a_cache_hit_re_renders_nothing() -> None:
    """`CACHED` means the TTL had not expired: no request, no new facts.

    The catalog only drops its memoised index on a real update, so
    re-ranking here would re-render byte-identical rows and overwrite the
    operator's search summary with a sentence about a cache.
    """
    catalog = _FakeCatalog(refresh_outcome=MetadataRefresh.CACHED)
    app = _Host(catalog, initial_query="openai/gpt-4o")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        results = screen.query_one("#model-results", OptionList)
        await until(pilot, lambda: results.option_count > 0, label="initial results")
        searches_before = catalog.search_calls

        await pilot.press("ctrl+r")
        await until(pilot, lambda: "cache" in _status_text(screen).lower(), label="outcome shown")

        assert catalog.search_calls == searches_before
        assert _status_text(screen).startswith("Model metadata served from cache")


async def test_a_refresh_landing_after_unmount_touches_no_widget() -> None:
    """The completion path must check the screen is still there.

    The screen-owned worker is cancelled on dismissal, but cancellation
    only lands at an `await`: a refresh whose await returns in the same
    tick the screen goes away runs on to the status line regardless, and
    `query_one` on a screen whose widgets are gone raises. Driving the
    coroutine directly is the only way to pin that window deterministically
    — a scheduling race cannot be asserted on.
    """
    catalog = _FakeCatalog(refresh_outcome=MetadataRefresh.UPDATED)
    app = _Host(catalog, initial_query="openai/gpt-4o")
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        await until(
            pilot,
            lambda: screen.query_one("#model-results", OptionList).option_count > 0,
            label="initial results",
        )

        screen.dismiss(None)
        await until(pilot, lambda: app.result is None, label="dismissed")
        await until(pilot, lambda: not screen.is_attached, label="unmounted")

        await screen._refresh_metadata()

        assert catalog.refresh_calls == 1


async def test_a_refresh_that_raises_is_reported_not_crashed() -> None:
    """A worker exception would tear the screen down mid-setup."""

    class _Exploding(_FakeCatalog):
        async def refresh_metadata(self) -> MetadataRefresh:
            self.refresh_calls += 1
            raise RuntimeError("metadata source blew up")

    catalog = _Exploding()
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None

        await pilot.press("ctrl+r")
        await until(
            pilot,
            lambda: "unavailable" in _status_text(screen).lower(),
            label="failure reported",
        )
        assert app.result == "unset"


async def test_cancelling_while_a_refresh_is_in_flight_does_not_crash() -> None:
    """Esc during a refresh is ordinary operator behaviour.

    The worker outlives the screen's widgets for as long as the network
    call runs. If the completion path then wrote to a status line that no
    longer exists, the reward for pressing Esc would be a crash dialog on
    top of the wizard.
    """
    gate = asyncio.Event()
    catalog = _FakeCatalog(refresh_gate=gate)
    app = _Host(catalog)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.screen_ref is not None)
        screen = app.screen_ref
        assert screen is not None
        screen.query_one("#model-query", Input).value = "gpt"
        await pilot.pause()

        await pilot.press("ctrl+r")
        await until(pilot, lambda: catalog.refresh_calls == 1, label="refresh started")

        await pilot.press("escape")
        await until(pilot, lambda: app.result is None, label="screen dismissed")

        gate.set()
        await pilot.pause()
        await pilot.pause()

        assert app.result is None
