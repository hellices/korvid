from korvid import __version__
from korvid.agent.install_hint import isolated_install_hint


def test_standard_install_hint_uses_isolated_tool_environments() -> None:
    hint = isolated_install_hint()
    requirement = f"korvid[all]=={__version__}"
    assert f"uv tool install --force '{requirement}'" in hint
    assert f"pipx install --force '{requirement}'" in hint
    assert "pip install" not in hint


def test_entra_install_hint_keeps_the_entra_extra() -> None:
    hint = isolated_install_hint(entra=True)
    requirement = f"korvid[all,entra]=={__version__}"
    assert f"uv tool install --force '{requirement}'" in hint
    assert f"pipx install --force '{requirement}'" in hint
