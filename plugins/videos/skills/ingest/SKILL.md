---
name: ingest
description: Ingest stage of a video — inventory the recordings of each take (program/switcher output, ISO camera angles, per-mic audio), write the edit project file, measure audio sync offsets between files, scan which camera shows what, import the rushes into DaVinci Resolve and build synced multicam clips. Use when new footage must be probed, synced, imported or turned into multicam clips, when cameras drift or don't line up, or when running /videos:ingest.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Formats, fields and measurements: [engine](${CLAUDE_PLUGIN_ROOT}/references/engine.md).

## Do

1. `videos.py show <video>` → the `record:` line gives the record folder; work from there (`cd` into it). `mkdir -p cut`.
2. Sources still copying (a studio drive syncing, an upload)? Wait until the folder size is stable for 3 minutes: new folders keep appearing mid-copy.
3. Inventory each take: the **program** file (live-switched output, else the only camera), every ISO angle, the file a transcript was or will be made on, extra per-mic WAVs. Write `cut/edit.json`, one source per take with a short id (`A`, `B`… or the take's time), `fps`, `language`, `keyterms` (names and products the speech-to-text must spell right). Media paths that only hold on this machine go in `cut/edit.local.json` (same shape, merged over it, never committed). Example and field table: engine.md § edit.json.
4. Sync: `uv run "$S/sync.py" --project cut/edit.json`. It prints, per file, duration / fps / frames and the offset to the program file from 4 windows. Pass = all windows agree, correlation > 0.6, the take's files agree within 0.5 s. `NO SYNC` → `--search 3` (a lead over 0.5 s was missed once). Then rerun with `--write` (stores `words_offset_s` and each angle's `offset_s`; backup beside edit.json). Never assume an offset: one studio's camera → program offset ran 0.004–0.144 s across takes of the same evening.
5. Camera scan, for sources with angles (tighten uses it to switch cameras; a GPU decodes ~10 min of 4K per angle per minute): `uv run "$S/angles_scan.py" --project cut/edit.json --src A [--src B]` → `cut/scans/<id>.scan.json`. It prints how often the program shows a camera vs a screen and whether raw green screen is visible.
6. Resolve (Blackmagic's DaVinci Resolve MCP, Resolve Studio running; skip with the NLE named in the record otherwise). Set `"resolve": {"root_bin": "<shoot name>"}` in edit.json, then for each mode print the call and run it with the MCP tool `run_script_unsafe` (60 s cap, one mode per call):
   `python "$S/resolve_build.py" --project cut/edit.json --mode import --snippet` then `--mode multicam --snippet`.
   Read `result`: `import` → `missing` empty; `multicam` → `frames` = `expected` per source. A mismatch of one frame = confirm the offset on `get_thumbnails` of 4 frames of the multicam and the program clip. Multicam renames the media-pool clips `1 <id> program`, `2 <id> <angle>`… because Resolve orders angles by clip name (the files on disk stay untouched).
7. Record: `videos.py set <slug> ingest "done <date>" --next "transcribe"`; add the media folder to the record (`videos.py set <slug> media "<path>"`) and the Resolve bin (`videos.py set <slug> resolve "<project> / <bin>"`).

## Checks

- Every file decodes; frame counts of one take agree within ~0.5 s; offsets measured, stored, not guessed.
- Never modify or move source media. Heavy media stays under the media root; the record keeps only `cut/` data.
- A WAV-only source on a timeline with another fps lands on wrong source times (Resolve gives WAVs the project fps): wrap it in a mov with a tiny black video at the timeline fps before import (engine.md § Resolve).
- Status words: anything failing that the human must fix (missing angle, corrupt file) → `videos.py set <slug> ingest "blocked <why>"`.
