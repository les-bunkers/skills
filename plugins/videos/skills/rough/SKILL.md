---
name: rough
description: Rough cut stage of a video — choose what to keep from the transcript as text anchors, snap them to the words, produce a cut list with retimed captions and colour markers, print every seam, and build the rough timeline in DaVinci Resolve as the long 9:16 vertical (multicam aware). Use when choosing takes, building or rebuilding a rough cut or selects timeline, changing what a cut keeps, reading the seams of a cut list, or when running /videos:rough.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Work from the record folder (`videos.py show <video>` → `record:`). Anchor format, markers and cut-list fields: [engine](${CLAUDE_PLUGIN_ROOT}/references/engine.md).

## Do

1. Gate: ingest and transcribe `done`. Read the record's brief, decisions and `style:` files, then the whole transcript (`python "$S/words_dump.py" --project cut/edit.json --src A [--src B]`).
2. Decide keep ranges with the brief: the best take of each idea, false starts and retakes out, asides and private remarks out, claims to verify flagged. Write `cut/rough.json`: `timeline` (the video's plain name, "<title>": no format, stage, version or date in it; the build adds the format), `segments` as `[src, [approx s, "phrase"], [approx s, "phrase"], {options}]` (times as the dump prints them; `*` = any one misheard word; `face: true` on hooks/outros where the program shows a screen but the speaker talks to camera; `alt` = alternate take parked after the end), `markers` (Green = short candidates: moments that stand alone as a short, cut by clips once the 9x16 is approved), `end_note` (what was cut and why).
3. `python "$S/rough.py" --project cut/edit.json --spec cut/rough.json --check` → fix every failing anchor (the error shows the words near it).
4. `python "$S/rough.py" --project cut/edit.json --spec cut/rough.json --out cut/rough` → `cut/rough.cuts.json` + `cut/rough.srt`. Read EVERY printed seam `…cut before || kept … || cut after…`: a seam that cuts mid-thought or repeats a word is a wrong anchor.
5. Resolve (rules for every call: `/videos:resolve`): `python "$S/resolve_build.py" --project cut/edit.json --cuts cut/rough.cuts.json --format 9x16 --dry-run` (tracks, counts, first items), then `--snippet` and run the printed code with the Resolve MCP tool `run_script_unsafe`. V1 = multicam or the program file, sound linked; `face` segments = the multicam clip's active angle switched to the face camera (the build places the pieces, then switches V1's angles through a .drt round trip; if that fails they stay as clips on V2 / V3 and `angles` in the result says why); subtitles; markers. Check `result`: `len` = `expected`, `subs` > 0, `markers` = count in the cut list, `format.ok` true (1080×1920, sources centre-cropped to fill the frame). Then `get_thumbnails` on the timeline across every camera: a face cut by the frame edge or off the crop → Pan per piece (framing: [resolve](${CLAUDE_PLUGIN_ROOT}/references/resolve.md) § Timelines). The build lands in `01 Timelines/WIP/Latest` as `<title> 9x16`, tagged with the video's name, `9x16` and `rough`. A timeline with the same name exists → `--mode inspect` first (`same_as_cut_list: false` = hand edits → ask), then `--replace`: the existing one is filed in `01 Timelines/WIP/Archive` as `<title> 9x16 vNN <date> rough` (`archived` in the result) and the new build takes the working name. Nothing is deleted. Vertical first because the 16:9 and the shorts are made from the approved 9x16 (`/videos` § Order).
6. `videos.py set <slug> rough "review <human>: <timeline name>, markers Yellow = claims to check" --next "human watches the rough"`. Claims, prices and facts to check go in the record's notes.

## Gate

Status `review` = wait. The human's notes go under `## Decisions`; rerun steps 2–5 with `--replace` (the previous rough goes to the archive under its version name; the working timeline keeps its name). `done` only when the human approves the rough: tighten builds on it.

## Markers

Blue chapters · Green short candidates (cut from the approved 9x16 by clips) · Yellow claims or facts to check · Red end of the main cut (its note lists what was cut) · Purple alternate takes.
