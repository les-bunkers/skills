---
name: tighten
description: Tighten stage of a talking-head video in the editor's own style — breaths, fillers (« euh », "um") and dead air out, a spare sentence-initial « donc » out where it cuts cleanly, phrase trims (false starts, repeats, filler phrases) proposed by an agent and cut only on clean edges, tight pauses, J-cuts, camera switches, join check, then the tight timeline in DaVinci Resolve with review markers. Use when tightening a rough cut or making it more aggressive, removing breaths, fillers or false starts, trimming phrases, adding J-cuts or camera switches, rebuilding a tight timeline, or when running /videos:tighten.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Work from the record folder (`videos.py show <video>` → `record:`). Detector, style values and their measurements, report fields: [engine](${CLAUDE_PLUGIN_ROOT}/references/engine.md).

## Before

- Gate: rough `done` (the human approved what is kept).
- Taste: read the record's `style:` files. Defaults = the house style (`uv run "$S/tighten.py" --dump-style`): pause ~0.12 s across a join, dead air > 0.4 s out, J-cut 2 frames where the held picture stays silent, French filler and « donc » rules. Another editor's taste → copy the dump to `cut/style.json`, change values, set `"style": "style.json"` in edit.json. Language-specific lists (fillers, spare words) sit under `languages`.
- Inputs per source in edit.json: `ears` + `fillers` from `/agent-ears` (breaths, timed fillers; see `/videos:transcribe`), `scan` from ingest. Missing ones only weaken the pass; tighten warns.
- Several videos to tighten: the first one is the preview. Its review sets pace, phrase aggressiveness and camera frequency before any other gets built (prep for the others is fine).

## Do

1. Cameras, if the source has angles: `python "$S/angle_sheet.py" --project cut/edit.json --cuts cut/rough.cuts.json` → look at `cut/scans/sheet_<id>.jpg`. A camera showing green screen, an empty seat or the wrong instant (offset) = fix edit.json (drop that angle, re-sync) before trusting the planner.
2. Words file: `python "$S/words_dump.py" --project cut/edit.json --cuts cut/rough.cuts.json` → `cut/phrase-trims/words.txt` (`id:word`, `/ ` = pause ≥ 0.4 s, `[x]` = dropped anyway).
3. Phrase-trim agent: one subagent per video, brief (in the speech's language; French example):
   > You are a video editor working in spoken Québec French. Read `cut/phrase-trims/words.txt`. Propose cuts that keep the meaning and sound natural: false starts, restarts, repeats, filler phrases (tsé, genre, en fait, faque, pis là, bon, OK?), redundant restatements, empty transitions. Never cut names, steps, numbers, punchlines, or anything the picture depends on. One span = contiguous words inside one `## seg`. Write `cut/phrase-trims/spans.json` = `[{"seg", "first", "last", "kind", "conf": "high|med|low", "why"}]`.
   English speech: filler phrases you know, like, I mean, kind of, sort of, so yeah. A 21-min test video got 196 proposals.
4. `python "$S/phrase_trims.py" --words cut/phrase-trims/words.txt --spans cut/phrase-trims/spans.json` → read every `-removed-  ::  before ‖ after` line as it would sound. Reject by judgment with `"skip": true` in spans.json (the test skipped 3: a clearer word was the one cut, broken grammar, the picture needed it). Then `--apply --overrides cut/overrides.json` (keeps a backup).
5. Tighten: `uv run "$S/tighten.py" --project cut/edit.json --cuts cut/rough.cuts.json --out cut/tight` → `cut/tight.cuts.json`, `cut/tight.srt` (captions minus removed words), `cut/tight.report.md`. ~10 s per 20 min of cut. Read the report: every cut with its timeline time and the words around it; spare-word table with ids; phrase trims cut or kept (no clean edge); timed fillers cut or why kept; "Kept: voice where the transcript has no words" = listen, maybe real words.
6. Adjust and rerun 5: `overrides.json` `keep_spare` (a « donc » that means "so": keep it), `drop_spare`, or drop a phrase span. A switch: `--no-glide` (never cut inside the voice), `--no-cameras`, `--no-ears`, `--no-fillers`.
7. Join check: `uv run "$S/join_check.py" --project cut/edit.json --cuts cut/tight.cuts.json --json checks/joins.json`. Pass = no join over 35 dB above the floor (speech ≈ 55; the test cut: p50 8 / 7, max 31). Fail → find the join in the report by its timecode, drop or keep that span / spare word, rerun 5. Joins cut inside the voice are listed apart (loud by design, cross-faded).
8. Resolve (MCP `run_script_unsafe`, one call per step; print each call with `--snippet`):
   - Rebuilding a timeline the human may have touched: `python "$S/resolve_build.py" --project cut/edit.json --cuts <last built cut list> --name "<timeline>" --mode inspect --snippet` → `same_as_cut_list: false` = hand edits → ask before replacing.
   - `python "$S/resolve_build.py" --project cut/edit.json --cuts cut/tight.cuts.json --name "<title> — tight v1" --dry-run`, then `--snippet` → run. Result: `V1`/`A1` = parts, `V2` = face parts, `V3` = camera pieces, `glide.xfade` = `glide.joins`, `glide.fail` empty (sub-frame audio needs Resolve's preference "Align audio edits to frame boundaries" OFF). The build restores the open timeline and page.
   - Review markers: same command with `--mode review_markers --snippet` → run.
9. `videos.py set <slug> tighten "review <human>: <timeline>, Fuchsia Sand Sky first, then Pink Mint Cyan, Lavender = J" --next "human listens to the review markers"`. In the record: length before → after, cuts by kind, phrase trims cut / proposed, J-cuts, camera pieces, join check max.

## Review markers

Fuchsia phrase trim on a soft dip (listen) · Sand timed filler cut on a soft dip (listen) · Sky filler cut inside the voice with a 1-frame cross fade (listen) · Pink phrase trim (check the meaning) · Mint timed filler · Cyan breath glued to a word · Lavender J-cut (sound before picture). V3 pieces = camera switches: delete one to fall back to the program / face angle.

## Gate

Status `review` = wait for the human. Their notes → `## Decisions`, then overrides / style, rerun 5–8 under a new version name. `done` when approved; `/videos:audio` and `/videos:grade` work on that timeline, `/videos:master` renders it.

## Rules

- Never cut voice the transcript lacks (`speech?`): it is listed, not cut.
- Phrase spans and spare words are cut only where both edges come off cleanly; the report lists the rest as kept.
- Never delete the human's timelines: new versions beside the old ones.
