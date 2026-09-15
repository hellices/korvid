"""The Action Palette's domain model: derivation and ranking (issue #388).

The palette is a search surface over the two catalogs the app already
executes from — `APP_BINDINGS` (declarative `Binding`s) and the typed
`COMMANDS` (`:` command descriptors) — never a third, hand-maintained
table. `derive_action_entries`/`derive_command_entries` build immutable
`PaletteEntry` values straight from those catalogs, so a new binding or
command is automatically searchable, and `rank_entries` orders them for a
query without ever touching Textual or an app instance: no modal, no
dispatch, no wiring here (that's a later task).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from textual.binding import BindingType
from textual.fuzzy import Matcher

from korvid.ui.action_availability import ActionAvailability
from korvid.ui.app_bindings import as_binding, base_action, help_groups_for_action
from korvid.ui.command import CommandDescriptor, PaletteCommand
from korvid.ui.widgets.help_screen import key_label


@dataclass(frozen=True, slots=True)
class AppActionInvocation:
    """Invoke a bound app action by name (as `check_action`/`run_action` do)."""

    action: str


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Invoke a `:` command by its canonical (bare, colon-free) text."""

    canonical_text: str


PaletteInvocation = AppActionInvocation | CommandInvocation


@dataclass(frozen=True, slots=True)
class PaletteEntry:
    """One immutable, searchable palette row derived from a real catalog."""

    id: str
    title: str
    description: str
    category: str
    trigger: str
    search_terms: tuple[str, ...]
    declaration_order: int
    availability: ActionAvailability
    invocation: PaletteInvocation


def _humanize_action_id(action: str) -> str:
    """Turn a snake_case action id into a short palette title.

    Deterministic and catalog-derived, not a new metadata table: e.g.
    ``drain_node`` -> ``Drain node``. Only the first letter is capitalized
    so multi-word ids read as a normal sentence fragment rather than Title
    Case.
    """
    spaced = action.replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:] if spaced else spaced


def derive_action_entries(
    bindings: Sequence[BindingType],
    *,
    overrides: Mapping[str, str] | None = None,
    availability: Callable[[str], ActionAvailability],
) -> list[PaletteEntry]:
    """Derive one `PaletteEntry` per bound app action from `APP_BINDINGS`.

    Bindings that share an action (e.g. `shift+l`/``L`` both running
    ``logs_multi``, or a real key with its ``--alt`` id) collapse into a
    single entry keyed on the first binding encountered; `overrides` (the
    `keybindings:` config remap, issue #35) takes precedence over each
    binding's default key so the trigger shown matches what actually runs.
    Parameterized action expressions (e.g. ``favorite_namespace(3)``) have
    no single key to invoke generically from the palette, so they are
    skipped entirely rather than surfacing a broken entry.

    Title and description are both catalog-derived, not a new hand-written
    table: the title is the humanized action id (``drain_node`` ->
    ``Drain node``), and the description is the `Binding`'s own
    `description` text — which is sometimes a fuller explanation (e.g.
    `resize_pod`'s ``"Resize pod CPU/memory in place (K8s 1.35+)"``) that
    reads better as a palette second line than as a footer label.

    The canonical action id is the entry's search term, so the name the
    user reads in the docs, in an audit line or in a `run_action` call
    (``delete_resource``, ``logs_multi``) finds its own row exactly - the
    humanized title alone does not contain the underscore.

    An action classified under more than one help group by
    `ACTION_HELP_GROUPS` (e.g. `open_filter` in both `"Table"` and
    `"Logs"`) surfaces under only its *first* listed group as the entry's
    one palette `category` — it does not get a second, duplicate entry per
    group.
    """
    remapped = overrides or {}
    entries: list[PaletteEntry] = []
    seen_actions: set[str] = set()
    for order, raw in enumerate(bindings):
        binding = as_binding(raw)
        if "(" in binding.action:
            continue
        action = base_action(binding.action)
        if action in seen_actions:
            continue
        seen_actions.add(action)
        key = remapped.get(action, binding.key)
        entries.append(
            PaletteEntry(
                id=f"action:{action}",
                title=_humanize_action_id(action),
                description=binding.description,
                category=help_groups_for_action(action)[0],
                trigger=key_label(key),
                search_terms=(action,),
                declaration_order=order,
                availability=availability(action),
                invocation=AppActionInvocation(action),
            )
        )
    return entries


def _command_search_terms(
    descriptor: CommandDescriptor, palette: PaletteCommand
) -> tuple[str, ...]:
    """Every spelling one command row answers to, deterministically ordered.

    A command is reached by typing it, so each way of typing it has to be a
    search term: the canonical text first, then every alias the parser
    itself accepts (`problems` for `:pulse`, `agent` for `:ai`), each in
    both the bare and the colon form the user may reach for. The palette's
    own synonyms (`PaletteCommand.aliases`) come last and stay bare - they
    are prose (`port forward`), not command spellings.

    Deduplicated with `dict.fromkeys`, so the order is the declaration
    order above and the first occurrence wins: `pulse` is both the
    canonical text and the descriptor's first alias, and appears once.
    """
    spellings: list[str] = []
    for text in (palette.canonical_text, *descriptor.aliases):
        spellings.extend((text, f":{text}"))
    return tuple(dict.fromkeys([*spellings, *palette.aliases]))


