---
name: formats
description: Formats stage — graphics, captions, hook and end cards rendered as transparent (alpha) layers and composited on the NLE timeline by default, the 16:9 / 9:16 / 4:3 versions rendered from the NLE, loudness and video CI per file; a baked HyperFrames route for hands-off batches. Use when adding motion graphics, overlays, captions, hooks or end cards to a video, rendering graphics with transparency for Resolve, cropping a master to vertical/square/wide, building an offer video or bumper, or running /videos:formats.
argument-hint: "[video name] [formats]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Engine: HyperFrames (HTML → video; `npm install -g hyperframes`, then its own skills: `/hyperframes` — read captions, overlays, vertical and format guides first, motion and review guides before any motion). Brand and look = the record's `style:` files; none → ask for the brand doc once and add it to `style:`.

## Graphics on an edit: alpha layers in the NLE (default)

Everything drawn over footage is a layer: rendered alone with a transparent background, placed on its own named track above the picture, composited by the NLE. The human can move, scale, trim or drop any layer without a re-render, and their cut, angles and reframes stay untouched. Zooms, blurs, PiP of another camera and transitions are made on the clips in the NLE, never baked. Render, placement and alpha details: [layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md).

1. Inputs: the approved edit timeline (record `resolve:`), the Words file of the cut (source transcript shifted through the cuts read back from the timeline, `/videos:master` step 2), copy from the content-hub item when the record links one.
2. Plan the layers: one per idea, cued on the words it illustrates → `<media>/layers/layers.json` (id, record frame, frames, track, anchor words, note). A run of cards on one topic (a list, prices, steps) can become a split section, the speaker moved aside and the cards in a side panel: [layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md) § Split section. Claims check on every text first: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/claims_check.py" <spec or project dir> --slug <slug> --words <cut words>`.
3. Build each layer as a HyperFrames composition with a transparent root on a canvas of the timeline's aspect, the element at its default place; `hyperframes check --at-transitions --json`; render `--format mov --resolution <preset matching the timeline>` (ProRes 4444 + alpha) to `<media>/layers/renders/`. First layer = preview: one built, placed and approved before the batch.
4. Place them on the edit timeline on a track named for the layers (V1 / A1 untouched, one marker per layer). Switching the current timeline under a human who is editing → ask first, switch back after.
5. Look at a composite still per layer (edges, position, nothing over a face or the caption band), then `videos.py set <slug> formats "doing layers on <timeline> track <name>, <human> adjusting in the NLE"` (`doing`: no render exists yet for CI to test; `review` comes with the CI'd renders of step 8) and let the human adjust in the NLE.
6. Formats: 16:9 = the master rendered from the NLE with the layers track on (`/videos:master`). 9:16 / 4:3 = a timeline per format in the NLE (the cut reframed per clip, format-specific layers authored at that format's size, captions inside the platform safe area), rendered from the NLE.
7. Captions: the NLE's subtitle track when a plain look is enough; a designed word-highlight look = one caption layer per format rendered with alpha (rules in the baked route step 2), rebuilt when the cut moves.
8. Loudness and video CI on every NLE render that ships: steps 4–6 of the baked route below, with `--input <layers.json>` so CI refuses a final once the layers changed.

## Baked route: one HyperFrames render per format (fallback)

Only for motion-only pieces with no footage under them (bumper, motion beat) or a hands-off batch the human asked for (many shorts, ad variants of an approved baked short).

1. Inputs: the master from `/videos:master`, the Words file of the master (shift the source transcript through the cut list, or transcribe the master itself), hook text, CTA line + URL, end-card length. Copy (hook, on-screen text, CTA) comes from the content-hub item when the record links one.
2. One HyperFrames project folder per format, one root per folder: `<media>/formats/<fmt>/index.html`.
   - 9:16 from a 4K 16:9 master: crop 1215×2160 → 1080×1920; offset `crop_x` when the face sits off-center.
   - 4:3: 2880×2160 → 1440×1080. 16:9: the master as is.
   - Captions: phrase groups from the Words file (never end a line on an article, preposition or pronoun; lone words merge forward), one fixed caption band per format (text never jumps between shots), active word highlighted; keep text inside the platform safe area (Reels/Shorts UI: y 168–1330 on 1920).
   - Hook: legible at frame 0 (it is the thumbnail), holds ~2.8 s, two lines max; captions start after it.
   Claims, before rendering: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/claims_check.py" <media>/formats/<fmt> --slug <slug> --captions cap-line --words <master words>` (+ `--text` for each variant hook). Red (a price, %, multiplier or banned phrase nobody approved) → change the text or get it approved; amber → show the line with the review. [claims](${CLAUDE_PLUGIN_ROOT}/references/claims.md)
3. Per format: `hyperframes check --at-transitions --json > ../check.json 2>/dev/null` (`ok: true`; unread-line warnings on speech captions are expected; `--at-transitions` also samples every tween start and end, where a label that has not left yet overlaps the next one). Motion-only pieces (bumper, offer video, motion B-roll): `hyperframes snapshot --review` too, and read `review-seam-NN.jpg` (−0.2 to +0.2 s around every cut) and `review-phone.jpg` (360 px wide) before rendering; skip it on word-timed captions, where every caption change counts as a cut (126 s teaching video: 977 frames, 165 seam sheets, 1.4 GB, ~12 min) and video CI covers the render → `hyperframes render . --output ../renders/<slug>_<fmt>.mp4 --quiet`. One render chain at a time (≈ 25 s per 13 s of video per format on a fast GPU machine).
4. Loudness on every render that ships: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/loudness.py" fix ../renders/<slug>_<fmt>.mp4 ../final/<slug>_<fmt>.mp4 --json ../final/loudness.json`.
5. Contact sheet per format (`ffmpeg -i <final> -vf "fps=1/2,scale=270:-1,tile=6x4" -frames:v 1 sheet.jpg`) and look at it: hook legible, no caption on a face, nothing under platform UI.
6. Video CI on every format before anyone sees it: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" ../final/<slug>_*.mp4 --slug <slug> --stage formats --var hook="<hook text>" --var cta="<closing text>" --events <graphics spec> --input <spec or row file> --input <format project dirs>` (~30 s and ~$0.01 per short format; `--events` = graphic in/out times so text collisions over a static shot are sampled too; `--input` = what the render was built from, so the gate refuses a final once its spec or project changed). RED → fix and re-render; AMBER → show the flagged frames with the review. After a re-render the same command is the regression test (unchanged frames come from cache for free). [ci](${CLAUDE_PLUGIN_ROOT}/references/ci.md).

## Motion and offer graphics

- Motion shows the real thing (a map, real reviews, the actual UI, real data), not text on cards.
- Springs are closed-form functions of time (`1 - e^(-ζωt)(cos ω_d t + k sin ω_d t)` on normalized time), never a simulation stepped frame by frame: a seekable render draws frame 600 without frames 0–599.
- Split edges: a moving indicator, underline or chip springs its leading edge first and its trailing edge ~0.07 s later on a stiffer spring, so it stretches in flight and settles at its new width. A rigid box sliding at constant speed reads as generated.
- Container first, text second: the old label is gone by the time its box starts moving; the new label enters once the box is ~70 % of the way. Two labels readable in one box at once is the overlap video CI flags (`text_collision`).
- Build graphics as HyperFrames compositions timed to words in the Words file (cue on words, not guessed frames), rendered with alpha (ProRes 4444 `.mov`) as layers for the NLE unless the piece is baked ([layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md)). A transparent layer can't blur the footage under it: panels ~.9 opaque + soft shadow, no `backdrop-filter`.
- Music-led pieces only (bumper, offer video, hook card, motion-only piece; voice-led videos keep cueing on words): map the bed once, `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/beats.py" analyze <bed> --out <media>/formats/music/<bed>.beats.json` (bpm, beats, downbeats, hits, sections; 2–5 s per track, ~20 s on first run). Write cue times in the graphics spec, then land them: `beats.py snap <bed>.beats.json --json <spec> --path <list.path.*.cues> --key t --grid downbeat,beat --fps <fps> [--offset <timeline position of music 0>]` → a new `<spec>.snapped.json`. Snap the frame where a graphic lands (impact, final counter number, cut), not the start of its ease-in; big entrances on downbeats, counters and chips on beats, accents `--grid hit --min-strength 0.8`. Hear it first: `beats.py click <bed> <bed>.beats.json <bed>.click.wav`. [beats](${CLAUDE_PLUGIN_ROOT}/references/beats.md)
- 3D objects: render sprites headless in Blender, place them as images.
- Check vertical centering of text inside chips and pills on a rendered frame: display fonts often sit several pixels low.

## Variants (ads A/B)

An approved short can ship 2–3 opening hooks (and CTAs) without a rebuild: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/variants.py" <media>/formats/<fmt> variants.json --out <media>/variants/<fmt> --audio-from <media>/final/<slug>_<fmt>.mp4`. Only after the human approved the base short (preview before batches).

- variants.json: `{"slots": {"hook": {"selector": "#hook-line"}, "cta": {"selector": "#cta-line"}}, "variants": [{"id": "h2", "hook": "<inner HTML>"}]}`; one slot: `--selector "#hook-line" --set h2="..."`. Write each value with the builder's own markup (same spans and classes) so only the words change, in the approved hook's length class.
- Per variant: hard-linked clone of the project, the slot's inner HTML rewritten, `check --at-transitions` compared with the same check of the unchanged project (only new findings count; new errors block the render: a longer hook overflowed its box in testing; `check` stops at 80 findings by default, variants.py raises the cap), a full render (~1.6 s per second of 9:16 on a fast GPU machine), the base final's audio stream-copied after a one-frame match (no loudness pass needed). Run `--check-only` first when a hook is longer than the approved one.
- Why clones and not HyperFrames `--variables` / `--batch`: `check` takes no variables, so a variant's text can only be checked once it is in the HTML; `data-var-text` sets plain text (drops `<br>` and styled spans); a batch row is still a full render.
- Then CI per variant with the plain text from `<out>/variants.json` (`ci_vars`): `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" <out>/<slug>_<fmt>_h2.mp4 --slug <slug> --stage formats --var hook="<text>" --input variants.json`.
- Variant renders are raw (no size cap): fine for ad platforms; cap before uploading to a host with a file limit.

## Gate and record

Layers route: `doing` while the human adjusts layers in the NLE, `review` once the NLE renders passed CI. First short of a baked batch = preview: `videos.py set <slug> formats "review <human>: <renders folder or review page>"` (refused without fresh, non-red CI on the formats); batch the rest only after the go. Done → `formats "done <date> 9x16 16x9 4x3"`, then `/videos:qc`. Many verticals cut from long podcasts → `/videos:clips`.
