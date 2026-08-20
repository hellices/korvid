# MCP follow README demo

## Goal

Add a second README animation that proves an external assistant can use korvid
over MCP while the real TUI visibly follows its investigation.

The animation must communicate the product behavior in 15 seconds or less:

1. one natural-language request triggers real korvid MCP tool calls;
2. each read moves the TUI to the evidence being inspected;
3. the sequence ends in the Helm release browser;
4. no cluster mutation is implied.

The existing keyboard-driven TUI animation remains the primary demo. The new
animation belongs with the README's MCP feature description.

## Recording composition

Record one VS Code window with two visible regions:

- the korvid TUI occupies approximately 75% of the width in an editor-area
  terminal;
- GitHub Copilot Chat occupies approximately 25% in the secondary side bar.

Hide unrelated editor chrome and keep the TUI status bar visible. The
`MCP ·follow` indicator and Copilot's real MCP tool-call cards are the visual
proof that the screen changes come from the external assistant.

Capture at approximately 1440 by 900 pixels and export the final asset at
1280 pixels wide. The GIF should use 12 to 15 frames per second and remain
small enough for the README to load quickly, targeting 8 MB or less.

## Scenario

Enter this prompt before recording starts:

> In the shop namespace, find the unhealthy pod, inspect the cause and open
> its logs, then show me the Helm releases.

Start the visible recording with the Enter key. The expected tool and screen
sequence is:

| Time | MCP activity | Visible korvid state |
|---|---|---|
| 0–1 s | Prompt submitted | Pod view with `MCP ·follow` visible |
| 1–4 s | `list_resources` | Shop pod list |
| 4–7 s | `diagnose_pod` | Describe screen for the unhealthy pod |
| 7–10 s | `get_logs` | Live logs for that pod |
| 10–13 s | `helm_list_releases` | Helm release browser |
| 13–15 s | No new call | End caption: **One prompt. Korvid follows.** |

Use a stable demo cluster or deterministic fixture data containing one
obviously unhealthy pod in `shop` and at least one Helm release. Allow the
known read-only korvid MCP tools in the recording session before capture so
confirmation prompts do not interrupt the sequence.

## Editing rules

The recording must contain real MCP requests and real follow-mode screen
transitions. Remove only model and network idle time. Do not synthesize tool
cards, fake transitions, reorder calls, or accelerate cursor and screen motion
to imply behavior that did not occur.

Hold each destination screen for roughly two seconds so the viewer can
distinguish the pod list, describe, logs, and Helm views. Prefer hard cuts in
idle regions over a global speed increase.

If the model chooses a different tool sequence, repeat the recording rather
than editing it into the expected sequence. The final take must visibly match
the tool calls shown in Copilot Chat.

## Scope and safety

This animation demonstrates MCP reads and follow mode only. korvid itself
supports Helm install, upgrade, rollback, and uninstall through the TUI, but
the current MCP surface exposes only Helm release reads. External write
proposals support delete, scale, rollout restart, and pod resize, not Helm
operations.

Do not include a Helm install wizard in this animation. A user-driven install
would dilute the follow-mode message, while implying an MCP-driven install
would misrepresent the current product boundary.

Do not record production resources, credentials, customer identifiers, or
logs containing sensitive data. MCP log results are tool-shaped but are not
credential-pattern masked by korvid.

## Repository changes

- Add the optimized animation as `docs/assets/mcp-follow-demo.gif`.
- Add recording and optimization instructions under `docs/demo/`.
- Add the animation and a one-sentence caption to the README MCP section.
- Keep `docs/assets/demo.gif` and its existing regeneration workflow unchanged.

The GUI recording itself is intentionally manual because VS Code Copilot Chat
is part of the evidence. Repository instructions should make the setup,
scenario, edit constraints, and export command repeatable even though the
model's response latency is not deterministic.

## Verification

- Confirm the final runtime is at most 15 seconds.
- Inspect the GIF at rendered README width and verify all four TUI states are
  distinguishable.
- Confirm the Chat tool cards match the visible transition order.
- Confirm `MCP ·follow` remains visible whenever the main TUI is shown.
- Confirm the final frame is the Helm release browser with the end caption.
- Confirm the file is 1280 pixels wide and no larger than 8 MB.
- Run existing README and documentation contract tests after the README change.
