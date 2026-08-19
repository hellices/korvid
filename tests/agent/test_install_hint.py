import pytest

from korvid import __version__
from korvid.agent.install_hint import isolated_install_hint


@pytest.mark.parametrize("extras", ["agent", "mcp", "observability", "all,entra"])
def test_install_hint_preserves_extras_and_uses_isolated_tools(extras: str) -> None:
    hint = isolated_install_hint(extras=extras)
    requirement = f"korvid[{extras}]=={__version__}"
    assert f"uv tool install --force '{requirement}'" in hint
    assert f"pipx install --force '{requirement}'" in hint
    assert "pip install" not in hint
    assert "development checkout or active virtualenv" in hint
    assert "complete extras in that environment instead" in hint
