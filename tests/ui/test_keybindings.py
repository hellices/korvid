"""Keybinding remapping UI tests (issue #35): the `keybindings:` config
section actually rebinds keys at startup, warns on bad entries, and the
help overlay shows the effective keys."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from textual.binding import Binding

from korvid.core.config import KorvidConfig
from korvid.core.keybindings import APPROVAL_KEYS, plan_keybindings
from korvid.core.session_timeline import SessionTimeline
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen

from .test_app import _pod, make_app
from .waits import until


def _config(keybindings: dict[str, str]) -> KorvidConfig:
    return KorvidConfig(namespace="default", keybindings=keybindings)


async def test_remapped_key_triggers_action_and_default_is_freed() -> None:
    app = make_app([_pod("web")], config=_config({"help": "f1"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("question_mark")  # freed default must be inert now
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        await pilot.press("f1")
        await until(
            pilot,
            lambda: isinstance(app.screen, HelpScreen),
            label="help opens on f1",
        )


async def test_unknown_action_warns_at_startup_instead_of_crashing() -> None:
    app = make_app([_pod("web")], config=_config({"warp_drive": "w"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("warp_drive" in n.message for n in app._notifications),
            label="unknown-action warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_approval_dialog_actions_cannot_be_remapped() -> None:
    # Safety invariant: approval dialogs are confirmed only by their fixed
    # keystrokes — config must never rebind them.
    app = make_app([_pod("web")], config=_config({"confirm": "enter"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("approval" in n.message for n in app._notifications),
            label="protected-action warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_help_overlay_shows_remapped_key() -> None:
    app = make_app([_pod("web")], config=_config({"logs": "ctrl+g"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("question_mark")
        await until(
            pilot,
            lambda: isinstance(app.screen, HelpScreen),
            label="help overlay open",
        )
        body = app.screen.body_text() if isinstance(app.screen, HelpScreen) else ""
        assert "Ctrl-G" in body
        # The old default key row for logs is gone from the overlay.
        assert "  l          Logs" not in body


async def test_uppercase_alt_binding_follows_the_remap() -> None:
    # sort_by_age is bound to both shift+a and the terminal-delivered "A";
    # remapping the action must retire both spellings. "z" (not "g", now
    # the operational relationship graph's default key, issue #281) is an
    # arbitrary free key with no default binding of its own.
    pods = [_pod("bb"), _pod("aa")]
    app = make_app(pods, config=_config({"sort_by_age": "z"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")

        def _sorted_by_age() -> bool:
            return any(
                "AGE" in str(c.label) and "▼" in str(c.label) for c in table.columns.values()
            )

        await pilot.press("A")
        await pilot.pause()
        assert not _sorted_by_age()
        await pilot.press("z")
        await until(pilot, lambda: _sorted_by_age(), label="z sorts by age")


async def test_timeline_binding_can_be_remapped() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    app = make_app([_pod("web")], config=_config({"timeline": "ctrl+g"}), session_timeline=timeline)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("T")  # freed default must be inert now
        await pilot.pause()
        assert not isinstance(app.screen, SessionTimelineScreen)
        await pilot.press("ctrl+g")
        await until(
            pilot,
            lambda: isinstance(app.screen, SessionTimelineScreen),
            label="timeline opens on remap",
        )


def test_keybindings_doc_directs_to_help_overlay_not_static_inventory() -> None:
    # The approved design contract: docs/keybindings.md must direct users to
    # the dynamic `?` help overlay for the complete effective set (including
    # remaps), rather than embedding a static exhaustive action-name inventory.
    # A hidden collapsible block ("??? note") is still a hidden inventory and
    # must not exist.
    doc = Path(__file__).parents[2].joinpath("docs", "keybindings.md").read_text()

    # The doc must actively point users to the in-app overlay.
    assert "Press `?` for the complete effective set" in doc, (
        "docs/keybindings.md must direct users to the `?` help overlay"
    )

    # Discovery contract: action names come from the app; an unrecognised name
    # is skipped at startup with a warning that lists every valid action name.
    assert "Action names come from the app itself" in doc, (
        "docs/keybindings.md must explain that action names come from the app"
    )
    assert "skipped at startup with a warning that lists every valid action name" in doc, (
        "docs/keybindings.md must explain that an unrecognised name is skipped with a warning"
    )

    # The hidden inventory block must be absent — not hidden behind a
    # collapsible block, not present in any form.
    assert '??? note "Every remappable action name"' not in doc, (
        "docs/keybindings.md must not contain a hidden inventory of action names"
    )
    assert "Every remappable action name" not in doc, (
        "docs/keybindings.md must not contain an action-name inventory title"
    )


def _app_bindings() -> list[Binding]:
    """`KorvidApp.BINDINGS`, normalised to `Binding` objects."""
    return [raw if isinstance(raw, Binding) else Binding(*raw) for raw in KorvidApp.BINDINGS]


def _default_keys(action: str) -> tuple[str, ...]:
    """Every key the shipped app binds to `action`, spelled as it binds it."""
    return tuple(binding.key for binding in _app_bindings() if binding.action == action)


def test_documented_remap_example_survives_the_real_keybinding_planner() -> None:
    """The `keybindings:` snippet in docs/keybindings.md must be a remap the
    shipped app actually accepts. `ctrl+x` (interrupt_agent) and `g`
    (relationships) are defaults of other actions, so an example using them
    is silently dropped with a startup warning."""
    doc = Path(__file__).parents[2].joinpath("docs", "keybindings.md").read_text()
    block = doc.split("```yaml", 1)[1].split("```", 1)[0]
    documented = dict(re.findall(r"^\s{2}([a-z_]+):\s*(\S+)", block, flags=re.MULTILINE))
    assert documented, "docs/keybindings.md must keep a worked remap example"

    bindings = _app_bindings()
    plan = plan_keybindings(
        dict(documented),
        KorvidApp._binding_actions(),
        {binding.action for binding in bindings if binding.priority},
        reserved_keys={binding.key: binding.action for binding in bindings if binding.id is None},
    )
    assert plan.warnings == ()
    assert plan.overrides == documented


async def test_documented_remap_example_rebinds_the_running_app() -> None:
    """End-to-end proof for the same snippet: the freed defaults go inert
    and the documented keys drive the documented actions.

    Round-13 review (comment 3862106877): the freed default used to be the
    literal `"A"`. If `sort_by_age`'s product default moved, `"A"` would
    become a key bound to nothing, `assert not _sorted_by_age()` would hold
    for the wrong reason and the "the default really is inert" half of this
    contract would quietly stop testing anything. Both halves are derived
    from the shipped `BINDINGS` now.
    """
    doc = Path(__file__).parents[2].joinpath("docs", "keybindings.md").read_text()
    block = doc.split("```yaml", 1)[1].split("```", 1)[0]
    documented = dict(re.findall(r"^\s{2}([a-z_]+):\s*(\S+)", block, flags=re.MULTILINE))
    defaults = _default_keys("sort_by_age")
    assert defaults, "sort_by_age must still ship a default binding for the remap to free"
    assert documented["sort_by_age"] not in defaults, (
        f"the documented remap {documented['sort_by_age']!r} is one of sort_by_age's own "
        f"defaults {defaults}; freeing it would prove nothing"
    )
    pods = [_pod("bb"), _pod("aa")]
    app = make_app(pods, config=_config(documented))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        assert app._keybinding_overrides == documented
        assert not any("keybindings:" in n.message for n in app._notifications)

        def _sorted_by_age() -> bool:
            return any(
                "AGE" in str(c.label) and "▼" in str(c.label) for c in table.columns.values()
            )

        for freed in defaults:
            await pilot.press(freed)  # every freed default must be inert now
            await pilot.pause()
            assert not _sorted_by_age(), f"the freed default {freed!r} still sorted by age"
        await pilot.press(documented["sort_by_age"])
        await until(pilot, _sorted_by_age, label="documented key sorts by age")


def test_favorite_namespace_keys_are_not_remappable() -> None:
    # The nine 1-9 favorite bindings carry no keymap id — the keymap cannot
    # move them, so offering them as remappable actions would be a lie.
    actions = KorvidApp._binding_actions()
    assert not any(action.startswith("favorite_namespace") for action in actions)


async def test_shifted_letter_remap_works_via_terminal_uppercase_spelling() -> None:
    # Real terminals deliver shift+g as "G" — the documented `shift+g`
    # syntax must still work there, not only under Pilot.
    pods = [_pod("bb"), _pod("aa")]
    app = make_app(pods, config=_config({"sort_by_age": "shift+g"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")

        def _sorted_by_age() -> bool:
            return any(
                "AGE" in str(c.label) and "▼" in str(c.label) for c in table.columns.values()
            )

        await pilot.press("G")  # the terminal spelling of shift+g
        await until(pilot, lambda: _sorted_by_age(), label="G sorts by age")


async def test_priority_action_cannot_take_an_approval_dialog_key() -> None:
    # toggle_agent is a priority binding (fires before any screen); giving
    # it "y" would steal the approval dialog's confirm keystroke.
    app = make_app([_pod("web")], config=_config({"toggle_agent": "y"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("approval" in n.message for n in app._notifications),
            label="priority/approval-key warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_help_overlay_applies_remap_to_helm_rows() -> None:
    """The Helm rows in the help overlay come from dedicated remappable
    bindings (issue #114): the overlay must advertise the effective keys,
    not the hardcoded defaults."""
    app = make_app([_pod("web")], config=_config({"helm_install": "x"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("question_mark")
        await until(
            pilot,
            lambda: isinstance(app.screen, HelpScreen),
            label="help overlay open",
        )
        body = app.screen.body_text() if isinstance(app.screen, HelpScreen) else ""
        assert "x          Install chart" in body
        assert "i          Install chart" not in body


async def test_favorite_digit_keys_are_reserved_against_overrides() -> None:
    # The 1-9 favorites are excluded from the remappable-action map, but
    # their keys must still be reserved: `logs: "1"` would otherwise be
    # accepted while the live favorite binding still owns the key.
    app = make_app([_pod("web")], config=_config({"logs": "1"}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("reserved" in n.message for n in app._notifications),
            label="reserved-key warning notified",
        )
        assert app._keybinding_overrides == {}


@pytest.mark.parametrize("key", sorted(APPROVAL_KEYS))
async def test_palette_binding_cannot_take_an_approval_dialog_key(key: str) -> None:
    """`open_action_palette` is a priority binding (it fires over any
    screen), so the planner must refuse every key the approval dialogs
    listen for - the palette may never become the confirm keystroke."""
    app = make_app([_pod("web")], config=_config({"open_action_palette": key}))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("approval" in n.message for n in app._notifications),
            label="priority/approval-key warning notified",
        )
        assert app._keybinding_overrides == {}


async def test_palette_binding_is_remappable() -> None:
    """Ctrl-P is only the *default*: the palette open is a normal, id-carrying
    binding the `keybindings:` section can move (issue #35)."""
    app = make_app([_pod("web")], config=_config({"open_action_palette": "ctrl+j"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        await pilot.press("ctrl+p")  # freed default must be inert now
        await pilot.pause()
        assert not isinstance(app.screen, ActionPaletteScreen)
        await pilot.press("ctrl+j")
        await until(
            pilot,
            lambda: isinstance(app.screen, ActionPaletteScreen),
            label="palette opens on ctrl+j",
        )


async def test_escape_closes_a_remapped_palette_and_the_open_key_cannot_stack_one() -> None:
    """Escape is the documented universal close, remap or not (issue #388).

    Korvid's modals all close on Escape and the palette says so on its own
    hint line, so a remapped open key does not need to become a second,
    dynamically-bound close key - and a screen binding that tracked the
    config would be one more priority key resolved against whatever the
    user chose. What the remapped key must not do is *stack*: it stays a
    palette open, and the surface guard refuses an open over an open modal,
    so pressing it again leaves exactly one palette on the stack.
    """
    app = make_app([_pod("web")], config=_config({"open_action_palette": "ctrl+j"}))
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        table.focus()
        await pilot.press("ctrl+j")
        await until(
            pilot,
            lambda: isinstance(app.screen, ActionPaletteScreen),
            label="palette opens on ctrl+j",
        )
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert len([s for s in app.screen_stack if isinstance(s, ActionPaletteScreen)]) == 1
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.screen is app.screen_stack[0],
            label="escape closes the remapped palette",
        )
        assert not any(isinstance(s, ActionPaletteScreen) for s in app.screen_stack)
        assert app.focused is table


def test_keybindings_doc_documents_the_action_palette_key() -> None:
    """The palette is only discoverable if the key is written down.

    The documented key is derived from the shipped binding, so a future
    default change fails here instead of leaving the page quietly wrong,
    and the two facts that make the surface honest have to be on the page:
    an action that does not apply stays searchable *with its reason*, and
    the key itself is remappable like any other.
    """
    defaults = _default_keys("open_action_palette")
    assert defaults == ("ctrl+p",), "update docs/keybindings.md with the new default key"
    doc = Path(__file__).parents[2].joinpath("docs", "keybindings.md").read_text()
    flat = " ".join(doc.split())
    assert "`Ctrl-P`" in doc, "docs/keybindings.md must document the Action Palette key"
    row = [line for line in doc.splitlines() if line.startswith("|") and "`Ctrl-P`" in line]
    assert len(row) == 1, "the Action Palette needs exactly one key-table row"
    assert re.search(r"search|palette", row[0], re.I), (
        "the Ctrl-P row must say the key searches actions"
    )
    assert re.search(r"unavailable|not apply|does not apply", flat, re.I), (
        "docs/keybindings.md must explain that unavailable actions stay searchable"
    )
    assert re.search(r"reason", flat, re.I), (
        "docs/keybindings.md must say an unavailable action shows why"
    )
    assert re.search(r"`Esc`[^.]*close", flat, re.I), (
        "docs/keybindings.md must document Esc as the palette's close key"
    )
    assert re.search(r"`open_action_palette`", flat), (
        "docs/keybindings.md must name the remappable action id for the palette"
    )


def test_keybindings_doc_describes_the_catalog_the_palette_actually_searches() -> None:
    """`Ctrl-P` searches the bound app actions and the built-in `:` commands
    that opt in - not the resource views (`:pods`, `:deploy`), which are
    parsed from the live alias table and never enumerated, and not `:q`,
    which opts out because the bound Quit action is already its single
    entry. Promising "every ... `:` command" sends a reader looking for
    rows the palette will never show, and implies a duplicate `:q`.
    """
    from korvid.ui.command import COMMANDS

    omitted = [descriptor.aliases for descriptor in COMMANDS if descriptor.palette is None]
    assert omitted == [("q", "quit")], "update docs/keybindings.md: the omissions changed"

    doc = Path(__file__).parents[2].joinpath("docs", "keybindings.md").read_text()
    paragraph = next(block for block in doc.split("\n\n") if "opens the Action Palette" in block)
    flat = " ".join(paragraph.split())
    assert "every app action and `:` command" not in flat, (
        "docs/keybindings.md must not promise every `:` command"
    )
    assert "built-in `:` commands" in flat, (
        "docs/keybindings.md must say the palette searches the built-in commands"
    )
    assert "`:pods`" in flat, "docs/keybindings.md must say resource views are not palette rows"
    assert re.search(r"`:q`.{0,80}Quit", flat), (
        "docs/keybindings.md must explain that `:q` is not a second Quit row"
    )
