from __future__ import annotations

from collections.abc import Callable

from textual.events import Key
from textual.reactive import var
from textual.suggester import Suggester
from textual.widgets import Input

from korvid.ui.command import ArgumentCompletion, argument_completion, parse_command


def _unknown_resource(_: str) -> str | None:
    return None


def _default_known() -> Callable[[str], str | None]:
    return _unknown_resource


class _CommandSuggester(Suggester):
    """Inline ghost-text completion for the command bar.

    Completes the first token from known commands (resource aliases plus
    built-ins like ``ns``) and, for ``ns <partial>``, the namespace name.
    """

    def __init__(self, bar: CommandBar) -> None:
        super().__init__(case_sensitive=True)
        self._bar = bar

    async def get_suggestion(self, value: str) -> str | None:
        return self._bar.complete(value)


class CommandBar(Input):
    """Hidden `:` command input; Enter dispatches onto the UI Bus."""

    known: var[Callable[[str], str | None]] = var(_default_known, init=False)
    command_words: var[list[str]] = var(list, init=False)
    namespace_words: var[list[str]] = var(list, init=False)
    context_words: var[list[str]] = var(list, init=False)

    def on_mount(self) -> None:
        self.display = False
        self.placeholder = "pods | deploy all | ns <name> | q"
        if self.suggester is None:
            self.suggester = _CommandSuggester(self)

    def complete(self, value: str) -> str | None:
        """Return the full completed command for ``value``, or None."""
        if not value or value != value.lstrip():
            return None
        head, sep, rest = value.partition(" ")
        if not sep:
            for word in self.command_words:
                if word.startswith(value) and word != value:
                    return word
            return None
        return self._complete_argument(head, rest)

    def _complete_argument(self, head: str, rest: str) -> str | None:
        """Complete an argument using the catalogued completion kind."""
        completion = argument_completion(head, self.known)
        words = (
            self.namespace_words
            if completion is ArgumentCompletion.NAMESPACE
            else self.context_words
            if completion is ArgumentCompletion.CONTEXT
            else ()
        )
        if rest:
            for word in words:
                if word.startswith(rest) and word != rest:
                    return f"{head} {word}"
        return None

    def open(self) -> None:
        self.value = ""
        self.display = True
        self.focus()

    def dismiss_bar(self) -> None:
        self.display = False
        self.value = ""

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.post_message(parse_command(event.value, self.known))
        self.dismiss_bar()

    async def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss_bar()
            event.stop()
        elif event.key == "tab":
            suggestion = self.complete(self.value)
            if suggestion is not None:
                self.value = suggestion
                self.cursor_position = len(suggestion)
            event.stop()
            event.prevent_default()
