---
name: new
description: Start a new video project — creates the video's record (kind, campaign, content-hub link, talent, sources, style files, deliverables) so every later stage and session knows where it stands. Use when the user wants to start, set up or register a new video, short, clip batch, motion piece or AI video, or runs /videos:new.
argument-hint: "[title or slug]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Record spec: [records](${CLAUDE_PLUGIN_ROOT}/references/records.md).

## Do

1. Project root: `videos.py where`. No `videos.json` and the user works across several clients or brands in one tree → `videos.py init --workspaces "<glob>" [--media-root <drive folder for heavy media>]` in the tree's root. One project → optional `videos.py init [--media-root …]` in it.
2. Gather from the request, the current folder and campaign docs; ask only what is missing:
   - title, kind (`talking-head` · `clip-batch` · `clip` · `motion` · `generative`), workspace when there are several;
   - campaign slug and content-hub ids if the team keeps a hub;
   - talent (who is on camera);
   - sources: paths of rushes, multicam takes, podcast episodes, existing NLE timelines;
   - style files (`style:`): the project's edit style, caption look, brand doc. None yet → note it; the first cutting stage asks;
   - deliverables: formats (9:16 / 4:3 / 16:9), length, platforms, date.
3. Slug: lowercase letters, digits, hyphens, campaign prefix when there is one. Check `videos.py board --all` for a duplicate first.
4. `videos.py new <slug> --title "…" --kind <kind> [--ws W] [--campaign … --content "…" --talent … --style "…" --code "…" --media "…" --resolve "…"]` → record + `cut/` + `checks/`. Media default = `<media_root>/<workspace>/videos/<slug>`.
5. In `video.md`: `## Decisions` = deliverables, formats, dates, what the human asked for, open questions. `## Notes` = the sources with durations (`ffprobe -v error -show_entries format=duration -of csv=p=0 <file>`).
6. `videos.py set <slug> brief "done <date>" --next "/videos:<first stage>"`, then show the board line.

## Notes

- Raw media never moves into the project folder: reference it. Nothing over ~100 MB goes next to the records.
- No content hub → hook, caption and CTA go in the record now and in `release.md` at publish.
- Clip batches: one record for the batch (`kind: clip-batch`); the clip list lives in the batch's own config (see `/videos:clips`).
