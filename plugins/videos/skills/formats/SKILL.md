---
name: formats
description: Formats stage — turns a master into the shipped formats (9:16, 4:3, 16:9) with word-timed captions, a hook card, motion graphics and an end card, one HyperFrames project per format, then renders and solves loudness per file. Use when cropping a master to vertical/square/wide, adding captions, hooks, end cards or motion graphics to a video, building an offer video or bumper, or running /videos:formats.
argument-hint: "[video name] [formats]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Engine: HyperFrames (HTML → video; `npm install -g hyperframes`, then its own skills: `/hyperframes` — read captions, overlays, vertical and format guides first, motion and review guides before any motion). Brand and look = the record's `style:` files; none → ask for the brand doc once and add it to `style:`.

## Feature short (talking head + captions + hook + end card)

1. Inputs: the master from `/videos:master`, the Words file of the master (shift the source transcript through the cut list, or transcribe the master itself), hook text, CTA line + URL, end-card length. Copy (hook, on-screen text, CTA) comes from the content-hub item when the record links one.
2. One HyperFrames project folder per format, one root per folder: `<media>/formats/<fmt>/index.html`.
   - 9:16 from a 4K 16:9 master: crop 1215×2160 → 1080×1920; offset `crop_x` when the face sits off-center.
   - 4:3: 2880×2160 → 1440×1080. 16:9: the master as is.
   - Captions: phrase groups from the Words file (never end a line on an article, preposition or pronoun; lone words merge forward), one fixed caption band per format (text never jumps between shots), active word highlighted; keep text inside the platform safe area (Reels/Shorts UI: y 168–1330 on 1920).
   - Hook: legible at frame 0 (it is the thumbnail), holds ~2.8 s, two lines max; captions start after it.
3. Per format: `hyperframes check --json > ../check.json 2>/dev/null` (`ok: true`; unread-line warnings on speech captions are expected) → `hyperframes render . --output ../renders/<slug>_<fmt>.mp4 --quiet`. One render chain at a time (≈ 25 s per 13 s of video per format on a fast GPU machine).
4. Loudness on every render that ships: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/loudness.py" fix ../renders/<slug>_<fmt>.mp4 ../final/<slug>_<fmt>.mp4 --json ../final/loudness.json`.
5. Contact sheet per format (`ffmpeg -i <final> -vf "fps=1/2,scale=270:-1,tile=6x4" -frames:v 1 sheet.jpg`) and look at it: hook legible, no caption on a face, nothing under platform UI.
6. Video CI on every format before anyone sees it: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" ../final/<slug>_*.mp4 --slug <slug> --stage formats --var hook="<hook text>" --var cta="<closing text>"` (~5 s and ~$0.005 per format). RED → fix and re-render; AMBER → show the flagged frames with the review. After a re-render the same command is the regression test (unchanged frames come from cache for free). [ci](${CLAUDE_PLUGIN_ROOT}/references/ci.md).

## Motion and offer graphics

- Motion shows the real thing (a map, real reviews, the actual UI, real data), not text on cards.
- Build graphics as HyperFrames compositions or overlays timed to words in the Words file (cue on words, not guessed frames), rendered with alpha (ProRes 4444 `.mov`) when they go back into the NLE.
- 3D objects: render sprites headless in Blender, place them as images.
- Check vertical centering of text inside chips and pills on a rendered frame: display fonts often sit several pixels low.

## Gate and record

First short of a batch = preview: `videos.py set <slug> formats "review <human>: <renders folder or review page>"` (refused without fresh, non-red CI on the formats); batch the rest only after the go. Done → `formats "done <date> 9x16 16x9 4x3"`, then `/videos:qc`. Many verticals cut from long podcasts → `/videos:clips`.
