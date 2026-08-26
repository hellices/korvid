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

Type "rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready; tmux kill-session -t korvid-mcp-demo 2>/dev/null; true"
Enter
Sleep 1s

# 139x42 is this terminal's own grid at the geometry set above, so the
# attach below resizes nothing. `-f /dev/null` starts the server without
# anyone's tmux.conf, and the status line — the one surface that would
# print a hostname, a user and today's date into a frame — is turned off
# before the split, so both panes are laid out over the full 42 rows.
Type "tmux -f /dev/null new-session -d -s korvid-mcp-demo -x 139 -y 42 'uv run --frozen python docs/demo/demo.py --scene mcp'"
Enter
Sleep 1s

Type "tmux set -t korvid-mcp-demo status off"
Enter
Sleep 500ms

# The client waits on a repo-local gate file rather than a fixed sleep:
# korvid's first start is slow and variable, and a client that raced it
# would open the story on a connection error. The gate below is dropped only
# after the scene publishes .korvid-mcp-demo-ready.
Type "tmux split-window -h -t korvid-mcp-demo -p 45 'while [ ! -f .korvid-mcp-demo-go ]; do sleep 0.1; done; uv run --frozen python docs/demo/mcp_client.py'"
Enter
Sleep 1s

Type "tmux select-pane -t korvid-mcp-demo:0.0"
Enter

# korvid's own startup: mount, first watch, and the MCP server's bind. The
# shared cold-start allowance every tape hides; see the reasoning in
# docs/demo/visual-storytelling.md. The allowance runs the scene's own
# readiness wait rather than an idle sleep: `--scene mcp` publishes
# .korvid-mcp-demo-ready from its Textual mount, and only after the MCP
# server reported itself bound. Bounded at 60s, so a server that never binds
# fails the recording instead of hanging it.
Type "waited=0; while [ ! -f .korvid-mcp-demo-ready ] && [ $waited -lt 600 ]; do sleep 0.1; waited=$((waited+1)); done"
Enter
Sleep 20s

# Release the client from a background subshell, so the gate is dropped from
# outside tmux and no keystroke of the trigger can reach the attached TUI —
# and only if that readiness signal really arrived, so the story can never
# open on a connection error.
Type "[ -f .korvid-mcp-demo-ready ] && ( sleep 0.7; touch .korvid-mcp-demo-go ) & clear; tmux attach-session -t korvid-mcp-demo"
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
Type "tmux kill-session -t korvid-mcp-demo 2>/dev/null; rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready"
Enter
Sleep 1s
