---
name: videos
description: Video production board and entry point for any project. With no argument it shows every video in progress (stage × status) and the next move for each; with a video name it opens that video's record, says where it stands and runs or proposes the next stage (/videos:new, ingest, transcribe, rough, tighten, audio, grade, master, formats, clips, qc, publish). Use when the user types /videos, names a video to continue, asks what video work is open or where a video stands, or makes any video request that doesn't name a stage.
argument-hint: "[video name or slug]"
---

`videos.py` below = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"` (stdlib only; `python` / `python3` also work). Record format, folders and status words: [records](${CLAUDE_PLUGIN_ROOT}/references/records.md).

## Do

1. No argument → `videos.py board` (`--ws <W>` one workspace, `--wide` every stage column, `--all` include published). Show it, name the next move per active video, stop. "No video records" → offer `/videos:new`; `videos.py where` shows which folder was searched.
2. A name → `videos.py show <query>` (exact slug, else fuzzy on slug and title; several hits → list them and ask). Read the record it prints: frontmatter = state, body = decisions, notes, log. Read the files listed in `style:` before any stage that cuts or styles.
3. Pick the stage: the user's verb (« tighten X » → tighten), else the first stage whose status is not `done`/`skip`. Status `review …` = waiting on a human: show what to review (timeline, markers, page) and stop, unless the user is giving that review now → record it under `## Decisions`, then continue.
4. Load only that stage's skill (table) and the skills or docs it names; run it.
5. After each stage: `videos.py set <slug> <stage> "<status> <date or what to look at>" --next "<next step>"` (master, formats, clips, generate, qc and publish need video CI first: next section). Doubts, skipped items and things to check go in `videos.py note <slug> "<text>"`. Never leave the record stale: the next session starts from it.

## Stages

| Stage | Skill | Covers |
|---|---|---|
| brief | `/videos:new` | create the record, kind, campaign / content-hub link, sources, style files |
| generate | your generation pipeline | AI video (kind `generative`): scenes → storyboard → references → voices → keyframes → animatic → shot clips; no skill in this plugin yet, paid models need a go-ahead |
| ingest | `/videos:ingest` | probe sources, sync offsets, camera angles, NLE import, multicam |
| transcribe | `/videos:transcribe` | word-level transcript with fillers kept, caption text |
| rough | `/videos:rough` | text anchors → cut list → timeline + markers |
| tighten | `/videos:tighten` | breaths, fillers, dead air, tight pauses, J-cuts, phrase trims, camera switches |
| audio | `/videos:audio` | speech cleanup, speaker split, −14 LUFS master (→ `audio-cleanup` plugin) |
| grade | `/videos:grade` | one look per set, LUT export |
| master | `/videos:master` | render from the NLE, read the cuts back, check the file |
| formats | `/videos:formats` | graphics, captions, hook, end card as alpha layers in the NLE; 9:16 · 4:3 · 16:9 |
| clips | `/videos:clips` | vertical clips cut from a long video or podcast (→ `vertical-clips` plugin) |
| qc | `/videos:qc` | one gate before anything ships |
| publish | `/videos:publish` | finals into `artifacts/videos/<slug>/`, post text, covers, schedule |

Not stages: `/videos:resolve` = rules and failure log for driving DaVinci Resolve through its MCP, used by every stage that touches Resolve; `/videos:learn` = folds past mistakes (record lessons + past agent sessions) back into the skills or the project's notes.

Companion plugins from the same marketplace: `audio-cleanup`, `vertical-clips`, `agent-ears` (breath / filler detection used by tighten). Missing → `/plugin install <name>@les-bunkers`.

## Video CI (mandatory)

Nothing reaches a human's eyes or a platform before video CI: before a review page, a contact sheet, a file path in chat, a status `review`, or a delivery, run `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" <files> --slug <video> --stage <stage> [--var hook="…" --var cta="…"]` on every file you are about to show.

