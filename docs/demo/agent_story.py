"""Deterministic `LLMProvider` for the documentation Agent capture.

Not shipped with the package — a development harness driven by
`docs/demo/agent.tape` (VHS). See `docs/demo/README.md`.

The capture must show korvid's own agent loop, so nothing here fabricates
panel events: the provider only decides *what the model would say*, and the
shipped `AgentRuntime` does the rest — it dispatches the tool calls through
the real `ToolExecutor`, feeds the results back as `role="tool"` messages,
mints the `[E1]`/`[E2]` references in the real `EvidenceLedger`, and
validates the answer's citations against them.

Deterministic, and offline by construction: `complete` never opens a socket
and never reads a credential, so the recording needs no provider account, no
network and no cluster. The pauses are pacing for the camera — a turn that
resolves instantly reads as a canned animation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from korvid.agent.provider import LLMProvider
from korvid.agent.runtime import AgentRuntime
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.reads import ReadOps
from korvid.tools.executor import ToolExecutor

#: The pod the whole story is about; it is the synthetic fixture's
#: CrashLoopBackOff pod, so both reads return real fixture content.
DEMO_POD = "payment-worker-6c9f7d-b3xnq"
DEMO_NAMESPACE = "shop"

#: The answer, split where the camera should see it arrive. Both markers are
#: claims about reads this turn performs — the runtime rejects any other
#: reference, so an edit that cites evidence the turn never gathered fails
#: `test_demo_agent_turn_uses_real_tools_and_mints_citations` instead of
#: quietly publishing an unsupported citation.
ANSWER_CHUNKS: tuple[str, ...] = (
    "The payment worker is repeatedly restarting after gateway failures. [E1] ",
    "Its recent logs show repeated gateway 503 responses; inspect the owner ",
    "and upstream availability before changing the workload. [E2]",
)


class DemoAgentProvider(LLMProvider):
    """Answers each iteration of one recorded turn with a fixed decision.

    Iteration 1 diagnoses the pod, iteration 2 reads its logs, iteration 3
    writes the answer. The messages it is handed are recorded rather than
    ignored: `seen_messages` is what proves the tool results really came
    back through the runtime, and is asserted on in the documentation
    contracts.
    """

    def __init__(self) -> None:
        self._iteration = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "deterministic-demo"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, stream
        self.seen_messages.append([dict(message) for message in messages])
        self._iteration += 1
        await asyncio.sleep(0.8)
        if self._iteration == 1:
            yield {
                "type": "tool_call",
                "id": "demo-diagnose",
                "name": "diagnose_pod",
                "arguments": f'{{"pod":"{DEMO_POD}","namespace":"{DEMO_NAMESPACE}"}}',
            }
        elif self._iteration == 2:
            yield {
                "type": "tool_call",
                "id": "demo-logs",
                "name": "get_logs",
                "arguments": (
                    f'{{"pod":"{DEMO_POD}","namespace":"{DEMO_NAMESPACE}"'
                    ',"container":"app","tail_lines":12}'
                ),
            }
        else:
            for chunk in ANSWER_CHUNKS:
                await asyncio.sleep(0.45)
                yield {"type": "text_delta", "text": chunk}


def build_demo_agent_runtime(
    reads: ReadOps,
    aliases: Mapping[str, ResourceMeta],
    *,
    provider: DemoAgentProvider | None = None,
) -> AgentRuntime:
    """The shipped runtime, wired to the synthetic cluster.

    Args:
        reads: the documentation fixture's `ReadOps` implementation.
        aliases: the kind aliases the executor resolves tool arguments with.
        provider: the deterministic provider to drive the turn with. Defaults
            to a fresh one; the contracts pass their own so they can inspect
            the messages the runtime handed over.

    Returns:
        A real `AgentRuntime` over a real `ToolExecutor` — no write tools, no
        UI bridge, no proposal tools.
    """
    return AgentRuntime(
        provider or DemoAgentProvider(),
        ToolExecutor(reads, aliases),
        cluster_context="current",
    )
