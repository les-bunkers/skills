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
5. After each stage: `videos.py set <slug> <stage> "<status> <date or what to look at>" --next "<next step>"`. Doubts, skipped items and things to check go in `videos.py note <slug> "<text>"`. Never leave the record stale: the next session starts from it.

## Stages

| Stage | Skill | Covers |
|---|---|---|
| brief | `/videos:new` | create the record, kind, campaign / content-hub link, sources, style files |
| ingest | `/videos:ingest` | probe sources, sync offsets, camera angles, NLE import, multicam |
| transcribe | `/videos:transcribe` | word-level transcript with fillers kept, caption text |
| rough | `/videos:rough` | text anchors → cut list → timeline + markers |
| tighten | `/videos:tighten` | breaths, fillers, dead air, tight pauses, J-cuts, phrase trims, camera switches |
| audio | `/videos:audio` | speech cleanup, speaker split, −14 LUFS master (→ `audio-cleanup` plugin) |
| grade | `/videos:grade` | one look per set, LUT export |
| master | `/videos:master` | render from the NLE, read the cuts back, check the file |
| formats | `/videos:formats` | 9:16 · 4:3 · 16:9, captions, hook, graphics, end card |
| clips | `/videos:clips` | vertical clips cut from a long video or podcast (→ `vertical-clips` plugin) |
| qc | `/videos:qc` | one gate before anything ships |
| publish | `/videos:publish` | finals into `artifacts/videos/<slug>/`, post text, covers, schedule |

Companion plugins from the same marketplace: `audio-cleanup`, `vertical-clips`, `agent-ears` (breath / filler detection used by tighten). Missing → `/plugin install <name>@les-bunkers`.

## House rules (every stage)

- Taste lives in the project: a stage that cuts or styles reads the record's `style:` files (edit style, caption look, brand). None → ask once, write the answer into a style file and add it to `style:`.
- Preview before full builds: one video, clip or look approved before a batch. Gates = status `review` until the human says go.
- Paid calls (speech-to-text APIs, generation, dubbing) need a go-ahead per job; cache every result next to the job and never request it twice.
- Never cut voice the transcript lacks: words a speech-to-text model missed are listed for the human, not cut.
- Grade per set: a look never crosses shoots or sets (lighting differs).
- One render chain at a time; check free RAM first (< 4 GB → wait). Background jobs longer than ~10 min get a periodic check that they are still moving.
- NLE timelines: before rebuilding one, compare it with the last build (hand edits → ask); build new timelines beside old ones, never delete the human's.
- Review pages for the human run on localhost (`python -m http.server --bind 127.0.0.1 <port>`); stop servers by their PID.
- Never modify source media. Heavy media (rushes, masters, renders, HyperFrames projects) goes under `media_root`; the project keeps records, cut data, reports and upload-ready finals.
- Before publishing, check the picture for readable secrets (passwords, Wi-Fi keys, emails, phone numbers, client screens): `/videos:qc` has the procedure.
