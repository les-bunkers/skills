---
name: wide
description: Wide stage of a talking-head video — make the 16:9 version from the approved long 9:16 in DaVinci Resolve: same cut, sound, camera angles and grade, framing redone for the wide frame, 9:16 stacked layouts turned into their side-by-side counterparts, 16:9 layers on the same cues, then render, video CI, review and approval. Runs side by side with the clips stage once the 9:16 is approved. Use when building, rebuilding or reviewing the 16:9 / landscape / YouTube version of a video that was cut vertical first, carrying a 9:16 change over to the 16:9, or when running /videos:wide.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Work from the record folder (`videos.py show <video>` → `record:`). Resolve rules for every call: `/videos:resolve`; the steps below in detail, with what to read back: [resolve](${CLAUDE_PLUGIN_ROOT}/references/resolve.md) § Timelines → Another format from the approved 9x16. Not yet run end to end: read every step back, and log what fails in that section.

## Before

- Gate: master `done` = the human approved the whole `<Video> 9x16` and a copy sits in `01 Timelines/Published` (`<Video> 9x16 vNN <date> approved`; older projects: `Final`). Nothing here changes the approved 9x16.
- Read the record's `style:` files for the wide framing (where the face sits, punch-ins) and the 16:9 layouts the project uses.
- clips may run at the same time on the same approved 9x16: one Resolve writer at a time (`/videos:resolve` § Sharing Resolve).

## Do

1. The timeline: `<Video> 16x9` in `01 Timelines/WIP/Processing` while you work on it, made from the approved 9x16, tagged `B.timeline_words("<Video> 16x9")` + `timeline`, `latest`.
   - Route A, default (keeps the human's hand work: angles, J-cuts, fades, extra audio tracks, clip grades): `DuplicateTimeline` of `<Video> 9x16` → `B.move` to `B.PROCESSING` → set the 16:9 size (the project's own 16:9 size, or 3840×2160) and read it back with `B.fmt_of(tl, project) == "16x9"` → restore the current timeline.
   - Route B (route A's size change returns False or reads back wrong): `python "$S/resolve_build.py" --project cut/edit.json --mode read_cuts --name "<Video> 9x16" --out cut/approved.cuts.json --snippet` → run, then `--cuts cut/approved.cuts.json --format 16x9 --dry-run` / `--snippet` → run (the name becomes `<Video> 16x9`). Place the mastered voice track at frame 0 (same cut, same sound: it lines up), re-apply the grade from the project's LUTs, then compare every hand edit of the 9x16 that a cut list does not hold (fades, extra tracks, nudged angles) and redo it.
2. Framing: reset the vertical framing on every picture item of the cut (Pan / Tilt 0, Zoom 1.0) or apply the project's wide framing rule; `get_thumbnails` across every camera. Stacked 9:16 layouts (two or three panels one above the other) become their 16:9 counterpart (side by side) on the same frames, hard cuts on the same frames as the 9x16.
3. Layers: switch off the 9x16 layers tracks (never delete them), then the 16:9 layers on their own tracks: same cues from `layers.json` (record frames and anchor words), drawn for the wide frame at 1920×1080 and rendered `landscape-4k` for a 3840×2160 timeline, into `<media>/layers/16x9/renders/`, filed in `03 Assets/Videos/<Video>/16x9` (`/videos:formats` steps 3–5, [layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md)). Captions: the subtitle track restyled for 16:9, or the 16:9 caption layer. A split section ([layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md) § Split section) is a 16:9 layout: offer it where the 9x16 shows a run of cards.
4. `B.audit(project)` → `ok`; `format_not_resolution` empty.
5. Render and check with `/videos:master` steps 2–6 on `<Video> 16x9` (`--suffix -16x9`; read the cuts back: they must equal the approved 9x16's, `unknown` empty), video CI `--stage wide`.
6. `B.release(resolve, project, "<Video> 16x9")` → `Waiting for review`, `videos.py set <slug> wide "review <human>: <Video> 16x9, <render path>"`. Approved → `B.snapshot(resolve, project, "<Video> 16x9", "approved", to=B.PUBLISHED)`, `B.release(…, review=False)` and `B.file_export(resolve, project, <render path>, "<Video> 16x9")` → `04 Exports/<Video>`, then `videos.py set <slug> wide "done <date> <path> -14.0/-1.9"`.

## Rules

- Same words, same order, same length as the approved 9x16: wide reframes and redraws, it never re-edits. A cut the human wants changed while reviewing the 16x9 goes back to the 9x16 first (`/videos:master` rules), then comes here again.
- Never delete or edit the 9x16 layers tracks or the approved 9x16; turn tracks off on the 16x9 copy.
- The mastered voice serves every format: no new audio pass unless the cut changed.
