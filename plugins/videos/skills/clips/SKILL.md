---
name: clips
description: Clip batch stage — many vertical 9:16 clips cut from long videos or podcasts, tracked as one record; routes to the vertical-clips plugin for selection, layout, transcript fixes, master, captions, renders, review page and export. Use when making Reels/Shorts/TikToks from a podcast or long talking-head video as a batch, or running /videos:clips.
argument-hint: "[batch name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Full procedure: `/vertical-clips` from the `vertical-clips` plugin (`/plugin install vertical-clips@les-bunkers`).

## Flow (record kind `clip-batch`, one record per batch)

1. rough: choose the moments (hook, claim, length 20–60 s) from the transcripts; write them into the batch config `/vertical-clips` describes (episodes, kept ranges, hook text, rank). New episode → its entry (video, fps, words file, cleaned audio, people, main speaker's side, optional LUT).
2. audio: the cleaned speech must cover every new range; a range outside the cleaned audio masters to silence → check before prep.
3. prep → plan (faces, sticky crops, stacked layout, contact sheet), transcript review (paid second transcript, cached once per clip), master (−14 LUFS, the episode's LUT).
4. Agent step: transcript fixes per clip (verbatim spoken text, `CHECK:` when unsure) + hook; edge fixes where another voice leaks in at a cut.
5. final → captions and hook per look → `hyperframes check` → render → QC; sheets, review page (every clip × look, CHECK words, QC, post text, posting calendar), covers.
6. Gate: the human picks the look and clears the CHECK words on the review page. Status `review` until then.
7. Export → upload-ready files + post text, one `artifacts/videos/<clip-slug>/` per clip (`/videos:publish`).

## Record

`videos.py set <batch> clips "review <human>: review page <path>, <n> CHECK words"`; after export `"done <date> <n> clips"`. Agent doubts and caption typos go in `videos.py note <batch> "…"`.
