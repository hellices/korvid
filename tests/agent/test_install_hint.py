import pytest

from korvid import __version__
from korvid.agent.install_hint import isolated_install_hint


@pytest.mark.parametrize("feature", ["agent", "mcp", "observability", "Entra"])
def test_install_hint_names_feature_and_preserves_superset_extras(feature: str) -> None:
    hint = isolated_install_hint(feature=feature)
    requirement = f"korvid[all,entra]=={__version__}"
    assert feature in hint
    assert f"uv tool install --force '{requirement}'" in hint
    assert f"pipx install --force '{requirement}'" in hint
    assert "pip install" not in hint
    assert "development checkout or active virtualenv" in hint
    assert "complete extras in that environment instead" in hint
