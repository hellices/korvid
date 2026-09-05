"""Profile manager modal screen (Task 9).

Lets an operator list, activate, add, edit, and delete model profiles.
Activation is write-only on the active pointer and never re-serialises the
profile set, so switching profiles cannot silently drop an entry that failed
to parse.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from korvid.agent.model_profiles import ModelCatalog
from korvid.core.config import ModelConnectionConfig, ModelConnectionsConfig

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class ProfileManagerResult:
    """What the manager hands back. Exactly one field is set.

    `activated` names a profile to switch to. `edited` carries a whole
    replacement profile set to persist. Splitting them keeps "switch"
    from silently rewriting a profile the operator did not touch.
    """

    activated: str | None = None
    edited: ModelConnectionsConfig | None = None


async def _never(
    _profile: ModelConnectionConfig | None = None,
) -> ModelConnectionConfig | None:  # pragma: no cover
    return None


_ACTIVE_MARKER = " (active)"
_INVALID_MARKER = " (invalid)"


def _is_unparsed_name(name: str, profiles: ModelConnectionsConfig) -> bool:
    return name in profiles.unparsed and name not in profiles.profiles


def _row_label(
    name: str,
    profiles: ModelConnectionsConfig,
    catalog: ModelCatalog | None,
) -> str:
    """Build the display row for one profile entry."""
    is_active = profiles.active == name
    if name in profiles.unparsed and name not in profiles.profiles:
        # Pure unparsed entry — show the raw parse problem
        raw = profiles.unparsed[name]
        error = f"parse error: {raw!r}"[:60]
        marker = _ACTIVE_MARKER if is_active else _INVALID_MARKER
        return f"{name}{marker} — {error}"

    profile = profiles.profiles.get(name)
    if profile is None:
        return name  # should not happen

    marker = _ACTIVE_MARKER if is_active else ""
    if profile.config_error is not None:
        marker = _ACTIVE_MARKER if is_active else _INVALID_MARKER
        label = profile.config_error[:60]
    elif catalog is not None:
        entry = catalog.entry(profile.model)
        label = entry.display_name if (entry and entry.display_name) else profile.model
    else:
        label = profile.model
    return f"{name}{marker} — {label}"


def _ordered_names(profiles: ModelConnectionsConfig) -> list[str]:
    """Parsed profiles first (insertion order), then unparsed-only (insertion order)."""
    parsed_names = list(profiles.profiles)
    unparsed_only = [n for n in profiles.unparsed if n not in profiles.profiles]
    return parsed_names + unparsed_only


class ProfileManagerScreen(ModalScreen["ProfileManagerResult | None"]):
    """List, activate, add, edit and delete model profiles.

    Args:
        profiles: The current profile set, rendered in insertion order.
        catalog: Used only to label a profile's model; `None` renders the
            raw reference, which is still complete information.
        open_editor: Pushes the model-search/edit flow for a profile,
            returning the edited profile or None. Injected so this screen
            is testable without the whole wizard.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Close"),
        Binding("enter", "activate", "Activate"),
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
    ]

    DEFAULT_CSS = """
    ProfileManagerScreen {
        align: center middle;
    }
    ProfileManagerScreen > Vertical {
        width: 70;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    ProfileManagerScreen .pm-title {
        text-style: bold;
        padding-bottom: 1;
    }
    ProfileManagerScreen #profile-list {
        height: auto;
        max-height: 16;
    }
    ProfileManagerScreen #profile-status {
        color: $warning;
        padding-top: 1;
    }
    ProfileManagerScreen .pm-hint {
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        profiles: ModelConnectionsConfig,
        catalog: ModelCatalog | None = None,
        open_editor: Callable[
            [ModelConnectionConfig | None], Awaitable[ModelConnectionConfig | None]
        ]
        | None = None,
    ) -> None:
        super().__init__()
        self._profiles = profiles
        self._catalog = catalog
        self._open_editor: Callable[
            [ModelConnectionConfig | None], Awaitable[ModelConnectionConfig | None]
        ] = open_editor if open_editor is not None else _never

    def _build_options(self) -> list[Option]:
        return [
            Option(_row_label(name, self._profiles, self._catalog), id=f"pm-{name}")
            for name in _ordered_names(self._profiles)
        ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Model profiles", classes="pm-title")
            with VerticalScroll():
                yield OptionList(*self._build_options(), id="profile-list")
            yield Static("", id="profile-status")
            yield Static(
                "Enter=activate  a=add  e=edit  d=delete  Esc=close",
                classes="pm-hint",
            )

    def on_mount(self) -> None:
        try:
            ol = self.query_one("#profile-list", OptionList)
        except NoMatches:
            self.call_after_refresh(self._focus_list)
            return
        self._focus_list_widget(ol)

    def _focus_list(self) -> None:
        try:
            ol = self.query_one("#profile-list", OptionList)
        except NoMatches:
            return
        self._focus_list_widget(ol)

    def _focus_list_widget(self, ol: OptionList) -> None:
        if ol.option_count:
            ol.highlighted = 0
        ol.focus()

    def _selected_name(self) -> str | None:
        """Return the profile name for the currently highlighted option, or None."""
        try:
            ol = self.query_one("#profile-list", OptionList)
        except NoMatches:
            return None
        idx = ol.highlighted
        if idx is None or idx < 0 or idx >= ol.option_count:
            return None
        opt = ol.get_option_at_index(idx)
        opt_id = opt.id or ""
        if opt_id.startswith("pm-"):
            return opt_id[3:]
        return None

    def _set_status(self, msg: str) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#profile-status", Static).update(msg)

    def _is_invalid_name(self, name: str) -> bool:
        """True when the profile is unparsed or carries a config_error."""
        if name in self._profiles.unparsed and name not in self._profiles.profiles:
            return True
        profile = self._profiles.profiles.get(name)
        return profile is not None and profile.config_error is not None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter key on OptionList triggers activation."""
        event.stop()
        opt_id = event.option.id or ""
        name = opt_id[3:] if opt_id.startswith("pm-") else None
        if name is None:
            return
        self._activate_name(name)

    def action_activate(self) -> None:
        """Activate the highlighted profile, refusing invalid ones."""
        name = self._selected_name()
        if name is None:
            return
        self._activate_name(name)

    def _activate_name(self, name: str) -> None:
        if self._is_invalid_name(name):
            self._set_status(f"{name!r} cannot be activated: the profile is invalid")
            return
        self.dismiss(ProfileManagerResult(activated=name))

    def action_delete(self) -> None:
        """Remove the selected profile from both halves, clear active pointer if needed."""
        name = self._selected_name()
        if name is None:
            return
        new_profiles = {k: v for k, v in self._profiles.profiles.items() if k != name}
        new_unparsed = {k: v for k, v in self._profiles.unparsed.items() if k != name}
        new_active = None if self._profiles.active == name else self._profiles.active
        result_config = ModelConnectionsConfig(
            profiles=new_profiles,
            active=new_active,
            unparsed=new_unparsed,
        )
        self.dismiss(ProfileManagerResult(edited=result_config))

    def action_edit(self) -> None:
        """Open the editor for the selected profile."""
        name = self._selected_name()
        if name is None:
            return
        existing = self._profiles.profiles.get(name)
        self.run_worker(self._run_edit(name, existing), exclusive=True)

    async def _run_edit(self, name: str, existing: ModelConnectionConfig | None) -> None:
        result = await self._open_editor(existing)
        if result is None:
            return
        # Replace in-place preserving insertion order
        new_profiles: dict[str, ModelConnectionConfig] = {}
        replaced = False
        for k, v in self._profiles.profiles.items():
            if k == name:
                new_profiles[k] = result
                replaced = True
            else:
                new_profiles[k] = v
        if not replaced:
            new_profiles[name] = result
        new_config = ModelConnectionsConfig(
            profiles=new_profiles,
            active=self._profiles.active,
            unparsed=dict(self._profiles.unparsed),
        )
        self.dismiss(ProfileManagerResult(edited=new_config))

    def action_add(self) -> None:
        """Open the editor to create a new profile."""
        self.run_worker(self._run_add(), exclusive=True)

    async def _run_add(self) -> None:
        result = await self._open_editor(None)
        if result is None:
            return
        # Append with a generated name
        existing_names = set(self._profiles.profiles) | set(self._profiles.unparsed)
        base = result.model.split("/")[-1] if "/" in result.model else result.model
        new_name = base
        counter = 1
        while new_name in existing_names:
            new_name = f"{base}-{counter}"
            counter += 1
        new_profiles = dict(self._profiles.profiles)
        new_profiles[new_name] = result
        new_config = ModelConnectionsConfig(
            profiles=new_profiles,
            active=self._profiles.active,
            unparsed=dict(self._profiles.unparsed),
        )
        self.dismiss(ProfileManagerResult(edited=new_config))

    def action_cancel(self) -> None:
        self.dismiss(None)