- RED: do not show it. Fix the cause, re-render, re-run. Can't fix → tell the human what failed, with the frames, and set the stage `blocked <what>`.
- AMBER: show it with the flagged frames (`checks/ci/<stage>/<file>_frames/`) and one line per flag. Never call an amber file clean.
- GREEN: show it. CI covers the mechanical and visual defects; taste, claims and pacing stay with the human.
- `videos.py set` refuses `review` / `done` on master, formats, clips, generate, qc and publish unless the stage's CI index is fresh (files unchanged since their run) and has no red file. A dropped file leaves the index with `video_ci.py <file> --slug <video> --stage <stage> --forget`; `--skip-ci "<why>"` passes the gate only when the human asked for the file as it is, and the reason is logged.
- Cost: the tech tier is local and free; the visual tier is a paid API call (~$0.005 per short, at most $0.50 per file). Ask the human once per project for a standing go-ahead, record it in the project's style or house notes, then run CI without asking. Without it: `--no-visual "no go-ahead yet"` (the file is at best amber).

Profiles, checks, thresholds, where each stage runs it: [ci](${CLAUDE_PLUGIN_ROOT}/references/ci.md).

## House rules (every stage)

- Taste lives in the project: a stage that cuts or styles reads the record's `style:` files (edit style, caption look, brand). None → ask once, write the answer into a style file and add it to `style:`.
- Preview before full builds: one video, clip or look approved before a batch. Gates = status `review` until the human says go.
- Mistakes are lessons: when the human corrects or rejects something, a call fails or silently does nothing, or work has to be redone, log it once fixed: `videos.py lesson <slug> <stage> "<tried> → <what happened> → <do instead>"`. `/videos:learn` folds them into the skills; after a video ships, or when `videos.py lessons` lists several, offer it.
- Paid calls (speech-to-text APIs, generation, dubbing) need a go-ahead per job; cache every result next to the job and never request it twice.
- Local GPU generation (video or image models running on your own card) costs nothing per call but holds the GPU and a lot of RAM: run only what was asked and say the expected minutes first.
- Never cut voice the transcript lacks: words a speech-to-text model missed are listed for the human, not cut.
- Grade per set: a look never crosses shoots or sets (lighting differs).
- One render chain at a time; check free RAM first (< 4 GB → wait). Background jobs longer than ~10 min get a periodic check that they are still moving.
- NLE timelines: before rebuilding one, compare it with the last build (hand edits → ask); build new timelines beside old ones, never delete the human's.
- DaVinci Resolve: load `/videos:resolve` before the first MCP call of a session. A timed-out script keeps running, one Resolve is shared with the human and other sessions, and many calls fail silently; its log ([resolve](${CLAUDE_PLUGIN_ROOT}/references/resolve.md)) lists every failed attempt with what to do instead.
- Camera changes on a multicam edit = the multicam clip's active angle, never another camera's clip on a track above it (Resolve: `scripts/resolve_multicam_angles.py`).
- Graphics are layers; compositing happens in the NLE. Anything drawn over footage (motion graphics, cards, labels, highlight boxes, hook and end cards, designed captions) renders alone with alpha (HyperFrames `--format mov` = ProRes 4444 + alpha, transparent root), on a canvas the size of the target timeline with the element at its default place (Resolve scales a smaller clip up to fit the frame), and goes on its own named track above the picture, where the human can move, scale, trim or drop it without a re-render. Changes to the footage itself (zooms, punch-ins, reframes, blurs and redactions, PiP of another camera, transitions, grade) are made on the clips in the NLE, never baked into a rendered copy of the cut. Bake graphics into one rendered video only for motion-only pieces with no footage under them, or a hands-off batch the human asked for. [layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md)
- Review pages for the human run on localhost (`python -m http.server --bind 127.0.0.1 <port>`); stop servers by their PID.
- Never modify source media. Heavy media (rushes, masters, renders, HyperFrames projects) goes under `media_root`; the project keeps records, cut data, reports and upload-ready finals.
- Before publishing, check the picture for readable secrets (passwords, Wi-Fi keys, emails, phone numbers, client screens): video CI scans finals for them, `/videos:qc` has the procedure for screen recordings.
