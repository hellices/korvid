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
# Hidden cold-start allowance shared by every tape; see the reason in
# docs/demo/visual-storytelling.md ("Running the tapes reproducibly").
Sleep 20s
Show
Type "/"
Type "payment-worker"
Enter
Sleep 2s
Type "g"
Sleep 5s
Hide
# RelationshipScreen consumes the first q; wait for the modal to dismiss
# before sending the app-level quit key.
Type "q"
Sleep 1s
Type "q"
