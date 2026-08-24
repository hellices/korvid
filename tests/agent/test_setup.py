"""`AgentSettings` — the record the `:ai` wizard hands to the composition root.

The wizard's choice travels as this dataclass from the setup screen to
`rebuild`, to `create_provider`, and (via `_persist_agent_settings`) into
`config.yaml`. Everything downstream treats `model_tier` as already
checked: `ModelRouter.resolve` takes a non-`None` tier as the user's own
decision and routes it without validating, and `save_agent_config` writes
whatever it is given. So the value has to be refused where it is first
assembled, not three layers later.
"""

from __future__ import annotations

import dataclasses

import pytest

from korvid.agent.setup import AgentSettings


def _settings(**overrides: object) -> AgentSettings:
    base: dict[str, object] = {
        "provider": "openai",
        "auth_method": "api_key",
        "base_url": "http://localhost:9999/v1",
        "model": "m",
    }
    base.update(overrides)
    return AgentSettings(**base)  # type: ignore[arg-type]  # kwargs are the test's subject


@pytest.mark.parametrize("tier", [None, "low", "high"])
def test_the_three_routes_korvid_ships_are_accepted(tier: str | None) -> None:
    """Automatic (`None`), `low`, and `high` — the same three `config.yaml`
    accepts for `agent.model_tier`."""
    settings = _settings(model_tier=tier)

    assert settings.model_tier == tier


@pytest.mark.parametrize("tier", ["small", "full", "LOW", "High", "", " ", "medium"])
def test_a_tier_no_router_can_route_is_refused_where_it_is_set(tier: str) -> None:
    """Including the two retired arm names.

    The removed profile key's `small` arm is spelled `low` now. A
    settings object carrying `small` reaches `ModelRouter.resolve`, which
    honours an explicit tier as the user's decision — the routed policy
    would be neither of korvid's arms, and the header would report the
    routing as the user's own choice.
    """
    with pytest.raises(ValueError, match="model_tier"):
        _settings(model_tier=tier)


def test_the_refusal_names_the_values_that_would_work() -> None:
    """An error an operator can act on without reading the source."""
    with pytest.raises(ValueError, match="model_tier") as caught:
        _settings(model_tier="small")

    message = str(caught.value)
    assert "small" in message
    assert "low" in message
    assert "high" in message


def test_a_non_string_tier_is_refused_too() -> None:
    """The wizard is not the only caller: a plugin or a hand-built settings
    object can pass anything a dataclass will hold."""
    with pytest.raises(ValueError, match="model_tier"):
        _settings(model_tier=1)


def test_the_check_runs_before_the_options_are_frozen() -> None:
    """`__post_init__` freezes `options` into read-only mappings.

    Doing that work for an object that is about to be refused is wasted,
    and (worse) leaves a half-initialised frozen dataclass reachable from
    an exception handler's traceback frame.
    """
    with pytest.raises(ValueError, match="model_tier"):
        _settings(model_tier="small", options={"nested": {"a": 1}})


def test_replacing_a_valid_settings_object_is_revalidated() -> None:
    """`dataclasses.replace` re-runs `__post_init__`, and the `:model`
    command builds its rebuild settings exactly that way."""
    settings = _settings(model_tier="low")

    with pytest.raises(ValueError, match="model_tier"):
        dataclasses.replace(settings, model_tier="small")

    assert dataclasses.replace(settings, model_tier="high").model_tier == "high"


def test_options_are_still_frozen_for_an_accepted_tier() -> None:
    """The validation must not displace what `__post_init__` already did."""
    settings = _settings(model_tier="high", options={"nested": {"a": 1}})

    with pytest.raises(TypeError, match="does not support item assignment"):
        settings.options["nested"] = 2  # type: ignore[index]  # read-only is the test
