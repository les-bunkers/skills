# Video records, folders and status words

Every video has one record. The record is the agent's memory between sessions: state in the frontmatter, the human's decisions and a dated log in the body. `videos.py` reads and writes it; it is plain Markdown, so Obsidian and any editor show it too.

## Folders

The same `<slug>` names the record, the published folder and the media folder. Slug = lowercase letters, digits and hyphens; prefix it with the campaign when there is one (`launch-03-studio-tour`).

```
<project>/.agents/videos/<slug>/          in progress
  video.md        state (frontmatter) + decisions + notes + log (body)
  cut/            cut lists, tighten passes, overrides, words files (small JSON/text)
  checks/         QC results, join checks, reports
<project>/artifacts/videos/                published
  index.md        table: video · campaign · formats · status · record
  <slug>/release.md + <slug>-9x16.mp4 · -16x9.mp4 · -4x3.mp4 · -cover.jpg · .srt · -share.mp4
<media_root>/<workspace>/videos/<slug>/    heavy media: rush links, masters, HyperFrames projects, renders
```

`<project>` = the folder holding `videos.json` (or the current folder). With `"workspaces": "clients/*"` in `videos.json`, each workspace folder holds its own `.agents/videos/` and `artifacts/videos/`, and the board groups by workspace. `media_root` lives in `videos.local.json` (machine-specific) or `$VIDEOS_MEDIA_ROOT`; without it, heavy media stays where the sources are and the record's `media:` points there. Existing project code may keep its own folders: `code:` and `media:` point at them.

## video.md frontmatter

Flat `key: value`, one line each.

```
title: Studio tour
slug: launch-03-studio-tour
workspace: acme
kind: talking-head            # talking-head | clip-batch | clip | motion | generative
campaign: spring-launch       # optional
content: hub:V03 V05          # optional ids in a content hub (see below)
talent: Host name
style: docs/edit_style.md     # taste files the stages read, space-separated paths
code: footage/2026-03-02/rough-cut
media: /mnt/media/acme/videos/launch-03-studio-tour
resolve: Acme project / Studio tour — tight v1
brief: done 2026-03-02        # one key per stage: status word + date or note
generate: …  ingest: …  transcribe: …  rough: …  tighten: …  audio: …  grade: …  master: …  formats: …  clips: …  qc: …  publish: …
next: editor reviews the tighten markers (pink first)
updated: 2026-03-04
```

Status = the first word of a stage value:

| Word | Meaning |
|---|---|
| `todo` | not started |
| `doing` | an agent is on it (say what is running) |
| `review` | waiting on a human; the rest says what to look at (`review editor: timeline v2, pink markers`) |
| `done` | finished, with the date |
| `skip` | does not apply to this kind or this video |
| `blocked` | the rest says why |

Body: `## Decisions` (what the human chose, dated) · `## Notes` (review notes, agent doubts, typos to check) · `## Log` (`videos.py set` appends `- 2026-03-04 tighten: done …`).

## Kinds and their stages

The stages a kind doesn't use start as `skip`.

| Kind | Stages |
|---|---|
| talking-head | brief ingest transcribe rough tighten audio grade master formats qc publish |
| clip-batch | brief transcribe rough audio grade clips qc publish (one record for the batch; every published clip gets its own `artifacts/videos/<slug>/`) |
| clip | brief transcribe rough audio grade formats qc publish (one vertical made by hand) |
| motion | brief audio formats qc publish (graphics-led: offer video, bumper, explainer) |
| generative | brief generate audio formats qc publish (AI video: scene options, storyboard, character/set references, voices, keyframes, animatic, shot clips in `generate`; paid models → ask first) |

## Campaign and content hub

`campaign:` = the campaign slug the project uses for its docs. `content:` = ids of the matching items in a content hub, if the team keeps one (scripts, hooks, captions, approval status). When linked, hook, on-screen text, caption and hashtags come from the hub item and the publish stage reports status back to it. When not linked, the copy lives in `release.md`. The stages never require a hub.

## release.md

```
title: …  slug: …  campaign: …  content: …  source: .agents/videos/<slug>/video.md
formats: 9x16 16x9 4x3   platforms: instagram youtube tiktok
post_date: 2026-03-16   status: ready|scheduled|posted   posted: ig=<url> yt=<url>
loudness: -14.0 LUFS / -1.9 dBTP   qc: pass 2026-03-15
```

Body: caption, hashtags, cover note, CTA link, claims checked. File names: lowercase letters, digits and hyphens only (messaging apps and uploaders mangle `@`, `#` and spaces).

## videos.py

`uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py" <command>` (stdlib only; `python` or `python3` works too).

| Command | Does |
|---|---|
| `where` | project root, config files, workspaces and record counts |
| `init [--media-root P] [--workspaces GLOB]` | writes `videos.json` (+ `videos.local.json` for the media root) in the current folder |
| `board [--ws W] [--wide] [--all]` | every video in progress, current stage, next step; `--wide` = every stage column; `--all` includes published |
| `show <query>` | one record (exact slug, else every word of the query in slug/title) |
| `new <slug> --title T --kind K [--ws W] [--campaign … --content … --talent … --style … --code … --media … --resolve … --next …]` | creates the record, `cut/`, `checks/`; workspace = `--ws`, else the one you are in |
| `set <slug> <stage\|key> "<value>" [--next "…"]` | sets a stage status (validated) or any field, appends a log line |
| `note <slug> "<text>"` | dated line under `## Notes` |
| `release <slug>` | creates `artifacts/videos/<slug>/release.md` from the record + a row in `index.md` |
