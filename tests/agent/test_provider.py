import pytest

from korvid.agent.provider import LLMProvider


def test_provider_is_abstract() -> None:
    with pytest.raises(TypeError, match="abstract"):
        LLMProvider()  # type: ignore[abstract]  # instantiating ABC is the test
