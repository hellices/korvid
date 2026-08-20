# korvid MCP follow README demo.
# Prepare the disposable cluster and tmux session as described in
# docs/demo/mcp-follow.md, then run:
#
#   vhs docs/demo/mcp-follow.tape

Output docs/assets/mcp-follow-demo.raw.gif

Set FontSize 14
Set Width 1440
Set Height 800
Set Framerate 25
Set Padding 8
Set TypingSpeed 5ms
Set Shell bash

# The export step in mcp-follow.md scales this 1440px source to 1280px/12fps.

Hide
Type "tmux attach-session -t $(cat ${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-cluster-name)"
Enter
Sleep 3s
Show

Type "Use korvid MCP in order: list_resources shop pods → get_logs unhealthy one → helm_list_releases."
Sleep 400ms
Enter
# Capture a slow model too; export trims the observed tail at demo_end.
Sleep 45s

Hide
Ctrl+B
Type "d"
