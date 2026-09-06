import pytest
from textual.app import App, ComposeResult
from textual.suggester import Suggester

from korvid.ui import command
from korvid.ui.widgets.command_bar import CommandBar

_KNOWN_RESOURCES = {"deploy": "deployments", "pods": "pods"}


def _bar() -> CommandBar:
    bar = CommandBar()
    bar.known = _KNOWN_RESOURCES.get
    bar.command_words = command.command_words(_KNOWN_RESOURCES)
    bar.namespace_words = ["default", "kube-system"]
    bar.context_words = ["dev west", "prod"]
    return bar


def test_standalone_bar_initializes_completion_state_before_mount() -> None:
    bar = CommandBar()
    bar.namespace_words.append("default")
    bar.context_words.append("dev-cluster")

    assert bar.complete("ns de") == "ns default"
    assert bar.complete("ctx dev") == "ctx dev-cluster"
    assert bar.complete("pods de") is None

    bar.known = _KNOWN_RESOURCES.get
    assert bar.complete("pods de") == "pods default"


def test_standalone_bars_have_independent_completion_lists() -> None:
    first = CommandBar()
    second = CommandBar()

    first.command_words.append("pods")
    first.namespace_words.append("default")
    first.context_words.append("dev")

    assert second.command_words == []
    assert second.namespace_words == []
    assert second.context_words == []


class _SuppliedSuggester(Suggester):
    async def get_suggestion(self, value: str) -> str | None:
        return value


class _CommandBarApp(App[None]):
    def __init__(self, bar: CommandBar) -> None:
        super().__init__()
        self._bar = bar

    def compose(self) -> ComposeResult:
        yield self._bar


async def test_mount_preserves_supplied_suggester() -> None:
    supplied = _SuppliedSuggester()
    bar = CommandBar(suggester=supplied)

    async with _CommandBarApp(bar).run_test():
        assert bar.suggester is supplied


@pytest.mark.parametrize("head", ["ns", "namespaces", "deploy", "pods"])
def test_namespace_arguments_complete_for_scope_commands(head: str) -> None:
    assert _bar().complete(f"{head} de") == f"{head} default"


@pytest.mark.parametrize("head", ["ctx", "context", "contexts"])
def test_context_arguments_complete_for_every_alias(head: str) -> None:
    assert _bar().complete(f"{head} dev") == f"{head} dev west"


def test_first_word_completion_uses_derived_command_words() -> None:
    bar = _bar()
    assert bar.complete("telep") == "telepresence"
    assert bar.complete("prop") == "proposals"


def test_argument_completion_does_not_depend_on_duplicate_alias_sets() -> None:
    bar = _bar()
    bar.known = _never_known
    assert bar.complete("model de") is None
    assert bar.complete("unknown de") is None


def _never_known(_: str) -> str | None:
    return None
