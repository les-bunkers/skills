---
name: publish
description: Publish stage — after QC and the human's approval, puts the shipped files into artifacts/videos/<slug>/ with release.md (formats, platforms, post date, caption, hashtags, loudness, QC), a small share copy and the index row; updates the content hub when linked. Posting stays with the human. Use when a video is approved and must be packaged for posting, scheduled, or marked as posted, or when running /videos:publish.
argument-hint: "[video name]"
disable-model-invocation: true
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Record and release.md spec: [records](${CLAUDE_PLUGIN_ROOT}/references/records.md).

## Do

1. Gate: `qc` is `done` and the human approved the final (look, text). Posting and ad publishing stay with the human.
2. `videos.py release <slug>` → `artifacts/videos/<slug>/release.md` (filled from the record) + a row in `artifacts/videos/index.md`.
3. Copy the shipped files in; names = lowercase letters, digits, hyphens: `<slug>-9x16.mp4`, `-16x9.mp4`, `-4x3.mp4`, `<slug>-cover.jpg`, `<slug>.srt`, and a share copy small enough to text:
   `ffmpeg -i <slug>-9x16.mp4 -c:v libx264 -preset slow -crf 23 -maxrate 2600k -bufsize 5200k -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart <slug>-share.mp4` (≈ 1 MB per 4 s).
   Files over ~100 MB stay under `media_root`; release.md links them.
4. release.md: formats, platforms, post_date, caption + hashtags (from the content-hub item when linked, else written here in the project's brand voice; claims checked), loudness and qc lines, CTA link.
5. Content hub linked (`content:` in the record) → update the item's status and copy through the hub's own tools.
6. Ads: drafts only in whatever ad tool the team uses; the human publishes.
7. `videos.py set <slug> publish "review <human>: ready in artifacts/videos/<slug>"`. After posting: release.md `status: posted`, `posted: ig=<url> yt=<url>`, then `videos.py set <slug> publish "done <date>"`.

## Clip batches

The `/vertical-clips` export writes each approved clip into its own `artifacts/videos/<clip-slug>/` (video, cover, share copy, release.md with post text and date) and the index.
