Output docs/assets/scenes/relationship-demo.mp4

Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 8
Set TypingSpeed 90ms
Set Shell bash

Hide
Type "uv run --frozen python docs/demo/demo.py --scene relationships"
Enter
# 20s, not the 6s a warm shell needs: `uv run` may resolve and install the
# project's dependencies on a cold cache before the TUI ever starts, and
# every keystroke below would otherwise land in the shell. The sleep is
# inside `Hide`, so it costs the recording nothing.
Sleep 20s
Show
Type "/"
Type "payment-worker"
Enter
Sleep 2s
Type "g"
Sleep 5s
Hide
Type "q"
