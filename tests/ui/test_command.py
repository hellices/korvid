from korvid.ui.command import parse_command
from korvid.ui.messages import NavigateCommand, QuitCommand, UnknownCommand


def test_pods_navigates() -> None:
    msg = parse_command("pods")
    assert isinstance(msg, NavigateCommand)
    assert msg.view == "pods"


def test_ns_switches_namespace() -> None:
    msg = parse_command("ns prod")
    assert isinstance(msg, NavigateCommand)
    assert msg.namespace == "prod"


def test_quit() -> None:
    assert isinstance(parse_command("q"), QuitCommand)
    assert isinstance(parse_command("quit"), QuitCommand)


def test_unknown_preserved() -> None:
    msg = parse_command("frobnicate all")
    assert isinstance(msg, UnknownCommand)
    assert msg.text == "frobnicate all"
