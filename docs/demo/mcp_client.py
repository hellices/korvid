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
width once the pane closed. :func:`run` therefore turns any failure into a
repository-local status file, :data:`FAILED_FILE`, published best-effort
beside the strict :data:`OK_FILE`. :func:`run` owns both: the story in
:func:`main` prints and returns, and the success marker is written only once
that coroutine has returned — which is to say once its session and its HTTP
transport have closed without raising — so no failure can arrive after a
success has already been published. The tape only records and leaves whatever
markers exist in place; it can decide nothing, because VHS exits 0 whatever
the shell it typed into did. The verdict belongs to
``docs/demo/record-mcp-follow.sh``, the wrapper that runs VHS: it promotes
the candidate render onto the published clip only when the failure marker is
absent and the success marker is present, rejecting the candidate — and
leaving the previously approved clip untouched — on every other outcome,
including a read-only checkout that stopped this run from writing either
marker at all. Under the shipped wrapper, which removes stale markers before
starting VHS, no :data:`OK_FILE` is enough on its own to reject the candidate.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

#: The loopback endpoint the `mcp` demo scene binds. Nothing outside this
#: machine is contacted, and no credential takes part.
URL = "http://127.0.0.1:7878/mcp"

#: Published once all four calls and the closing card have been printed and
#: the session and its transport have closed without raising — the one thing
#: that distinguishes a complete story from a pane that connected, printed
#: two beats and died. VHS cannot tell those apart. :func:`run` publishes it,
#: after :func:`main` has returned: written from inside the story it would
#: certify a run whose own teardown had not happened yet.
OK_FILE = Path(".korvid-mcp-demo-client-ok")

#: Published instead when the run fails. `docs/demo/record-mcp-follow.sh`
#: rejects the candidate on this file even if the success file is also
#: present, as defence in depth — though the client no longer publishes a
#: failure after a success: :data:`OK_FILE` is written only once everything
#: except the local closing hold has succeeded.
#: Publishing it is best-effort: a read-only checkout or permission error can
#: stop this write, and :func:`run` must not let that second failure escape as
#: a traceback. When the shipped wrapper's authoritative pre-clean succeeded,
#: it still sees no success marker from this run and rejects. Both files live in
#: the checkout being recorded, like the two handshake files, and are
#: removed on both sides of a run — :func:`run` clears them before the story
#: starts, so only what this run publishes can grade it, and the wrapper
#: removes them again once it has graded them.
FAILED_FILE = Path(".korvid-mcp-demo-client-failed")

NAMESPACE = "shop"
POD = "payment-worker-6c9f7d-b3xnq"

#: Lines of each tool result the pane prints. Tool results are unbounded
#: text (`get_logs` alone can return 500 lines), and a result that scrolled
#: the story out of the pane would take the evidence with it.
TAIL_LINES = 5

#: How long the closing card stays up. The capture stops before it
#: elapses: a client that exited first would close its pane and reflow the
#: TUI to full width inside the last captured frames. :func:`run` owns it,
#: after the success marker is published: the hold is local pacing for the
#: frames — nothing outside this process waits on it or observes it — so it
#: is a plain `asyncio.sleep`, and it stands outside the failure channel
#: because a story already certified as complete may not be re-graded by
#: whatever happens while its pane idles.
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

    This run may only be graded on evidence this run produced, so the
    client owns that guarantee itself rather than trusting it to whatever
    invoked it. Both `docs/demo/record-mcp-follow.sh` (before it starts
    VHS) and the tape itself (`rm -f` at the top of its recorded commands)
    already pre-clean these markers, so under the shipped recording flow
    this is defence in depth, not the only thing standing between a stale
    :data:`OK_FILE` and a promoted candidate. It is load-bearing the moment
    that assumption doesn't hold: this module invoked directly (this file
    run outside the wrapper, as the tests here do), or an external
    pre-clean skipped or removed by a future edit. Owning the markers here
    is the same defence `docs/demo/demo.py` gives its readiness file.
    """
    OK_FILE.unlink(missing_ok=True)
    FAILED_FILE.unlink(missing_ok=True)


async def main() -> None:
    """The story itself: four read-only calls, printed at reading speed.

    Prints its four beats and the closing card, closes the `ClientSession`
    and the Streamable HTTP transport it opened, and returns. It publishes
    no marker and takes no closing hold: the verdict is :func:`run`'s, and
    only :func:`run` can see whether this coroutine's own teardown — every
    `__aexit__` above — completed. Failures propagate there.
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


