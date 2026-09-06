from dataclasses import FrozenInstanceError

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from korvid.core.store import ALL_NAMESPACES
from korvid.ui import command, messages
from korvid.ui.command import parse_command
from korvid.ui.messages import (
    NavigateCommand,
    QuitCommand,
    ShowNamespacePicker,
    SortCommand,
    UnknownCommand,
)

# ---------------------------------------------------------------------------
# Shared fixture: fake `known` callable
# ---------------------------------------------------------------------------

_KNOWN: dict[str, str] = {
    "deploy": "deployments",
    "deployment": "deployments",
    "deployments": "deployments",
    "po": "pods",
    "pod": "pods",
    "pods": "pods",
}


def _known(alias: str) -> str | None:
    return _KNOWN.get(alias)


# ---------------------------------------------------------------------------
# Legacy grammar (must still work with the new two-arg signature)
# ---------------------------------------------------------------------------


def test_pods_navigates() -> None:
    msg = parse_command("pods", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "pods"


def test_ns_switches_namespace() -> None:
    msg = parse_command("ns prod", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.namespace == "prod"
    assert msg.view is None  # keep current kind


def test_quit() -> None:
    assert isinstance(parse_command("q", _known), QuitCommand)
    assert isinstance(parse_command("quit", _known), QuitCommand)


def test_unknown_preserved() -> None:
    msg = parse_command("frobnicate all", _known)
    assert isinstance(msg, UnknownCommand)
    assert msg.text == "frobnicate all"


def test_bare_ns_requests_picker() -> None:
    assert isinstance(parse_command("ns", _known), ShowNamespacePicker)


# ---------------------------------------------------------------------------
# Grammar v2 — new cases
# ---------------------------------------------------------------------------


def test_alias_navigates_to_canonical_plural() -> None:
    msg = parse_command("deploy", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "deployments"
    assert msg.namespace is None


def test_alias_all_sets_all_namespaces() -> None:
    msg = parse_command("deploy all", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "deployments"
    assert msg.namespace == ALL_NAMESPACES


def test_alias_with_explicit_namespace() -> None:
    msg = parse_command("deploy prod", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "deployments"
    assert msg.namespace == "prod"


def test_namespaces_keyword_opens_picker() -> None:
    assert isinstance(parse_command("namespaces", _known), ShowNamespacePicker)


def test_empty_text_is_unknown() -> None:
    msg = parse_command("", _known)
    assert isinstance(msg, UnknownCommand)


def test_whitespace_only_is_unknown() -> None:
    msg = parse_command("   ", _known)
    assert isinstance(msg, UnknownCommand)


def test_unknown_alias_returns_unknown_command() -> None:
    msg = parse_command("frobnicator", _known)
    assert isinstance(msg, UnknownCommand)
    assert msg.text == "frobnicator"


def test_po_shortname_resolves() -> None:
    msg = parse_command("po", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "pods"


def test_ns_view_none_means_keep_current_kind() -> None:
    msg = parse_command("ns kube-system", _known)
    assert isinstance(msg, NavigateCommand)
    assert msg.view is None
    assert msg.namespace == "kube-system"


def test_builtin_names_reserved_over_resource_aliases() -> None:
    """A cluster CRD alias like `model` must not shadow the :model built-in."""

    def crd_known(head: str) -> str | None:
        return {"model": "models", "agent": "agents", "ai": "ais", "mcp": "mcps"}.get(head)

    expected = (
        ("ai", "AI", ()),
        ("ai payload", "AI", ("payload",)),
        ("agent", "AI", ()),
        ("model gpt-4o", "MODEL", ("gpt-4o",)),
        ("mcp", "MCP", ()),
        ("mcp on", "MCP", ("on",)),
    )
    for text, operation_name, arguments in expected:
        msg = parse_command(text, crd_known)
        assert isinstance(msg, messages.BuiltinCommand)
        assert msg.operation is messages.BuiltinOperation[operation_name]
        assert msg.arguments == arguments


def test_proposals_is_reserved_over_resource_aliases() -> None:
    """A cluster CRD alias named `proposals` must not shadow the external
    write-proposal inbox (issue #110): the approval surface has to stay
    reachable no matter what resources the cluster serves."""

    def crd_known(head: str) -> str | None:
        return "proposals" if head == "proposals" else None

    msg = parse_command("proposals", crd_known)
    assert isinstance(msg, messages.BuiltinCommand)
    assert msg.operation is messages.BuiltinOperation.PROPOSALS
    assert msg.arguments == ()


@pytest.mark.parametrize(
    ("text", "operation_name", "arguments"),
    [
        ("ai first second", "AI", ("first", "second")),
        ("agent first second", "AI", ("first", "second")),
        ("model first second", "MODEL", ("first", "second")),
        ("mcp first second", "MCP", ("first", "second")),
        ("proposals", "PROPOSALS", ()),
        ("pf", "PORT_FORWARDS", ()),
        ("tp", "TELEPRESENCE", ()),
        ("telepresence", "TELEPRESENCE", ()),
    ],
)
def test_app_owned_builtin_aliases_have_canonical_operations(
    text: str, operation_name: str, arguments: tuple[str, ...]
) -> None:
    msg = parse_command(text, lambda _: "shadow-resource")
    assert isinstance(msg, messages.BuiltinCommand)
    assert msg.operation is messages.BuiltinOperation[operation_name]
    assert msg.arguments == arguments


@pytest.mark.parametrize("text", ["tp extra", "telepresence extra", "proposals extra", "pf stop"])
def test_zero_argument_builtins_reject_arguments(text: str) -> None:
    msg = parse_command(text, _known)
    assert isinstance(msg, UnknownCommand)
    assert msg.text == text


@pytest.mark.parametrize(
    "field", ["aliases", "help", "operation", "completion", "maximum_arguments"]
)
def test_command_catalog_is_immutable(field: str) -> None:
    descriptor = command.COMMANDS[0]
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        setattr(descriptor, field, None)


def test_command_words_are_derived_from_catalog_and_resources() -> None:
    words = command.command_words(["pods", "deploy", "model"])
    catalog_aliases = {alias for descriptor in command.COMMANDS for alias in descriptor.aliases}
    assert words == sorted(catalog_aliases | {"pods", "deploy"})


@pytest.mark.parametrize(
    ("alias", "message_type"),
    [
        ("q", QuitCommand),
        ("quit", QuitCommand),
        ("ns", ShowNamespacePicker),
        ("namespaces", ShowNamespacePicker),
        ("ctx", messages.ShowContextPicker),
        ("context", messages.ShowContextPicker),
        ("contexts", messages.ShowContextPicker),
        ("sort", SortCommand),
    ],
)
def test_dedicated_builtin_aliases_precede_resources(
    alias: str, message_type: type[Message]
) -> None:
    assert isinstance(parse_command(alias, lambda _: "shadow-resource"), message_type)


@pytest.mark.parametrize("head", ["ns", "namespaces", "deploy", "pods"])
def test_argument_completion_uses_namespaces_for_scope_commands(head: str) -> None:
    assert command.argument_completion(head, _known) is command.ArgumentCompletion.NAMESPACE


@pytest.mark.parametrize("head", ["ctx", "context", "contexts"])
def test_argument_completion_uses_contexts_for_context_aliases(head: str) -> None:
    assert command.argument_completion(head, _known) is command.ArgumentCompletion.CONTEXT


def test_argument_completion_is_absent_for_non_scope_builtins_and_unknowns() -> None:
    assert command.argument_completion("model", _known) is None
    assert command.argument_completion("frobnicate", _known) is None


def test_command_help_lists_proposals() -> None:
    from korvid.ui.command import command_help

    commands = [cmd for cmd, _ in command_help()]
    assert ":proposals" in commands


def test_command_help_pins_ai_payload_usage() -> None:
    """`follow` takes its own argument, so it needs its own row.

    A flat `:ai [off|follow|payload]` reads as three interchangeable
    words and hides that `:ai follow` is a toggle taking `on|off` — the
    same nesting `:mcp follow` already spells out.
    """
    from korvid.ui.command import command_help

    ai_rows = [command for command, _ in command_help() if command.startswith(":ai")]
    assert ai_rows == [":ai [off|payload]", ":ai follow [on|off]"]


def test_command_help_keeps_every_ai_subcommand_reachable() -> None:
    """Splitting the row must not drop a documented subcommand."""
    from korvid.ui.command import command_help

    ai_help = " ".join(f"{command} {text}" for command, text in command_help() if ":ai" in command)
    for word in ("off", "payload", "follow", "on", "setup"):
        assert word in ai_help, f"missing :ai {word}"


# ---------------------------------------------------------------------------
# :sort (issue #45)
# ---------------------------------------------------------------------------


def test_sort_with_column_parses() -> None:
    msg = parse_command("sort TEAM", _known)
    assert isinstance(msg, SortCommand)
    assert msg.column == "TEAM"


def test_bare_sort_clears() -> None:
    msg = parse_command("sort", _known)
    assert isinstance(msg, SortCommand)
    assert msg.column is None


def test_sort_extra_args_is_unknown_even_with_sort_alias() -> None:
    # A cluster exposing a `sort` resource alias must not turn a malformed
    # :sort into navigation — sort is a reserved builtin.
    known = {"sort": "sorts", **_KNOWN}.get
    msg = parse_command("sort TEAM extra", known)
    assert isinstance(msg, UnknownCommand)


# ---------------------------------------------------------------------------
# :ctx — runtime context switching (issue #36)
# ---------------------------------------------------------------------------


def test_bare_ctx_requests_context_picker() -> None:
    from korvid.ui.messages import ShowContextPicker

    assert isinstance(parse_command("ctx", _known), ShowContextPicker)
    assert isinstance(parse_command("context", _known), ShowContextPicker)
    assert isinstance(parse_command("contexts", _known), ShowContextPicker)


def test_ctx_with_name_switches() -> None:
    from korvid.ui.messages import SwitchContextCommand

    msg = parse_command("ctx staging", _known)
    assert isinstance(msg, SwitchContextCommand)
    assert msg.name == "staging"


def test_ctx_is_reserved_never_navigates() -> None:
    """A cluster CRD alias named `ctx` must not shadow the builtin, and a
    malformed :ctx must not fall through to alias navigation."""

    def known_with_ctx(alias: str) -> str | None:
        return "ctxes" if alias == "ctx" else _known(alias)

    from korvid.ui.messages import ShowContextPicker

    assert isinstance(parse_command("ctx", known_with_ctx), ShowContextPicker)


def test_ctx_accepts_names_with_spaces() -> None:
    """Kubeconfig context names are arbitrary strings: the whole remainder
    after :ctx is the name, so `dev west` is switchable directly."""
    from korvid.ui.messages import SwitchContextCommand

    msg = parse_command("ctx dev west", _known)
    assert isinstance(msg, SwitchContextCommand)
    assert msg.name == "dev west"


def test_command_help_lists_ctx() -> None:
    from korvid.ui.command import command_help

    commands = [cmd for cmd, _ in command_help()]
    assert any("ctx" in cmd for cmd in commands)


def test_command_help_describes_every_ai_argument() -> None:
    """`:ai` is three actions, not one. A description that names only
    setup leaves `off` and `payload` as bare words in the usage column
    with nothing saying what they do."""
    from korvid.ui.command import command_help

    description = next(text for command, text in command_help() if command == ":ai [off|payload]")

    assert "setup" in description.lower()
    assert "disconnect" in description.lower()
    assert "payload" in description.lower()
    assert "(also :agent)" in description


class _NamespacePickerApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.navigation: NavigateCommand | None = None

    def compose(self) -> ComposeResult:
        from korvid.ui.widgets.namespace_picker import NamespacePicker

        yield NamespacePicker()

    def on_navigate_command(self, message: NavigateCommand) -> None:
        self.navigation = message


async def test_namespace_picker_preserves_current_view() -> None:
    from korvid.ui.widgets.namespace_picker import NamespacePicker
    from tests.ui.waits import until

    app = _NamespacePickerApp()
    async with app.run_test() as pilot:
        app.query_one(NamespacePicker).open(["prod"])
        await pilot.press("enter")
        await until(pilot, lambda: app.navigation is not None, label="namespace command delivered")
        assert app.navigation is not None
        assert app.navigation.view is None
        assert app.navigation.namespace == "prod"