def derive_command_entries(
    commands: Sequence[CommandDescriptor],
    *,
    availability: Callable[[str], ActionAvailability],
) -> list[PaletteEntry]:
    """Derive one `PaletteEntry` per `CommandDescriptor.palette` entry.

    Descriptors that opt out via `palette_omit_reason` (e.g. ``:q`` — the
    bound Quit action is the single palette entry) contribute nothing.

    The title is `PaletteCommand.title`; the description is the
    descriptor's own first `help` row description — the same text the
    `:help` overlay already shows for that command — rather than a new,
    separately maintained metadata table. The search terms come from the
    same two places (see `_command_search_terms`), so a command is found by
    every spelling that would parse, not only by its prose title.
    """
    entries: list[PaletteEntry] = []
    for order, descriptor in enumerate(commands):
        palette = descriptor.palette
        if palette is None:
            continue
        description = descriptor.help[0][1] if descriptor.help else ""
        entries.append(
            PaletteEntry(
                id=f"command:{palette.canonical_text}",
                title=palette.title,
                description=description,
                category="Commands",
                trigger=f":{palette.canonical_text}",
                search_terms=_command_search_terms(descriptor, palette),
                declaration_order=order,
                availability=availability(palette.canonical_text),
                invocation=CommandInvocation(palette.canonical_text),
            )
        )
    return entries


def derive_palette_entries(
    bindings: Sequence[BindingType],
    commands: Sequence[CommandDescriptor],
    *,
    overrides: Mapping[str, str] | None = None,
    availability: Callable[[str], ActionAvailability],
    command_availability: Callable[[str], ActionAvailability],
) -> list[PaletteEntry]:
    """Derive the whole palette catalog: bound actions, then `:` commands.

    One list, built fresh by its caller for every render and re-checked
    after the modal is dismissed - never cached: availability is a live
    question (the selected row, the current view, an in-flight write) whose
    answer expires the moment the user does anything.

    Two callables, not one: `availability` is asked about *action names*
    and `command_availability` about *canonical command texts*. They are
    separate vocabularies that happen to be strings, so a command spelled
    like an action (`:help`) must not inherit that action's answer, in
    either direction.
    """
    return [
        *derive_action_entries(bindings, overrides=overrides, availability=availability),
        *derive_command_entries(commands, availability=command_availability),
    ]


def _leading_tokens(text: str) -> list[str]:
    return text.casefold().split()


def _match_tier(
    entry: PaletteEntry, lower_query: str, matcher: Matcher
) -> tuple[int, float] | None:
    """Best (tier, score) for `entry` against `lower_query`, or `None`."""
    primary_texts = [entry.title, *entry.search_terms]
    if any(text.casefold() == lower_query for text in primary_texts):
        return (0, 1.0)
    if any(
        tokens and tokens[0].startswith(lower_query)
        for tokens in (_leading_tokens(text) for text in primary_texts)
    ):
        return (1, max(matcher.match(text) for text in primary_texts))
    best_primary = max((matcher.match(text) for text in primary_texts), default=0.0)
    if best_primary > 0:
        return (2, best_primary)
    if entry.description:
        description_score = matcher.match(entry.description)
        if description_score > 0:
            return (3, description_score)
    return None


def _empty_query_rank(entry: PaletteEntry) -> tuple[int, int, str]:
    if entry.availability.invocable:
        group = 1 if isinstance(entry.invocation, CommandInvocation) else 0
    else:
        group = 2
    return (group, entry.declaration_order, entry.id)


def rank_entries(entries: Sequence[PaletteEntry], query: str) -> list[PaletteEntry]:
    """Rank `entries` for `query`, most relevant first.

    An empty query orders invocable actions, then invocable commands, then
    every unavailable entry (each group by declaration order, so the
    palette's default view mirrors the catalogs it was derived from). A
    non-empty query ranks by match tier first — an exact title or search
    term, then a leading-token prefix, then a fuzzy title/search-term
    match, then a fuzzy description match — breaking ties by score,
    invocable-first, then declaration order and id for a fully
    deterministic, stable order. An exact match on a currently-unavailable
    entry still ranks by its tier: availability only breaks ties, so its
    reason stays visible rather than hiding the entry.
    """
    stripped = query.strip()
    if not stripped:
        return sorted(entries, key=_empty_query_rank)

    lower_query = stripped.casefold()
    matcher = Matcher(stripped)
    ranked: list[tuple[tuple[int, float, int, int, str], PaletteEntry]] = []
    for entry in entries:
        tier_score = _match_tier(entry, lower_query, matcher)
        if tier_score is None:
            continue
        tier, score = tier_score
        invocable_rank = 0 if entry.availability.invocable else 1
        ranked.append(
            (
                (tier, -score, invocable_rank, entry.declaration_order, entry.id),
                entry,
            )
        )
    ranked.sort(key=lambda pair: pair[0])
    return [entry for _, entry in ranked]