async def run() -> None:
    """Run :func:`main` behind the status handshake the recorder grades.

    Clears both markers first, so only what this run publishes can grade it
    when the client is invoked directly. The shipped wrapper performs the
    authoritative pre-clean before VHS; this local clear is defence in depth.
    If it fails, the run does not start, and a best-effort
    :data:`FAILED_FILE` plus the fixed failure hold keeps the ordinary failure
    path bounded without claiming it can repair an unremovable stale marker.

    :data:`OK_FILE` is published here, not in :func:`main`, and only once
    `main` has *returned*: awaiting it to completion is what proves the
    `ClientSession` and the Streamable HTTP transport both closed without
    raising. Published from inside `main` it certified a run several
    `__aexit__`s before that run had finished, so anything the teardown
    raised arrived in this failure channel with a success already on disk —
    and because publishing :data:`FAILED_FILE` is best-effort, a checkout
    that could not take that second write left the wrapper a lone
    :data:`OK_FILE` and it promoted a failed run. Publishing success only
    after `main` returns makes the ordering total for this run's own markers:
    the failure channel is entered before this run can publish success.
    Removal of inherited markers remains the wrapper's authoritative
    precondition.

    Publishing :data:`FAILED_FILE` is itself best-effort: the same
    read-only checkout or permission error that broke `_clear_markers` (or
    anything else `main` raised) can just as easily stop this write, and a
    second `OSError` escaping this block would print a chained traceback
    into the recorded pane and skip the fixed failure line, the hold and
    the `SystemExit` below — the very failure this function exists to
    prevent. So an `OSError` from publishing the failure marker is caught
    and ignored. After the wrapper's pre-clean, it rejects the candidate
    because this run never published :data:`OK_FILE`. `_publish(OK_FILE)` is
    not given the same leniency: it stands inside the `try` above, so if it
    raises, the run is failed like any other.

    The closing hold stands *after* the failure channel, not in it: a
    published :data:`OK_FILE` is final, and an ordinary `Exception` from
    the hold must not be able to publish :data:`FAILED_FILE` beside a
    success it cannot retract, or print the fixed failure line under a
    story that finished. The hold is a plain `asyncio.sleep` — pacing for
    the frames rather than part of the story — and it is local: nothing
    outside this process observes it, and the capture stops before it
    elapses. A cancellation still interrupts it, exactly as it interrupts
    every other await here.

    Raises:
        SystemExit: with status 1 if the story failed, so a direct run still
            reports the failure — and reports it as the one exception the
            interpreter exits on silently. Letting the original propagate
            would print a traceback into a pane that is being recorded, and
            printing the exception would publish an unbounded string that
            may hold sensitive cluster text. Neither reaches a frame: the
            verdict travels in :data:`FAILED_FILE` instead when it can be
            written, and the pane is held open past the capture so it
            cannot close and reflow the TUI inside the last frames.

    `BaseException` is deliberately not caught: an interrupt or a cancelled
    run must stay interrupting. Neither can forge a success, because
    :data:`OK_FILE` is published only after the whole story, its teardown
    included, has succeeded.
    """
    try:
        _clear_markers()
        await main()
        _publish(OK_FILE)
    except Exception:
        with contextlib.suppress(OSError):
            _publish(FAILED_FILE)
        print(_line("\nclient run failed — this recording will be rejected."))
        await asyncio.sleep(FAILURE_HOLD)
        raise SystemExit(1) from None
    await asyncio.sleep(CLOSING_HOLD)


if __name__ == "__main__":
    asyncio.run(run())
