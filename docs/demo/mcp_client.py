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

Aborting is not enough on its own. VHS records for a fixed window and never
observes this pane, so a run that raised would still yield an apparently
finished asset — with a traceback in the frames and the TUI reflowed to full
width once the pane closed. :func:`run` therefore turns any failure into two
repository-local status files, :data:`OK_FILE` and :data:`FAILED_FILE`. The
tape only records and leaves both markers in place; it can decide nothing,
because VHS exits 0 whatever the shell it typed into did. The verdict belongs
to ``docs/demo/record-mcp-follow.sh``, the wrapper that runs VHS: it grades
the two markers afterwards and promotes the candidate render onto the
published clip only when failure is absent and success is present, rejecting
the candidate — and leaving the previously approved clip untouched — on
every other outcome.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

#: The loopback endpoint the `mcp` demo scene binds. Nothing outside this
#: machine is contacted, and no credential takes part.
URL = "http://127.0.0.1:7878/mcp"

#: Published once all four calls and the closing card have been printed —
#: the one thing that distinguishes a complete story from a pane that
#: connected, printed two beats and died. VHS cannot tell those apart.
OK_FILE = Path(".korvid-mcp-demo-client-ok")

#: Published instead when the run fails. `docs/demo/record-mcp-follow.sh`
#: rejects the candidate on this file even if the success file is also
#: present: a failure raised inside the closing hold is still a failed run.
#: Both files live in the checkout being recorded, like the two handshake
#: files, and are removed on both sides of a run — :func:`run` clears them
#: before the story starts, so only what this run publishes can grade it,
#: and the wrapper removes them again once it has graded them.
FAILED_FILE = Path(".korvid-mcp-demo-client-failed")

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

#: How long a failed run holds its pane open. The same reflow argument that
#: sizes :data:`CLOSING_HOLD` applies to a failure, only harder — nothing
#: about the run is worth publishing, but a pane that closes mid-capture
#: corrupts the *left* pane's frames too, and the tape's own teardown has
#: not run yet. So the hold outlasts the tape's 15 s visible window rather
#: than the remainder of one beat. Bounded, so a failed run still ends on
#: its own instead of hanging the recording.
FAILURE_HOLD = 30.0

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


def _section_body(lines: list[str], name: str) -> list[str]:
    """Every line of the section `name` opens: its header plus its body.

    A header must match `name` *whole* (a trailing colon aside):
    `CONTAINERS` names one section, and a sibling `CONTAINERS SUMMARY`
    would otherwise open on the same request and carry its whole body into
    the pane.

    Args:
        lines: The answer, already split into lines.
        name: The exact section title to collect.

    Returns:
        The matched header lines and the indented lines under them, in the
        order they appear; empty when `name` opens no section.
    """
    body: list[str] = []
    keeping = False
    for line in lines:
        if line[:1].strip():
            keeping = line.rstrip(":") == name
        if keeping:
            body.append(line)
    return body


def _sections(text: str, *names: str) -> list[str]:
    """The named sections of a structured answer, in the order asked.

    `diagnose_pod` answers with far more than a pane can hold, and its
    last lines are log excerpts — naming the sections keeps this beat on
    the verdict instead of on whichever lines happen to fall last.

    Repeated names are collected once, and the pane's
    :data:`SECTION_MAX_LINES` budget is divided evenly among the sections
    actually asked for. Clipping each section to its own share is what
    keeps this beat whole: a single `CURRENT HEALTH` grown past the budget
    would otherwise consume the pane and leave `CONTAINERS` unprinted —
    the exact failure this helper exists to prevent, one level up. The
    total is clipped once more at the end, as a safety net.

    Raises:
        RuntimeError: if any of ``names`` opened no section in ``text`` —
            drifted headers or an error answer would otherwise fall
            through as a half-printed, silently-skipped beat. The message
            names the sections that were missing; it never echoes
            ``text``, which is unbounded and may hold sensitive tool
            output.
    """
    wanted = list(dict.fromkeys(names))
    lines = text.splitlines()
    collected = {name: _section_body(lines, name) for name in wanted}
    missing = tuple(name for name in wanted if not collected[name])
    if missing or not wanted:
        raise RuntimeError(
            f"diagnose_pod answer is missing a requested section: {missing or names!r}"
        )
    per_section = max(1, SECTION_MAX_LINES // len(wanted))
    kept: list[str] = []
    for name in wanted:
        kept.extend(collected[name][:per_section])
    return kept[:SECTION_MAX_LINES]


async def _answered(lines: list[str], hold: float) -> None:
    """Print one bounded excerpt, then hold on the view korvid mirrored."""
    for line in lines:
        print(_line(f"  {line}"))
    await asyncio.sleep(hold)


def _publish(status: Path) -> None:
    """Publish one status file for the recorder to grade once VHS has returned.

    The marker is empty, so creating it is a single `open(O_CREAT)`:
    `docs/demo/record-mcp-follow.sh` either sees the file or does not, and
    there is no content it could observe half-written. Nothing is stored *in*
    it — a status file that carried the reason would be one more unbounded,
    unreviewed string living in the checkout.

    Args:
        status: :data:`OK_FILE` or :data:`FAILED_FILE`.
    """
    status.touch()


def _clear_markers() -> None:
    """Drop both markers, including ones an interrupted run left behind.

    This run may only be graded on evidence this run produced. Relying on
    an outside pre-clean meant a stale :data:`OK_FILE` in the checkout
    survived a client killed before it published anything (SIGKILL, a tape
    timeout), and `docs/demo/record-mcp-follow.sh` then read "failure
    absent, success present" and promoted a broken candidate. Owning the
    markers here is the same defence `docs/demo/demo.py` gives its
    readiness file; the tape's own `rm -f` stays as a second layer.
    """
    OK_FILE.unlink(missing_ok=True)
    FAILED_FILE.unlink(missing_ok=True)


async def main() -> None:
    """The story itself: four read-only calls, printed at reading speed.

    Publishes :data:`OK_FILE` once the closing card is printed and before
    the closing hold, so the marker certifies a story that finished rather
    than a process that survived. Failures propagate to :func:`run`.
    """
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
        _publish(OK_FILE)
        await asyncio.sleep(CLOSING_HOLD)


async def run() -> None:
    """Run :func:`main` behind the status handshake the recorder grades.

    Clears both markers first, so only what this run publishes can grade
    it. The clearing happens *inside* the failure channel on purpose: a
    marker that cannot be removed (a read-only checkout, a permission
    error) leaves a stale success on disk, so the run must not start —
    publishing :data:`FAILED_FILE` rejects the candidate whatever else is
    lying about beside it, and keeps the traceback out of the frames.

    Raises:
        SystemExit: with status 1 if the story failed, so a direct run still
            reports the failure — and reports it as the one exception the
            interpreter exits on silently. Letting the original propagate
            would print a traceback into a pane that is being recorded, and
            printing the exception would publish an unbounded string that
            may hold sensitive cluster text. Neither reaches a frame: the
            verdict travels in :data:`FAILED_FILE` instead, and the pane is
            held open past the capture so it cannot close and reflow the
            TUI inside the last frames.

    `BaseException` is deliberately not caught: an interrupt or a cancelled
    run must stay interrupting. Neither can forge a success, because
    :data:`OK_FILE` is published only after the closing card is printed.
    """
    try:
        _clear_markers()
        await main()
    except Exception:
        _publish(FAILED_FILE)
        print(_line("\nclient run failed — this recording will be rejected."))
        await asyncio.sleep(FAILURE_HOLD)
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(run())
