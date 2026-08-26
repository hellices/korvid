# Landing clip: an external MCP host drives korvid, and korvid follows.
#
# Two panes, one machine. The left pane runs the `mcp` scene of the demo
# harness — korvid's real TUI over the synthetic cluster, serving the real
# KorvidMCPServer over Streamable HTTP on 127.0.0.1:7878. The right pane
# runs docs/demo/mcp_client.py, a real MCP SDK ClientSession calling four
# read-only tools. Nothing is staged: every view the left pane opens is
# korvid's follow bridge mirroring the answer the right pane just got.
#
#   vhs docs/demo/mcp-follow.tape
#
# Reproducible from this repository alone: loopback only, no credential,
# no MCP endpoint file, no cluster. The tmux status line is turned off
# before anything is captured, so no hostname, user or date reaches a
# frame; the shell that composes the panes is never captured either.

Output docs/assets/scenes/mcp-follow-demo.mp4

Set Shell bash
Set FontSize 14
Set Width 1280
Set Height 710
Set Padding 0

# --- composition, never captured -------------------------------------
Hide

Type "rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready .korvid-mcp-demo-client-ok .korvid-mcp-demo-client-failed; tmux kill-session -t korvid-mcp-demo 2>/dev/null; true"
Enter
Sleep 1s

# 139x42 is this terminal's own grid at the geometry set above, so the
# attach below resizes nothing. `-f /dev/null` starts the server without
# anyone's tmux.conf, and the status line — the one surface that would
# print a hostname, a user and today's date into a frame — is turned off
# before the split, so both panes are laid out over the full 42 rows.
#
# Both panes are launched with `--extra mcp`. The MCP stack is an optional
# extra, not a dependency group, so a clean checkout running `uv run --frozen`
# alone gets an environment where neither the scene's lazy
# `korvid.mcp.server` import nor the client's MCP SDK import resolves. The
# flag changes nothing that reaches a frame: the same code runs and prints
# the same output.
Type "tmux -f /dev/null new-session -d -s korvid-mcp-demo -x 139 -y 42 'uv run --frozen --extra mcp python docs/demo/demo.py --scene mcp'"
Enter
Sleep 1s

Type "tmux set -t korvid-mcp-demo status off"
Enter
Sleep 500ms

# The client waits on a repo-local gate file rather than a fixed sleep:
# korvid's first start is slow and variable, and a client that raced it
# would open the story on a connection error. The gate below is dropped only
# after the scene publishes .korvid-mcp-demo-ready.
Type "tmux split-window -h -t korvid-mcp-demo -p 45 'while [ ! -f .korvid-mcp-demo-go ]; do sleep 0.1; done; uv run --frozen --extra mcp python docs/demo/mcp_client.py'"
Enter
Sleep 1s

Type "tmux select-pane -t korvid-mcp-demo:0.0"
Enter

# korvid's own startup: mount, first watch, and the MCP server's bind. The
# hidden allowance runs the scene's own readiness wait rather than an idle
# sleep; the reason the allowance is hidden at all is written down once, in
# docs/demo/visual-storytelling.md. `--scene mcp` publishes
# .korvid-mcp-demo-ready from its Textual mount, and only after the MCP
# server reported itself bound. The loop is bounded at a wall-clock 60s
# deadline via bash's own `SECONDS` builtin — reset with `SECONDS=0`, then
# looped while `$SECONDS` stays under 60 — so a server that never binds fails
# the recording instead of hanging it. Counting `sleep 0.1` iterations up to a
# fixed total instead assumes each one costs exactly 0.1s, but fork+exec of
# `sleep` itself is not free: measured wall time for that approach ran past
# this tape's 65s hidden allowance. `SECONDS` is wall-clock and immune to
# that overhead, so the deadline stays ~60s regardless of it.
#
# The Sleep below is VHS's own clock, and VHS never learns that the loop it
# typed has returned. So the allowance must outlast the loop's whole 60s
# wall-clock deadline rather than a typical start: at 65s the composition
# stays hidden even in the worst case, keeping >= 5s of margin. Readiness
# arriving early costs nothing — the client pane is still held by the
# separate .korvid-mcp-demo-go gate, dropped only below, so no part of the
# story can run inside the hidden block.
Type "SECONDS=0; while [ ! -f .korvid-mcp-demo-ready ] && [ $SECONDS -lt 60 ]; do sleep 0.1; done"
Enter
Sleep 65s

# Release the client from a background subshell, so the gate is dropped from
# outside tmux and no keystroke of the trigger can reach the attached TUI.
# Fail-closed, in an explicit branch: the release *and* the attach happen
# only where readiness is known to exist, and a missing signal prints the
# reason into the hidden block, tears the session down and exits without
# attaching. `[ -f ready ] && ( ... ) & clear; tmux attach-session` does not
# express that — bash's `&` terminates the whole and_or list, so the attach
# is a separate, unconditional command.
Type "if [ -f .korvid-mcp-demo-ready ]; then ( sleep 0.7; touch .korvid-mcp-demo-go ) & clear; tmux attach-session -t korvid-mcp-demo; else echo 'mcp-follow.tape: .korvid-mcp-demo-ready never appeared; failing without attach'; tmux kill-session -t korvid-mcp-demo 2>/dev/null; rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready .korvid-mcp-demo-client-ok .korvid-mcp-demo-client-failed; exit 1; fi"
Enter
Sleep 400ms

# --- the story, captured ---------------------------------------------
Show

# list_resources → pods table; diagnose_pod → describe; get_logs → log
# pane, held while the lines stay readable; helm_list_releases → Helm.
Sleep 15s

Hide

# --- teardown, never captured ----------------------------------------
Ctrl+B
Type "d"
Sleep 500ms

# VHS records the 15s above on its own clock and stops; it never observes
# the client pane, so a client that raised on its second call still yields
# an apparently finished asset. The client publishes its verdict into two
# repo-local files instead — .korvid-mcp-demo-client-ok only after all four
# calls and the closing card were printed, .korvid-mcp-demo-client-failed
# on any exception (no traceback, no error text, and the pane held open
# past this capture so it cannot close and reflow the TUI mid-frame).
#
# The recording is accepted only when failure is absent *and* success is
# present, and the failure file outranks the success file: a client that
# raised inside its own closing hold, after publishing success, is still a
# failed run. Anything else — a failure, or a run that never reached its
# fourth answer — tears the session down, removes every recording side
# effect and exits non-zero, so a truncated story cannot be published as a
# complete one. This runs after Hide and after the detach above: before
# Hide it would type a shell command into the captured frames, and before
# the detach it would type it into the attached TUI.
Type "if [ ! -f .korvid-mcp-demo-client-failed ] && [ -f .korvid-mcp-demo-client-ok ]; then tmux kill-session -t korvid-mcp-demo 2>/dev/null; rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready .korvid-mcp-demo-client-ok .korvid-mcp-demo-client-failed; else echo 'mcp-follow.tape: the client pane did not report a completed run; rejecting this recording'; tmux kill-session -t korvid-mcp-demo 2>/dev/null; rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready .korvid-mcp-demo-client-ok .korvid-mcp-demo-client-failed; exit 1; fi"
Enter
Sleep 1s
