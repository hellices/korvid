"""korvid's agent layer: one interaction harness, published as one surface.

`__all__` below is what the composition root and the UI wire against,
grouped by the part of the harness that owns it:

| group | what it is |
| --- | --- |
| interaction | the typed workspace snapshot and UI actions (`AgentUiBridge`) |
| model routing | `ModelRouter` resolving a `ResolvedAgentPolicy` |
| prompt | `PromptHarness` composing the deterministic layer order |
| gateway | `RequestGateway` — the single provider boundary and its snapshot |
| tools | `ToolHarness` — policy-aware dispatch, evidence, approval routing |
| engine | `AgentEngine`/`NativeAgentEngine` — the one agent loop |
| session | `AgentSession`/`DefaultAgentSession` — the one production session |
| provider | the `LLMProvider` ABC every adapter and plugin implements |
| evidence | the citation ledger |
| events | what a turn yields to the UI |
| setup | the configurator contract the setup screen drives |

Five public contracts are deliberately **not** in that list: they live in
two submodules a plugin author imports them from directly.

- `korvid.agent.provider_plugin` — `ProviderPlugin`,
  `ProviderPluginMetadata`, `ProviderPluginConfig`, and
  `PROVIDER_PLUGIN_API_VERSION` (the API 2 descriptor/capability
  contract `ValidatedPluginProvider` enforces);
- `korvid.agent.credentials` — `CredentialSource`.

They stay out of `__all__` because publishing them would make the plugin
validator part of every start, which is the boundary
`tests/test_optional_extras.py` pins; naming them here costs nothing
because this is prose, not an import.

Attributes resolve lazily (PEP 562). `korvid.__main__` imports
`korvid.agent.interaction` before it knows whether the agent is enabled, and
an eager package import would drag the engine, the gateway and the provider
ABC into every MCP-only or read-only start. Static analysis still sees the
real types through the `TYPE_CHECKING` block below.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .engine import (
        AgentEngine as AgentEngine,
    )
    from .engine import (
        AgentTurnRequest as AgentTurnRequest,
    )
    from .events import (
        AgentError as AgentError,
    )
    from .events import (
        AgentEvent as AgentEvent,
    )
    from .events import (
        TextDelta as TextDelta,
    )
    from .events import (
        ToolCallFinished as ToolCallFinished,
    )
    from .events import (
        ToolCallStarted as ToolCallStarted,
    )
    from .events import (
        TurnComplete as TurnComplete,
    )
    from .events import (
        TurnInterrupted as TurnInterrupted,
    )
    from .evidence import (
        Evidence as Evidence,
    )
    from .evidence import (
        EvidenceLedger as EvidenceLedger,
    )
    from .interaction import (
        AgentUiBridge as AgentUiBridge,
    )
    from .interaction import (
        ClusterFacts as ClusterFacts,
    )
    from .interaction import (
        DrillDown as DrillDown,
    )
    from .interaction import (
        InteractionContext as InteractionContext,
    )
    from .interaction import (
        Navigate as Navigate,
    )
    from .interaction import (
        OpenDescribe as OpenDescribe,
    )
    from .interaction import (
        OpenLogs as OpenLogs,
    )
    from .interaction import (
        PaneContext as PaneContext,
    )
    from .interaction import (
        ResourceIdentity as ResourceIdentity,
    )
    from .interaction import (
        SetFilter as SetFilter,
    )
    from .interaction import (
        UiAction as UiAction,
    )
    from .interaction import (
        UiActionResult as UiActionResult,
    )
    from .model_policy import (
        CapabilitySource as CapabilitySource,
    )
    from .model_policy import (
        ModelCapabilities as ModelCapabilities,
    )
    from .model_policy import (
        ModelCatalogEntry as ModelCatalogEntry,
    )
    from .model_policy import (
        ModelDescriptor as ModelDescriptor,
    )
    from .model_policy import (
        ModelRouter as ModelRouter,
    )
    from .model_policy import (
        ModelRoutingError as ModelRoutingError,
    )
    from .model_policy import (
        ModelTier as ModelTier,
    )
    from .model_policy import (
        PolicyEnvironment as PolicyEnvironment,
    )
    from .model_policy import (
        ResolvedAgentPolicy as ResolvedAgentPolicy,
    )
    from .native_engine import NativeAgentEngine as NativeAgentEngine
    from .outbound import (
        OutboundPolicy as OutboundPolicy,
    )
    from .outbound import (
        OutboundSnapshot as OutboundSnapshot,
    )
    from .prompt_harness import (
        ComposedPrompt as ComposedPrompt,
    )
    from .prompt_harness import (
        PromptCompositionError as PromptCompositionError,
    )
    from .prompt_harness import (
        PromptHarness as PromptHarness,
    )
    from .prompt_harness import (
        PromptInputs as PromptInputs,
    )
    from .prompt_harness import (
        StaticPromptTooLargeError as StaticPromptTooLargeError,
    )
    from .prompt_harness import (
        UnknownPromptOverlayError as UnknownPromptOverlayError,
    )
    from .prompt_harness import (
        UnknownPromptPackError as UnknownPromptPackError,
    )
    from .prompt_harness import (
        cluster_context_note as cluster_context_note,
    )
    from .provider import (
        REQUEST_SENT as REQUEST_SENT,
    )
    from .provider import (
        LLMProvider as LLMProvider,
    )
    from .request_gateway import (
        PreparedGatewayRequest as PreparedGatewayRequest,
    )
    from .request_gateway import (
        RequestGateway as RequestGateway,
    )
    from .session import (
        AgentSession as AgentSession,
    )
    from .session import (
        DefaultAgentSession as DefaultAgentSession,
    )
    from .session import (
        SessionRetargetError as SessionRetargetError,
    )
    from .setup import (
        AgentConfigurator as AgentConfigurator,
    )
    from .setup import (
        AgentSettings as AgentSettings,
    )
    from .setup import (
        DeviceLoginPrompt as DeviceLoginPrompt,
    )
    from .tool_harness import (
        ToolExecution as ToolExecution,
    )
    from .tool_harness import (
        ToolHarness as ToolHarness,
    )

#: Published name -> the submodule that defines it. One source of truth for
#: `__all__` and for lazy attribute resolution, so a name can never be
#: advertised without being importable.
_EXPORTS: Final[dict[str, str]] = {
    "AgentConfigurator": "setup",
    "AgentEngine": "engine",
    "AgentError": "events",
    "AgentEvent": "events",
    "AgentSession": "session",
    "AgentSettings": "setup",
    "AgentTurnRequest": "engine",
    "AgentUiBridge": "interaction",
    "CapabilitySource": "model_policy",
    "ClusterFacts": "interaction",
    "ComposedPrompt": "prompt_harness",
    "DefaultAgentSession": "session",
    "DeviceLoginPrompt": "setup",
    "DrillDown": "interaction",
    "Evidence": "evidence",
    "EvidenceLedger": "evidence",
    "InteractionContext": "interaction",
    "LLMProvider": "provider",
    "ModelCapabilities": "model_policy",
    "ModelCatalogEntry": "model_policy",
    "ModelDescriptor": "model_policy",
    "ModelRouter": "model_policy",
    "ModelRoutingError": "model_policy",
    "ModelTier": "model_policy",
    "NativeAgentEngine": "native_engine",
    "Navigate": "interaction",
    "OpenDescribe": "interaction",
    "OpenLogs": "interaction",
    "OutboundPolicy": "outbound",
    "OutboundSnapshot": "outbound",
    "PaneContext": "interaction",
    "PolicyEnvironment": "model_policy",
    "PreparedGatewayRequest": "request_gateway",
    "PromptCompositionError": "prompt_harness",
    "PromptHarness": "prompt_harness",
    "PromptInputs": "prompt_harness",
    "REQUEST_SENT": "provider",
    "RequestGateway": "request_gateway",
    "ResolvedAgentPolicy": "model_policy",
    "ResourceIdentity": "interaction",
    "SessionRetargetError": "session",
    "SetFilter": "interaction",
    "StaticPromptTooLargeError": "prompt_harness",
    "TextDelta": "events",
    "ToolCallFinished": "events",
    "ToolCallStarted": "events",
    "ToolExecution": "tool_harness",
    "ToolHarness": "tool_harness",
    "TurnComplete": "events",
    "TurnInterrupted": "events",
    "UiAction": "interaction",
    "UiActionResult": "interaction",
    "UnknownPromptOverlayError": "prompt_harness",
    "UnknownPromptPackError": "prompt_harness",
    "cluster_context_note": "prompt_harness",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve a published contract from the submodule that defines it.

    Args:
        name: A name from `__all__`.

    Returns:
        The published object.

    Raises:
        AttributeError: For any name this package does not publish.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{module}", __name__), name)


def __dir__() -> list[str]:
    """The published surface, so tab completion matches `__all__`."""
    return list(__all__)
