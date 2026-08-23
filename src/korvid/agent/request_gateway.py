"""The single seam every embedded-provider request crosses.

`RequestGateway` is the one place the runtime reaches a provider. It owns
two invariants that were previously spread across the engine loop:

- **Exact handoff proof.** The session's *latest outbound payload* — the
  thing a user can export and inspect — changes only once a request has
  demonstrably reached the provider, never when a payload was merely
  built. `LLMProvider.complete` is an async generator, so obtaining it
  transmits nothing; the body runs on the first `__anext__`. A built-in
  adapter proves the handoff explicitly with `REQUEST_SENT` (emitted once
  the transport accepted the request, before the status code is judged);
  a plugin, which the published contract forbids from emitting that
  bookkeeping event, proves it with its first completion event. Either
  way the gateway records the pending snapshot exactly once, before it
  awaits the provider again, and signals the caller through a synchronous
  callback so token accounting can settle a request that really ran.

- **`REQUEST_SENT` never escapes.** The engine's stream contract knows
  text, tool calls, usage and done; the gateway consumes the built-in
  acknowledgement and never yields it onward.

Preparation is synchronous and hermetic: caller-supplied tool schemas are
deep-thawed into private copies, the provider's dialect hook runs *before*
the outbound policy so anything it adds is sanitized and captured in the
snapshot, and the provider is handed a payload reconstructed from the
canonical snapshot — so no later mutation of the returned object can
change what actually crosses the boundary.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from korvid.agent.outbound import (
    OutboundPolicy,
    OutboundSnapshot,
    PreparedOutbound,
    provider_prepared_messages,
)
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.core.redaction import RedactionRecord


class RequestProvenance(Protocol):
    """What a message carries that its text alone cannot say.

    Structural view of `korvid.agent.conversation.RequestView`: the
    per-position ingress redactions whose evidence a prior pass may have
    removed, and the indices of tool results the *producer* declared
    failures. Both are projected onto the exact indices of the messages
    handed to `prepare`, so the outbound policy can address them
    positionally.
    """

    @property
    def ingress(self) -> Mapping[int, Sequence[RedactionRecord]]: ...

    @property
    def tool_errors(self) -> Collection[int]: ...


@dataclass(frozen=True)
class PreparedGatewayRequest:
    """A sanitized, snapshotted request plus the estimate of its prompt size.

    `prepared` is the canonical `PreparedOutbound` (messages, tools, and the
    immutable exact snapshot). `prompt_estimate` is measured from the exact
    serialized payload the boundary produced — not from history accounting,
    which cannot see what preparation dropped to fit or what a provider
    dialect hook added inside the boundary — so a provider that omits usage
    is never charged zero input for a request that was really transmitted.
    """

    prepared: PreparedOutbound
    prompt_estimate: int

    @property
    def snapshot(self) -> OutboundSnapshot:
        """The immutable canonical record of the exact redacted payload."""
        return self.prepared.snapshot


def _thaw(value: Any) -> Any:
    """Deep-copy a schema into plain, mutable `dict`/`list` structures.

    Tool schemas may arrive as immutable `MappingProxyType`/tuples (a
    plugin's frozen config, a shared registry entry). The outbound policy
    requires plain lists, and the gateway must not retain a reference to a
    caller-owned object, so every mapping becomes a fresh `dict` and every
    sequence a fresh `list`. Scalars are returned as-is.
    """
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


class RequestGateway:
    """Own every request from preparation through proven handoff."""

    def __init__(self, provider: LLMProvider, policy: OutboundPolicy) -> None:
        """Wire the gateway to one provider and its outbound policy.

        Args:
            provider: The adapter this gateway is the sole caller of.
            policy: The fail-closed boundary that shapes, redacts, bounds
                and snapshots each request.
        """
        self._provider = provider
        self._policy = policy
        self._latest_outbound_payload: OutboundSnapshot | None = None
        self._active_iterator: AsyncIterator[dict[str, Any]] | None = None

    @property
    def latest_outbound_payload(self) -> OutboundSnapshot | None:
        """The last payload proven to have reached the provider, or None.

        Updated only on handoff proof, so a request that could not be
        transmitted (a missing credential, an unresolvable host) leaves the
        previous real handoff on display.
        """
        return self._latest_outbound_payload

    def prepare(
        self,
        messages: list[dict[str, Any]],
        tools: Any,
        *,
        iteration: int,
        provenance: RequestProvenance | None = None,
    ) -> PreparedGatewayRequest:
        """Build one bounded, redacted, snapshotted request. Synchronous.

        The provider's dialect hook runs *before* the outbound policy, so a
        field it adds is sanitized, size-checked and recorded in the exact
        snapshot rather than slipped past the boundary inside `complete`.

        Args:
            messages: Conversation history to send, already projected onto
                the indices the provenance addresses.
            tools: Tool schemas; frozen `MappingProxyType`/tuple structures
                are thawed into private copies before the policy sees them.
            iteration: One-based tool-loop iteration, recorded verbatim in
                the snapshot.
            provenance: Per-position ingress redactions and producer-declared
                tool-result failures. `None` means neither applies.

        Returns:
            The prepared request and the prompt-size estimate measured from
            its exact payload.

        Raises:
            OutboundPolicyError: if the request cannot be safely prepared.
        """
        thawed_tools = _thaw(tools)
        prepared_messages = provider_prepared_messages(self._provider, messages)
        prepared = self._policy.prepare(
            self._provider.descriptor.model,
            prepared_messages,
            thawed_tools,
            iteration=iteration,
            ingress=provenance.ingress if provenance is not None else None,
            tool_errors=provenance.tool_errors if provenance is not None else None,
        )
        prompt_estimate = len(prepared.snapshot.payload_json) // 4
        return PreparedGatewayRequest(prepared=prepared, prompt_estimate=prompt_estimate)

    async def stream(
        self,
        prepared: PreparedGatewayRequest,
        on_transmitted: Callable[[], None],
    ) -> AsyncIterator[dict[str, Any]]:
        """Transmit one prepared request and yield its completion events.

        The provider is handed a payload reconstructed from the canonical
        snapshot, so no mutation of `prepared` between preparation and this
        call can change what crosses the boundary. On the first event — a
        built-in's `REQUEST_SENT` or a plugin's first completion event — the
        snapshot becomes the session's latest outbound payload and
        `on_transmitted` is called, exactly once and before the next await.
        `REQUEST_SENT` is consumed; every other event is yielded onward. The
        provider iterator is closed on normal completion, error and
        cancellation without masking the primary exception.

        Args:
            prepared: The request returned by `prepare`.
            on_transmitted: Synchronous callback invoked once, at the moment
                handoff is proven.
        """
        snapshot = prepared.snapshot
        payload = json.loads(snapshot.payload_json)
        iterator = self._provider.complete(payload["messages"], payload["tools"])
        self._active_iterator = iterator
        acknowledged = False
        try:
            async for event in iterator:
                if not acknowledged:
                    acknowledged = True
                    self._latest_outbound_payload = snapshot
                    on_transmitted()
                if event.get("type") == REQUEST_SENT:
                    continue
                yield event
        except BaseException:
            # Preserve the primary exception/cancellation: the close must
            # never replace or leak into it.
            await _aclose_suppress(iterator)
            raise
        else:
            await _aclose(iterator)
        finally:
            if self._active_iterator is iterator:
                self._active_iterator = None

    async def aclose(self) -> None:
        """Close the active provider iterator, if a stream is in flight.

        This is iterator lifecycle only: the provider itself is owned and
        closed by the composition root, not by the gateway.
        """
        iterator = self._active_iterator
        if iterator is not None:
            self._active_iterator = None
            await _aclose_suppress(iterator)


async def _aclose(iterator: AsyncIterator[dict[str, Any]]) -> None:
    """Close an async iterator on the normal path, letting close errors surface."""
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        await aclose()


async def _aclose_suppress(iterator: AsyncIterator[dict[str, Any]]) -> None:
    """Close an async iterator, suppressing every close error.

    Used when a primary exception or cancellation is already propagating,
    so the close cannot replace or leak into it.
    """
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        with contextlib.suppress(BaseException):
            await aclose()
