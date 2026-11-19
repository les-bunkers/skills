---
name: rough
description: Rough cut stage of a video — choose what to keep from the transcript as text anchors, snap them to the words, produce a cut list with retimed captions and colour markers, print every seam, and build the rough timeline in DaVinci Resolve (multicam aware). Use when choosing takes, building or rebuilding a rough cut or selects timeline, changing what a cut keeps, reading the seams of a cut list, or when running /videos:rough.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Work from the record folder (`videos.py show <video>` → `record:`). Anchor format, markers and cut-list fields: [engine](${CLAUDE_PLUGIN_ROOT}/references/engine.md).

## Do

1. Gate: ingest and transcribe `done`. Read the record's brief, decisions and `style:` files, then the whole transcript (`python "$S/words_dump.py" --project cut/edit.json --src A [--src B]`).
2. Decide keep ranges with the brief: the best take of each idea, false starts and retakes out, asides and private remarks out, claims to verify flagged. Write `cut/rough.json`: `timeline` (the video's plain name, "<title>": no stage, version or date in it), `segments` as `[src, [approx s, "phrase"], [approx s, "phrase"], {options}]` (times as the dump prints them; `*` = any one misheard word; `face: true` on hooks/outros where the program shows a screen but the speaker talks to camera; `alt` = alternate take parked after the end), `markers`, `end_note` (what was cut and why).
3. `python "$S/rough.py" --project cut/edit.json --spec cut/rough.json --check` → fix every failing anchor (the error shows the words near it).
4. `python "$S/rough.py" --project cut/edit.json --spec cut/rough.json --out cut/rough` → `cut/rough.cuts.json` + `cut/rough.srt`. Read EVERY printed seam `…cut before || kept … || cut after…`: a seam that cuts mid-thought or repeats a word is a wrong anchor.
5. Resolve (rules for every call: `/videos:resolve`): `python "$S/resolve_build.py" --project cut/edit.json --cuts cut/rough.cuts.json --dry-run` (tracks, counts, first items), then `--snippet` and run the printed code with the Resolve MCP tool `run_script_unsafe`. V1 = multicam or the program file, sound linked; `face` segments = the multicam clip's active angle switched to the face camera (the build places the pieces, then switches V1's angles through a .drt round trip; if that fails they stay as clips on V2 / V3 and `angles` in the result says why); subtitles; markers. Check `result`: `len` = `expected`, `subs` > 0, `markers` = count in the cut list. Then `get_thumbnails` on the timeline to eyeball it. The build lands in `01 Timelines/WIP/Latest`, tagged with the video's name and `rough`. A timeline with the same name exists → `--mode inspect` first (`same_as_cut_list: false` = hand edits → ask), then `--replace`: the existing one is filed in `01 Timelines/WIP/Archive` as `<title> vNN <date> rough` (`archived` in the result) and the new build takes the plain name. Nothing is deleted.
6. `videos.py set <slug> rough "review <human>: <timeline name>, markers Yellow = claims to check" --next "human watches the rough"`. Claims, prices and facts to check go in the record's notes.

## Gate

Status `review` = wait. The human's notes go under `## Decisions`; rerun steps 2–5 with `--replace` (the previous rough goes to the archive under its version name; the working timeline keeps the plain name). `done` only when the human approves the rough: tighten builds on it.

## Markers

Blue chapters · Green short / clip candidates · Yellow claims or facts to check · Red end of the main cut (its note lists what was cut) · Purple alternate takes.
