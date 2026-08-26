"""A clean external MCP client for the follow capture.

Not shipped with the package — this is the right-hand pane of the two-pane
recording driven by ``docs/demo/mcp-follow.tape`` (VHS). It is a real MCP
SDK ``ClientSession`` speaking Streamable HTTP to the ``KorvidMCPServer``
that the ``mcp`` scene of ``docs/demo/demo.py`` serves on :data:`URL`, so
the clip's follow evidence is a genuine external host driving the running
TUI — nothing about the exchange is replayed or drawn.

Everything this pane prints is authored here and bounded. A landing clip
publishes whatever the terminal showed, so the client never echoes the
server's endpoint file, its own process, its working directory, the name of
any assistant, a token count, or an unbounded tool result: it prints a fixed
excerpt of each answer and holds long enough for the mirrored view to be
read. An answer the SDK flags as an error is not an excerpt of korvid's
work at all, so the run aborts on it rather than publishing it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

#: The loopback endpoint the `mcp` demo scene binds. Nothing outside this
#: machine is contacted, and no credential takes part.
URL = "http://127.0.0.1:7878/mcp"

NAMESPACE = "shop"
POD = "payment-worker-6c9f7d-b3xnq"

#: Lines of each tool result the pane prints. Tool results are unbounded
#: text (`get_logs` alone can return 500 lines), and a result that scrolled
#: the story out of the pane would take the evidence with it.
TAIL_LINES = 5

#: How long the closing card stays up. The capture stops before it
#: elapses: a client that exited first would close its pane and reflow the
#: TUI to full width inside the last captured frames.
CLOSING_HOLD = 6.0

#: Printable width of one pane line: one column short of the 62 the tape's
#: split leaves this pane. Tool results are far wider than that, and a
#: single soft-wrapped line would push the story up the pane, so lines are
#: clipped instead — the left pane is where a result is read in full.
LINE_WIDTH = 61

#: Lines of a structured answer :func:`_sections` prints. `diagnose_pod`'s
#: `CONTAINERS` section grows with the container count, so a named section
#: is no more bounded by nature than a raw tail is: without a cap, a wider
#: fixture would scroll the verdict beat — and korvid's mirrored view of it
#: — out of the captured frames. Two tails' worth, because this beat shows
#: two sections rather than one answer's end.
SECTION_MAX_LINES = TAIL_LINES * 2


def _text(result: Any, call: str) -> str:
    """The text blocks of one `tools/call` answer, joined.

    Args:
        result: The SDK's `CallToolResult` for `call`.
        call: The tool name that was called, for the failure message.

    Returns:
        The answer's text blocks joined by newlines.

    Raises:
        RuntimeError: if the SDK flagged the answer as an error. A failed
            `tools/call` still carries `content` — the server's error text
            — so joining it would publish a failure as though it were
            korvid's answer and let the run reach its closing card. The
            message names `call` only; it never echoes the result, which
            is unbounded and may hold sensitive cluster text.
    """
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool call failed: {call}")
    return "\n".join(
        str(getattr(item, "text", "")) for item in result.content if getattr(item, "text", "")
    )


def _line(text: str) -> str:
    """One pane line: clipped to :data:`LINE_WIDTH`, never wrapped."""
    return text if len(text) <= LINE_WIDTH else text[: LINE_WIDTH - 1] + "…"


def _asking(name: str, **arguments: str | int) -> None:
    """Announce the call before it is made, so the pane reads in order."""
    shown = " ".join(f"{key}={value}" for key, value in arguments.items())
    print(_line(f"\ncall {name}"))
    print(_line(f"     {shown}"))


def _tail(text: str, call: str) -> list[str]:
    """The last :data:`TAIL_LINES` lines of an answer.

    Args:
        text: The answer's text, already checked for `is_error` by `_text`.
        call: The tool name that produced `text`, for the failure message.

    Returns:
        The answer's last :data:`TAIL_LINES` lines, in order.

    Raises:
        RuntimeError: if `text` holds no line to print — empty, or only
            whitespace. The caller (`_answered`) would otherwise print
            nothing and hold for the full beat anyway, publishing a blank
            evidence beat that looks like normal pacing. The message
            names `call` only; it never echoes `text`, which is unbounded
            and may hold sensitive cluster text.
    """
    if not text.strip():
        raise RuntimeError(f"MCP tool call answered with nothing to show: {call}")
    return text.splitlines()[-TAIL_LINES:]


def _sections(text: str, *names: str) -> list[str]:
    """The named sections of a structured answer, in the order asked.

    A section is its unindented header line plus the indented lines under
    it. `diagnose_pod` answers with far more than a pane can hold, and its
    last lines are log excerpts — naming the sections keeps this beat on
    the verdict instead of on whichever lines happen to fall last.

    A header must match a requested name *whole* (a trailing colon aside):
    `CONTAINERS` names one section, and a sibling `CONTAINERS SUMMARY`
    would otherwise open on the same request and carry its whole body into
    the pane. Repeated names are collected once, and the result is clipped
    to :data:`SECTION_MAX_LINES` for the same reason `_tail` clips.

    Raises:
        RuntimeError: if none of ``names`` matched a header in ``text`` —
            drifted headers or an error answer would otherwise fall
            through as an empty, silently-skipped beat. The message names
            the sections that were asked for; it never echoes ``text``,
            which is unbounded and may hold sensitive tool output.
    """
    kept: list[str] = []
    for name in dict.fromkeys(names):
        keeping = False
        for line in text.splitlines():
            if line[:1].strip():
                keeping = line.rstrip(":") == name
            if keeping:
                kept.append(line)
    if not kept:
        raise RuntimeError(f"diagnose_pod answer is missing every requested section: {names!r}")
    return kept[:SECTION_MAX_LINES]


async def _answered(lines: list[str], hold: float) -> None:
    """Print one bounded excerpt, then hold on the view korvid mirrored."""
    for line in lines:
        print(_line(f"  {line}"))
    await asyncio.sleep(hold)


async def main() -> None:
    print("external MCP client — MCP SDK over Streamable HTTP")
    print(f"connecting to {URL}")
    print("read-only tools only; korvid follows every answer")
    async with (
        streamable_http_client(URL) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        _asking("list_resources", kind="pods", namespace=NAMESPACE)
        listed = await session.call_tool("list_resources", {"kind": "pods", "namespace": NAMESPACE})
        await _answered(_tail(_text(listed, "list_resources"), "list_resources"), 2.2)

        _asking("diagnose_pod", pod=POD, namespace=NAMESPACE)
        diagnosed = await session.call_tool("diagnose_pod", {"pod": POD, "namespace": NAMESPACE})
        await _answered(
            _sections(_text(diagnosed, "diagnose_pod"), "CURRENT HEALTH", "CONTAINERS"), 3.2
        )

        _asking("get_logs", pod=POD, container="app", tail_lines=12)
        log_arguments: dict[str, Any] = {
            "pod": POD,
            "namespace": NAMESPACE,
            "container": "app",
            "tail_lines": 12,
        }
        logged = await session.call_tool("get_logs", log_arguments)
        await _answered(_tail(_text(logged, "get_logs"), "get_logs"), 3.6)

        _asking("helm_list_releases", namespace=NAMESPACE)
        released = await session.call_tool("helm_list_releases", {"namespace": NAMESPACE})
        await _answered(_tail(_text(released, "helm_list_releases"), "helm_list_releases"), 2.4)

        print("\nread-only investigation complete —")
        print("korvid followed every answer onto the screen.")
        await asyncio.sleep(CLOSING_HOLD)


if __name__ == "__main__":
    asyncio.run(main())
