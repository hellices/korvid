# Landing video experience design

## Goal

Make the landing-page demonstrations feel alive without requiring a visitor to
press Play, and replace the abbreviated Agent and MCP clips with complete,
legible operational stories.

The result should behave like a lightweight GIF while retaining the pause,
seek, fallback, accessibility, and bandwidth advantages of local MP4 video.

## Scope

This change covers:

- playback behavior for the landing hero and three-driver scene switcher;
- a new deterministic Agent demonstration;
- a new MCP follow demonstration;
- the landing copy and provenance statements that describe those captures;
- focused tests and local browser verification.

It does not redesign the surrounding landing page, introduce remote media, run
a live model in documentation tests, or change Korvid's production Agent or MCP
contracts.

## Playback contract

### Hero

The hero video starts automatically when it is visible, muted, inline, and
looping. It pauses after leaving the viewport and restarts from the beginning
when it becomes visible again. Native controls remain available so a visitor
can pause or seek.

### Scene switcher

Only the selected scene may play. Selecting a tab:

1. pauses every other scene;
2. promotes that scene's deferred poster and video source;
3. starts the selected video from the beginning when the switcher is visible.

Leaving the switcher's viewport pauses all three videos. Returning restarts
only the selected scene from the beginning. No inactive scene downloads video
bytes before its first selection.

### Motion preference and browser policy

When `prefers-reduced-motion: reduce` matches, no video starts
programmatically. Posters and native controls remain usable.

Every `play()` call handles its returned promise. A browser-policy rejection is
not an application error: the poster and controls remain visible and usable.
Other initialization errors keep the existing no-JavaScript fallback behavior
and are reported through the current per-switcher error path.

## Agent story

### Visitor-visible sequence

The new Agent clip runs for approximately 12 to 15 seconds:

| Time | Visible event |
|---|---|
| 0–3 s | The real AgentPanel receives `Why is the payment worker failing?` through its input widget. |
| 3–6 s | A pod diagnosis read identifies `CrashLoopBackOff` and repeated restarts. |
| 6–9 s | A log read surfaces repeated synthetic gateway `503` entries. |
| 9–12 s | The panel renders a concise answer with citations created by the turn's evidence ledger. |
| 12–15 s | The grounded answer remains still long enough to read. |

### Execution boundary

The capture uses a deterministic provider and the real Agent loop, tool
executor, evidence ledger, and AgentPanel against the documentation-only
synthetic cluster. No external provider, credential, network call, or live
cluster is involved.

The deterministic provider may choose the fixed tool sequence, but it must not
inject pre-rendered tool events or a pre-rendered answer directly into the
panel. Tool results and citation identifiers come from the real documentation
tool path. The final answer may be deterministic, but its citations must refer
to evidence minted during that turn.

Landing and Agent-guide copy describe this as a deterministic synthetic-cluster
walkthrough. They must not present it as proof of live-model quality or a live
cluster investigation.

### On-screen chrome and privacy

The Agent frame is korvid's own terminal, so korvid's own chrome stays in it.
The panel header reads `⚡ korvid-demo · ~↑… ↓… tok`: `korvid-demo` is the
synthetic label the capture harness passes as `agent_model_name`, and the
counters are korvid's local character estimate, which is what the leading `~`
marks — the deterministic provider reports no usage for korvid to display.
Neither identifies an external provider nor states a bill, and covering a
product's own header would misrepresent the product.

What must not appear anywhere in the frame is evidence about someone real: a
real or external provider identity, billing or token-spend metadata, a
credential, a user path or branch, a real cluster or context name, or
unrelated client chrome.

## MCP story

### Visitor-visible sequence

The replacement MCP clip runs for approximately 12 to 15 seconds and follows
the original MCP demo contract:

| Time | MCP activity | Visible Korvid state |
|---|---|---|
| 0–3 s | The external request is submitted and `list_resources` runs. | The `shop` pod list identifies the unhealthy workload. |
| 3–6 s | `diagnose_pod` runs. | Korvid follows to the pod diagnosis. |
| 6–10 s | `get_logs` runs. | The log view remains readable for at least two seconds. |
| 10–13 s | `helm_list_releases` runs. | Korvid follows to Helm releases. |
| 13–15 s | The client presents its summary. | The final Helm state remains visible. |

The capture contains real read-only MCP requests and real follow-mode
navigation. Model or network idle time may be removed, but calls and TUI
transitions must not be synthesized, reordered, or globally accelerated.

### Framing and privacy

The external client region includes only the prompt, relevant tool activity,
and final summary. That region's model name, token spend, repository paths,
branches, startup inventories, credentials, and unrelated editor chrome stay
outside the frame rather than being covered after capture: they identify a
real external provider, a real bill, and a real working tree. Korvid's own
region keeps its product chrome under the Agent story's rule above.

The synthetic logs contain no credential-like values or real identifiers. The
clip includes no mutation or approval dialog.

## Assets and presentation

- Keep MP4 as the visitor-facing format.
- Preserve a local poster for every clip and the current no-JavaScript image
  fallbacks.
- Replace the Agent and MCP scene MP4s and posters in place so existing links
  remain stable.
- Record at the existing 1280-pixel width and preserve square-pixel geometry.
- Prefer 12 to 15 frames per second for terminal captures.
- Hold evidence and final states rather than slowing the entire video.
- Update media provenance with the exact harness, scenario, editing boundary,
  duration, and truthfulness limitations.

## Focused verification

The work does not run the repository-wide quality gate. Verification is limited
to the changed behavior:

- controller tests for visible selected-scene autoplay, offscreen pause,
  reduced-motion behavior, rejected `play()` promises, and no eager inactive
  video download;
- Agent tests proving the capture harness traverses the real tool and evidence
  path and produces valid citations;
- MCP asset checks for the required scene order, a readable log hold, duration,
  dimensions, square pixels, and absence of excluded client details;
- existing landing accessibility and no-JavaScript fallback tests affected by
  the markup change;
- local MkDocs serving and browser checks at desktop and mobile widths.

## Acceptance criteria

The work is complete when:

1. the hero starts without a click for visitors who allow motion;
2. only the visible selected scene starts automatically;
3. all programmatic playback stops offscreen and under reduced motion;
4. the Agent clip visibly progresses from prompt to two real synthetic reads to
   a cited answer;
5. the MCP clip visibly holds the log view before reaching Helm releases and
   the final summary;
6. both clips remain truthful about their evidence and execution boundaries;
7. no real cluster, credential, user path, branch, external provider identity,
   or billing and token-spend metadata is present — the Agent clip's synthetic
   `korvid-demo` label and korvid's own estimated `tok` counters are product
   rendering, not live-provider or cost evidence;
8. focused tests and local browser playback pass without running the full
   quality gate.
