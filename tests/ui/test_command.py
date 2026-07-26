from korvid.core.store import ALL_NAMESPACES
from korvid.ui.command import parse_command
from korvid.ui.messages import NavigateCommand, QuitCommand, ShowNamespacePicker, UnknownCommand

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

    for text in ("ai", "agent", "model gpt-4o", "mcp", "mcp on"):
        msg = parse_command(text, crd_known)
        assert isinstance(msg, UnknownCommand)
        assert msg.text == text
