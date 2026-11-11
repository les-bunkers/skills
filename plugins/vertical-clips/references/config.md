# clips.json: the batch config

One JSON file per batch, next to the work. `clips.local.json` beside it is merged over it (dicts key by key, other values replaced) and holds machine-specific values: media paths, `cache_dir`. Keep the local file out of version control. Relative paths resolve against the folder holding `clips.json`; `~` and `$VARS` expand.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `out` | `"clips"` | folder for the clip folders, `_review/`, `_deliver/`, `_logs/`, `_cache/` |
| `language` | `"en"` | transcript language (`fr`, `en`, `fr-CA` …): picks the filler, weak-word and double tables in `vc_lang.py`, the OpenAI language and the page `lang` |
| `looks` | `["signal"]` | looks `final` builds and renders; env `LOOKS` overrides, clip `looks` overrides per clip |
| `look_presets` | | your own looks: `{"brand": {"base": "signal", "accent": "#00E0A0", "ink": "#FFFFFF", "cap_font": "Inter"}}` (keys of `PRESETS` in `build_vertical.py`) |
| `fonts` | | extra font files for presets: `{"Inter": {"file": "fonts/Inter.woff2", "weight": "100 900"}}`; ship the font's license with it |
| `layout` | `{"seam": 1040}` | `seam` = caption centre line and top-panel height of stacks; `low_face` (default seam − 124) = lowest face-box bottom a single may have before it is lifted |
| `hook_s` | `2.8` | seconds the hook holds at frame 0 |
| `transcription` | | OpenAI prompt: `context` (one sentence about the show), `vocabulary` (names and terms, also the spelling list for the agent), or a full `prompt` template with `{people}` `{topic}` `{vocabulary}` |
| `schedule` | | review-page calendar: `start` (YYYY-MM-DD, default today), `per_day` (int, or `{"lane": n}`; default 2), `tag_gap` (default 3) |
| `publish` | | export into the videos plugin layout: `dir`, `campaign`, `source` (the batch record, e.g. `.agents/videos/<batch>/video.md`), `platforms` (default `instagram youtube tiktok`), `slug_prefix` (default `<campaign>-`, else `clip-`) |
| `title` | `"Vertical clips"` | review page title |
| `cache_dir` | `~/.cache/vertical-clips` | YuNet model; `$VERTICAL_CLIPS_CACHE` overrides |
| `episodes` | | `{key: episode}` |
| `clips` | | list of clips, or the path of a sibling JSON file holding the list (or `{"clips": [...]}`) |

## Episode

| Key | Required | Meaning |
|---|---|---|
| `video` | yes | source video; all times are seconds of this file |
| `words` | yes | Words file of the video (shared format, `time_base: source`); events are ignored, `type: filler` words never reach the captions |
| `words_splices` | | `[{"file", "offset", "from", "to"}]`: replace the main words starting in `[from, to)` with a second Words file's words (times + `offset`) in that window. For an STT dropout re-transcribed separately; fit `offset` against another transcript's word times, file names lie |
| `captions` | | second transcript: YouTube auto-captions `.vtt` (word-timed `<c>` tags), any `.vtt`/`.srt` (cue text spread over the cue) or a Words file |
| `audio` | | speech-cleaned audio on the video's time origin (WAV/FLAC at 48 kHz are read in place; anything else is decoded once into `<out>/_cache/`). Default = the video's own audio |
| `fps` | | probed from the video when absent |
| `people` | | who speaks, for the transcription prompt: `"the guest (main speaker) and the host"` |
| `main_side` | | `left` or `right`: the main speaker's seat in the wide shot; their panel goes on top of a stack (default `left`) |
| `lut` | | `.cube` grade for this set only |
| `language`, `prompt`, `vocabulary` | | per-episode overrides |

## Clip

| Key | Meaning |
|---|---|
| `id` | folder name and file prefix, `EP1-03` |
| `episode` | episode key (default: the id before the first `-`) |
| ranges | first match: `cuts` (Cut list file, its segments in order) · `ranges` (`[[in, out], …]`) · `in` + `out` (or `from_text` + `to_text`, optional `near` seconds to pick the occurrence, `lead` 0.15 / `tail` 0.3 s pads that never reach into the neighbouring word) minus `trims` (`[[a, b], …]`), after `prepend` ranges (a cold open) |
| `hook` | frame-0 text, `"Lead *tone-2 phrase*"`; empty = no hook, captions from 0 s |
| `rank` | posting order inside an episode and lane |
| `topic` | one sentence: what the clip claims (prompt + review page) |
| `post`, `hashtags` | caption text and hashtags for the post (export, review page) |
| `account` | posting lane (default `main`) |
| `tags` | themes; one never repeats within `tag_gap` slots of a lane |
| `looks` | looks for this clip |
| `cover_at` | clip seconds of the cover frame |
| `schedule` | `false` keeps it off the calendar (an alternate cut) |
| `approved` | `true` once the human approved it: export takes only these unless `--all` |
| `slug`, `content` | published folder name; content-hub id for release.md |

Frames: `in_f = floor(in × fps)`, `out_f = ceil(out × fps)`.

## Files per clip (`<out>/<id>/`)

| File | Written by | Notes |
|---|---|---|
| `plan.json`, `plan_sheet.jpg` | clip_plan | shots, faces, layout; edit before mastering to override a shot |
| `<id>.cuts.json` | clip_plan | kept ranges after edges.json, shared Cut list format |
| `stt_review.json`, `stt_review.md` | stt_reconcile | hunks; never regenerated once fixes exist |
| `openai_stt.json`, `clip_16k.wav` | stt_reconcile | paid result, cached forever; clip audio sent |
| `stt_fixes.json` | agent | `[{"hunk"|"word"|"words", "text", "why"}]` |
| `edges.json` | agent | `[{"seg", "in_s"|"out_s"|"cut"|"drop", "why"}]` |
| `words.json`, `text_fixes.md` | stt_reconcile --apply | Words format, times in `assets/master.mp4` |
| `assets/master.mp4`, `master.json` | clip_master | 1080×1920, −14 LUFS |
| `9x16-<look>/`, `build-<look>.json`, `check-<look>.json` | build_vertical, run_batch | HyperFrames project per look |
| `renders/<id>_<look>.mp4`, `raea-<look>.json`, `sheet.jpg`, `<id>_<look>_cover.jpg` | run_batch, covers | |

Batch level: `<out>/index.html` (review page), `_review/schedule.json`, `_review/qc.json`, `_deliver/`, `_logs/`, `_cache/`.
