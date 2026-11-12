---
name: master
description: Master stage of a video — render the approved DaVinci Resolve timeline to a master file with a stock preset and only the mastered audio track playing, read the final cuts back out of Resolve so captions and graphics follow the edit, and check the delivered file (frame count, loudness, true peak, and that the sound is the right track in sync). Use when an approved timeline must be rendered, a master must be re-rendered after a cut moved, cuts must be read back from Resolve, or when running /videos:master.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Work from the record folder (`videos.py show <video>` → `record:`). Resolve notes: [engine](${CLAUDE_PLUGIN_ROOT}/references/engine.md) § Resolve.

## Do

1. Gate: tighten (or rough) `done` = the human approved the timeline; audio `done` = a mastered track sits on it (`/videos:audio`). The record's `resolve:` names the timeline. Confirm the render with the human (it occupies the GPU and Resolve for minutes).
2. Cuts as they are now, never as planned (the human may have moved some): `python "$S/resolve_build.py" --project cut/edit.json --mode read_cuts --name "<timeline>" --out cut/final.cuts.json --snippet` → run with the Resolve MCP tool `run_script_unsafe`. `unknown` must be empty. In-the-voice joins come back on whole frames.
3. Render: `python "$S/render_finals.py" --timeline "<timeline>" --target-dir "<media folder>/masters" --audio-track MASTER --suffix -master --snippet` → run it. Per timeline it checks that only the audio track named `MASTER…` is enabled (else skipped, with the track list), loads the stock preset by width ("YouTube - 2160p" at ≥ 3840 wide, else "YouTube - 1080p": H.264, keyframe every 24 frames, ~11 Mbps at 4K), sets only folder and name, starts. A stock preset first because a hand-built H.264 job once rendered sound only after audio-only renders.
4. Wait: poll with `run_script` (`project.GetRenderJobStatus(<job id>)`) until `Complete`; ffprobe sees a duration while Resolve still writes. Then `resolve.OpenPage("edit")` (Deliver swallows track toggles).
5. Check the file: `uv run "$S/finals_check.py" "<media folder>/masters/<name>-master.mp4" --cuts cut/final.cuts.json --project cut/edit.json --ref "<mastered track wav>" --json checks/master.json`.
   Pass = video stream present and frames = the timeline's length; −14 LUFS ± 0.5 and true peak ≤ −1.0 dBTP on the encoded file (ebur128; loudnorm reads ~0.3 LU low on a silent tail); content: render = 1.0 × the mastered track at lag 0 (a second track mixed in or a shifted edit fails). No mastered track yet → leave out `--ref` and add `--no-loudness`: the render is then compared with the sound rebuilt from the cut list and the sources.
   Two renders that must line up (track A vs track B): `uv run "$S/sync_check.py" a.wav b.wav` → every window lag 0.
6. Video CI on the master: `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" "<media folder>/masters/<name>-master.mp4" --slug <slug> --stage master` (spec, loudness, black / frozen / silent stretches, face framing, grade, secrets; at most 150 stills per check). RED → fix before anyone sees it.
7. `videos.py set <slug> master "done <date> <path> -14.0/-1.9" --next "formats"` (the measured values; refused without fresh, non-red CI). A failed check → `blocked <what failed>` and tell the human.

## Rules

- Text-free master: captions, hooks and graphics come in `/videos:formats`, from `cut/final.cuts.json` and the tight SRT.
- A cut moved after the render → read the cuts again (step 2), re-render, re-check, rerun formats for that video only.
- `/videos:qc` is the gate before anything ships.
