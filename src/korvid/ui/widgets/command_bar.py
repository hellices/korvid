from __future__ import annotations

from collections.abc import Callable

from textual.events import Key
from textual.suggester import Suggester
from textual.widgets import Input

from korvid.ui.command import parse_command


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

    def on_mount(self) -> None:
        self.display = False
        self.placeholder = "pods | deploy all | ns <name> | q"
        self.known: Callable[[str], str | None] = lambda _: None
        self.command_words: list[str] = []
        self.namespace_words: list[str] = []
        self.context_words: list[str] = []
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
        """Second-token completion: namespaces for :ns, contexts for :ctx."""
        if head in {"ns", "namespaces"} and rest:
            for ns in self.namespace_words:
                if ns.startswith(rest) and ns != rest:
                    return f"{head} {ns}"
        if head in {"ctx", "context", "contexts"} and rest:
            for ctx in self.context_words:
                if ctx.startswith(rest) and ctx != rest:
                    return f"{head} {ctx}"
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
