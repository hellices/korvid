Output docs/assets/scenes/.agent-demo.candidate.mp4

Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 8
Set TypingSpeed 70ms
Set Shell bash

Hide
Type "uv run --frozen python docs/demo/demo.py --scene agent"
Enter
# Hidden cold-start allowance shared by every tape; see the reason in
# docs/demo/visual-storytelling.md ("Running the tapes reproducibly").
Sleep 20s
Show
# The demo harness already auto-opened and focused the real AgentPanel
# `#agent-input` widget by this point; type straight into it and press
# Enter through the genuine Input/on_input_submitted path.
Type "Why is the payment worker failing?"
Enter
# The grounded turn runs two real read tools before it answers; hold on the
# settled answer long enough to read both tool rows and both citations.
Sleep 12s
Hide
# The focused input owns printable keys; close the panel through its
# priority binding before sending the app-level quit key.
Ctrl+A
Sleep 1s
Type "q"
