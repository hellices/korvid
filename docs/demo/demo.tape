# korvid README demo tape — drives docs/demo/demo.py (fake-data harness).
# Regenerate the GIF from the repository root:
#
#   vhs docs/demo/demo.tape
#
# Requires vhs (https://github.com/charmbracelet/vhs) with ttyd + ffmpeg.
Output docs/assets/demo.gif

Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 8
Set TypingSpeed 120ms
Set Shell bash

Hide
Type "uv run python docs/demo/demo.py"
Enter
Sleep 6s
Show

# Browse the pod table
Sleep 2.5s
Down@300ms 4
Sleep 1.5s

# Toggle all namespaces and back
Type "0"
Sleep 2.5s
Type "0"
Sleep 1.5s

# Filter down to the crashlooping payment pod
Type "/"
Sleep 800ms
Type "payment"
Sleep 800ms
Enter
Sleep 2s

# Describe it — manifest + warning events
Type "d"
Sleep 4.5s
Escape
Sleep 1s

# Tail its logs
Type "l"
Sleep 5.5s
Escape
Sleep 1s

# Jump to another resource kind with the command bar
Type ":"
Sleep 500ms
Type "deploy"
Sleep 500ms
Enter
Sleep 3s

# Help overlay
Type "?"
Sleep 3.5s
Escape
Sleep 1.5s

Hide
Type "q"
