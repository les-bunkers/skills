---
name: clips
description: Clips stage — short vertical 9:16 clips for social media. On a talking-head video, shorts cut in DaVinci Resolve from the approved long 9:16 (each a range of it with its cut, framing, grade, sound and layers, plus its own hook and end card), side by side with the wide stage; on podcasts or long videos without a vertical edit, a clip batch through the vertical-clips plugin. Use when making Shorts/Reels/TikToks from a video or podcast, picking short candidates, building, reviewing or exporting shorts, or running /videos:clips.
argument-hint: "[video or batch name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`.

Which route: the record has an approved `<Video> 9x16` (talking-head, master `done`) → route A. A podcast or long video that was never cut vertical (record kind `clip-batch`) → route B.

## Route A: shorts from the approved long 9x16 (talking-head)

Resolve rules for every call: `/videos:resolve`; the short timeline in detail: [resolve](${CLAUDE_PLUGIN_ROOT}/references/resolve.md) § Timelines → Another format from the approved 9x16 (not yet run end to end: read every step back, log what fails there).

1. Gate: master `done` (the 9x16 is approved and frozen in `01 Timelines/Final`). wide may run at the same time: one Resolve writer at a time.
2. Candidates: the Green markers of the rough, plus the words of the approved cut (cuts read back at master, Words file shifted through them). A short stands alone (no "as I said"), opens on a hook, runs 20–60 s, ends on a complete thought. Write `cut/shorts.json`: `[{"n": 1, "topic": "<2-4 words>", "from": [s, "first words"], "to": [s, "last words"], "hook": "…", "why": "…"}]` and ask the human to pick; keep the stage `doing` (a `review` status needs CI'd renders). Hooks are your words on the speaker's face: `uv run "$S/claims_check.py" cut/shorts.json --keys hook --slug <slug>` first ([claims](${CLAUDE_PLUGIN_ROOT}/references/claims.md)).
3. Per picked short: timeline `<Video> 9x16 S<NN> <topic>` (`B.tl_name(video, "9x16", n, topic)`; NN never reused), made from the approved 9x16 (`DuplicateTimeline` → `B.move` to Latest → ripple-delete everything outside the range on every track, edges trimmed and read back), tagged `B.timeline_words(name)` + `timeline`, `latest`. It keeps the cut, framing, grade, sound and layers of the 9x16. On top: the hook layer from frame 0 (legible at frame 0: it is the thumbnail), the end card after the last word, captions checked right after the hook. Layers of the long video that point outside the short (a "next chapter" card) come off.
4. First short = preview: one built, rendered, CI'd and approved before the others.
5. Render with `/videos:master` steps 2–6 (`--prefix "<Video> 9x16 S"` queues every short; `--suffix -short`), loudness per file (`uv run "$S/loudness.py" fix …`), then video CI per short: `uv run "$S/video_ci.py" <short> --slug <slug> --stage clips --profile clip --var hook="<that short's hook>" [--var cta="…"]`. Red → fix or leave it off; amber → show its flagged frames.
6. Review page (every short, its hook, post text, CI flags) on localhost (`serve.py`), `videos.py set <slug> clips "review <human>: <n> shorts, <url>"`. Approved → `B.snapshot(resolve, project, "<short name>", "approved", to=B.FINAL)` per short, `clips "done <date> <n> shorts"`; each short ships through `/videos:publish` as its own `artifacts/videos/<slug>-s<NN>-<topic>/`.

Rules: a short never changes the approved 9x16 (copies only). A cut that is wrong in the long video is fixed on the 9x16 first (`/videos:master` rules), then the shorts whose range it touches are rebuilt.

## Route B: clip batch from podcasts or long videos (`vertical-clips` plugin)

Full procedure: `/vertical-clips` from the `vertical-clips` plugin (`/plugin install vertical-clips@les-bunkers`). Record kind `clip-batch`, one record per batch.

1. rough: choose the moments (hook, claim, length 20–60 s) from the transcripts; write them into the batch config `/vertical-clips` describes (episodes, kept ranges, hook text, rank). New episode → its entry (video, fps, words file, cleaned audio, people, main speaker's side, optional LUT).
2. audio: the cleaned speech must cover every new range; a range outside the cleaned audio masters to silence → check before prep.
3. prep → plan (faces, sticky crops, stacked layout, contact sheet), transcript review (paid second transcript, cached once per clip), master (−14 LUFS, the episode's LUT).
4. Agent step: transcript fixes per clip (verbatim spoken text, `CHECK:` when unsure) + hook; edge fixes where another voice leaks in at a cut. Hooks are your words on the speaker's face: lint them before `final`, `uv run "$S/claims_check.py" clips.json --keys hook --slug <batch>` (a number, price or superlative nobody approved → red or amber; [claims](${CLAUDE_PLUGIN_ROOT}/references/claims.md)).
5. final → captions and hook per look → `hyperframes check` → render → QC; sheets, review page (every clip × look, CHECK words, QC, post text, posting calendar), covers. Video CI per clip before the review page: `uv run "$S/video_ci.py" <clip> --slug <batch> --stage clips --profile clip --var hook="<that clip's hook>" [--var cta="<closing text>"] --input <out>/<ID>/9x16-<look>` (one call per clip, since each hook differs; `--input` records the clip's HyperFrames project, so a caption or hook fix applied there without a new render and a new CI run blocks `done`); red clips are fixed or left off the page (`--forget`), amber clips carry their flagged frames on the page. Cover candidates: `decide.py pick <clip> --at <t1>,<t2>,… --res medium --score "How well does this frame work as a cover thumbnail?" unusable weak good excellent --gate "The person's eyes are closed or half-closed."` drops blinks and mid-word grimaces; the taste ranking is weak, so offer the kept frames to the human ([decide](${CLAUDE_PLUGIN_ROOT}/references/decide.md)).
6. Gate: the human picks the look and clears the CHECK words on the review page. Status `review` until then.
7. Export → upload-ready files + post text, one `artifacts/videos/<clip-slug>/` per clip (`/videos:publish`).

Record: `videos.py set <batch> clips "review <human>: review page <path>, <n> CHECK words"`; after export `"done <date> <n> clips"`. Agent doubts and caption typos go in `videos.py note <batch> "…"`.
