# Compact homepage

**Date:** 2026-08-27  
**Status:** Selected in unattended mode after visual comparison  
**Direction:** One media stage, three product highlights

## Problem

The homepage repeats the same product evidence:

- the Direct recording appears in both the hero and the three-driver switcher;
- the switcher explains Input, Evidence, and Result below videos that already
  show that flow;
- the contract map, write path, and six-card evidence mosaic restate concepts
  covered by the videos and focused guides;
- unconstrained video height can dominate or break the layout on wide or short
  viewports;
- the MCP recording has a slightly different native ratio from the other clips,
  while one forced ratio is applied directly to every video.

## Selected structure

### 1. Hero and driver switcher become one component

The hero keeps the headline, short copy, install command, and primary actions.
Its media column becomes the existing keyboard-accessible Direct / Agent / MCP
switcher. Direct is no longer rendered twice.

The stage shows one selected video at a time:

- Direct: the existing cockpit recording;
- Agent: the existing deterministic Agent recording;
- MCP: the existing local MCP follow recording.

The tabs carry the driver explanation. The repeated Input / Evidence / Result
rows and scene-specific paragraphs are removed.

### 2. A constrained media stage

The stage owns the 16:9 layout box. Each video fills that box with:

```css
width: 100%;
height: 100%;
object-fit: contain;
```

The component also uses:

```css
min-width: 0;
max-width: 54rem;
max-height: min(58vh, 540px);
```

The frame, not the replaced video element, owns the aspect ratio. The 1280×710
MCP clip may letterbox by a few pixels but is never stretched or cropped.

### 3. Three compact highlights

Below the hero, replace the contract map, write path, and evidence mosaic with
three linked cards:

- **SEE** — live resource tables, logs, and relationships;
- **GROUND** — bounded Agent/MCP reads and observability evidence;
- **CONTROL** — previews, a fresh approval keystroke, and fail-closed audit.

Each card has one sentence and a short chip list. The cards explain the product
contract once without replaying the videos in prose.

### 4. Keep only destination navigation

The final navigation remains, reduced to three destinations:

- Start operating;
- Explore Agent and MCP;
- Evaluate production use.

## Removed from the homepage

- duplicate Direct video;
- scene Input / Evidence / Result rows;
- contract-map section;
- five-stage write-path section;
- six-card evidence mosaic.

The underlying images and recordings remain in the repository and on their
focused guide pages. No binary media is removed or regenerated.

## Responsive and fallback behavior

- Desktop keeps a headline/copy column and one bounded media column.
- Tablet and mobile stack copy above media; the stage is width-bound and never
  exceeds the viewport.
- Tabs remain keyboard operable.
- Reduced motion still disables programmatic playback.
- Without JavaScript, authored media remains accessible and focused guide links
  provide every destination; implementation tests define the exact fallback.
- A real media failure reports an error and uses an available poster fallback.

## Acceptance

- Homepage has no more than three major content blocks after front matter:
  combined hero/switcher, highlights, destination navigation.
- Exactly three videos are authored; the Direct source appears once.
- Only the selected video displays during normal enhanced operation.
- No video is stretched or cropped; the media stage is capped at 540px and 58vh.
- Homepage source stays below 800 words.
- SEE / GROUND / CONTROL retain keyboard-first operation, bounded evidence,
  fresh approval, and fail-closed audit claims.
- Landing, Agent, and MCP media files remain byte-identical.
- Existing playback, reduced-motion, keyboard, fallback, link, and strict-build
  tests pass after their contracts are updated to the compact structure.
