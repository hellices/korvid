import pytest

from korvid.core.config import KorvidConfig
from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.registry import create_provider


def test_none_when_agent_disabled() -> None:
    assert create_provider(KorvidConfig()) is None


def test_openai_compat_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "sk-1")
    cfg = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai-compat",
        agent_base_url="http://x/v1",
        agent_model="m",
        agent_api_key_env="K",
    )
    p = create_provider(cfg)
    assert isinstance(p, OpenAICompatProvider)


def test_aliases_accepted() -> None:
    for alias in ("openai", "ollama", "azure", "vllm"):
        cfg = KorvidConfig(
            agent_enabled=True,
            agent_provider=alias,
            agent_base_url="http://x/v1",
            agent_model="m",
        )
        assert isinstance(create_provider(cfg), OpenAICompatProvider)


def test_none_when_model_missing() -> None:
    cfg = KorvidConfig(
        agent_enabled=True, agent_provider="openai-compat", agent_base_url="http://x/v1"
    )
    assert create_provider(cfg) is None


def test_none_when_provider_unknown() -> None:
    cfg = KorvidConfig(
        agent_enabled=True,
        agent_provider="mystery",
        agent_base_url="http://x/v1",
        agent_model="m",
    )
    assert create_provider(cfg) is None
