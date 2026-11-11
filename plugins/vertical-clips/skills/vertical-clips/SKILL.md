---
name: vertical-clips
description: 9:16 clip factory for podcasts and long talking-head videos. Sticky face crops and a stacked two-person layout, caption text fixed by the agent from three transcripts, -14 LUFS master with an optional per-set LUT, word-timed captions and a frame-0 hook rendered with HyperFrames, QC, covers, a local review page with a posting calendar, and export of approved clips. Use when making Reels, Shorts or TikToks from a podcast or long video, adding a clip or an episode to a clip batch, fixing caption text with a second transcript, re-rendering a clip in another caption look, reviewing clip text fixes, or exporting approved clips for posting.
argument-hint: "[clip ids]"
---

One batch = one folder holding `clips.json` (episodes + clips). Every script reads it from the current folder (`-c path` or `$VC_CONFIG` elsewhere) and writes one folder per clip under `out` (default `clips/`). Steps skip work whose output is newer than its inputs, so rerunning a stage is always safe.

Run commands from the batch folder after `VC="${CLAUDE_PLUGIN_ROOT}/scripts"`; any Python 3.11+ works (`uv run` too). Install once: [setup](${CLAUDE_PLUGIN_ROOT}/references/setup.md) (ffmpeg, uv, HyperFrames 0.9.19, optional raea). Config reference: [config](${CLAUDE_PLUGIN_ROOT}/references/config.md).

**Costs money:** the transcript review makes one OpenAI `gpt-4o-transcribe` call per clip (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, default `https://api.openai.com/v1`), cached forever in `<clip>/openai_stt.json`. Ask the human before the first paid call of a batch and say how many clips need one. Everything else runs locally.

## Step 1. Set up the batch

Owner: agent, with the human choosing the moments.

1. Per episode you need: the video, a Words file of it (shared Words format, any STT; `transcribe` stage of the videos plugin or your own), optionally YouTube auto-captions (`.fr-orig.vtt` / `.en-orig.vtt` from `yt-dlp --write-auto-subs`), a speech-cleaned audio file on the same time origin as the video (e.g. from the `audio-cleanup` plugin; plain source audio works, it is the default), the people and the main speaker's seat (`main_side`: left/right in the wide shot), and optionally a `.cube` LUT for that set.
2. Read the transcript to pick moments: `python "$VC/vc_config.py" transcript EP1 600 900` prints timed lines. Good clips: one claim, a hook in the first sentence, 20–60 s.
3. Write `clips.json`:
   ```json
   {"out": "clips", "language": "fr", "looks": ["signal"],
    "episodes": {"EP1": {"video": "media/ep1.mp4", "words": "media/ep1.words.json",
                         "captions": "media/ep1.fr-orig.vtt", "audio": "media/ep1.cleaned.wav",
                         "people": "the guest (main speaker) and the host", "main_side": "right",
                         "lut": "looks/ep1.cube"}},
    "clips": [{"id": "EP1-03", "episode": "EP1", "in": 673.36, "out": 779.67,
               "trims": [[677.38, 677.71], [687.52, 762.15]],
               "hook": "Ce n'est pas la caméra *qui coûte cher*", "rank": 1, "topic": "what the clip claims"}]}
   ```
   Kept ranges: `in`/`out` minus `trims`, or `ranges`, or text anchors `from_text`/`to_text` (+ `near`), or `cuts` (a shared Cut list file). Machine paths go in `clips.local.json` beside it. Every key: [config](${CLAUDE_PLUGIN_ROOT}/references/config.md).
4. Check: `python "$VC/vc_config.py" ranges` prints each clip's ranges with their first and last words. Fix anchors or trims until each range starts and ends on the words you want.

Done when every clip prints sensible edges.

## Step 2. Prep: plan, transcript review, master

Owner: agent. Command:

```
python "$VC/run_batch.py" prep [IDS…]        # PAID=1 once the human said yes to the OpenAI calls
```

Per clip: `plan.json` + `plan_sheet.jpg` (shots, faces, layout), `<ID>.cuts.json`, `stt_review.{json,md}` (hunks to decide), `assets/master.mp4` + `master.json`. Timing on a desktop: plan ~10–20 s, review ~5 s, master ~20 s to 3 min. Without `PAID=1` a clip with no cached OpenAI text stops at the review and says so; `python "$VC/stt_reconcile.py" ID --two-sources` reviews with the Words file and captions only.

Look at every `plan_sheet.jpg` (source frame with face boxes and crop | result): main speaker in the top panel of stacks, faces inside the crop, no shot flagged `review` (no face found). A wrong shot → edit its `layout`/`crop`/`panels` in `plan.json`, delete `assets/master.mp4`, rerun prep (the plan is only rebuilt when the clip's ranges change, so the edit stays).

Done when the sheets look right and every master printed about −14 LUFS.

## Step 3. Agent: fix the caption text

Owner: agent; the human clears the `CHECK:` items later on the review page.

Sources per clip: the Words file (word timings = the reference), captions (YouTube auto-captions: Google ASR, word starts), OpenAI on the clip's own audio (text only). `stt_review.md` lists every hunk where the reference differs from the captions (case, punctuation, fillers ignored) with OpenAI's reading and the reference context.

Write `<clip>/stt_fixes.json`:

```json
[{"hunk": 3, "text": "what is said", "why": "short reason"},
 {"word": 0, "text": "Ce", "why": "clip opens on this word"},
 {"words": [44, 46], "text": "", "why": "cut-off restart at a seam"}]
```

Omit a hunk = keep the reference. `"text": ""` deletes. Words no hunk covers (all three wrong, or a bad hunk split): `word` / `words` [i0, i1) on `stt_review.json` `ref_words` indices. Fixes may not overlap. Write `[]` when nothing needs fixing (final skips clips without the file). Then `python "$VC/stt_reconcile.py" ID --apply`, read `text_fixes.md`, and read the joined `words.json` text start to end as a viewer would.

Rules (language-specific forms: [Québec French](${CLAUDE_PLUGIN_ROOT}/references/transcript-rules-fr.md)):
- Text = what the speaker SAYS, verbatim, in the spoken register. Never rewrite it into the written standard language.
- The majority of three usually wins, but decide by meaning, grammar and topic. Each source fails its own way: the Words STT mishears rare words; YouTube drops fillers and short function words and writes numbers as digits; OpenAI smooths disfluencies and sometimes paraphrases, so it is the weakest on exact wording and the best tiebreak on content words.
- A hunk where only the captions have words (reference ∅): add them only if OpenAI or grammar backs them.
- Clip edges: words of the other speaker after the out-point, or a cut-off half word, get deleted. A reference that repeats the last sentence (STT hallucination) where captions and OpenAI have it once: delete the repeat only if both agree.
- Stutters (« la la la », "the the") collapse automatically at `--apply` (never across `. ? !`): leave them. Exact doubles of emphasis words (`vc_lang.KEEP_DOUBLE`) stay; three or more still collapse.
- Names and brands: spell them as the config `transcription.vocabulary` lists them.
- Keep the reference's punctuation style; fix punctuation only where a wrong period or question mark would break a caption line.
- Unsure → still choose, and start `why` with `CHECK:` plus what you heard and why. The review page lists every CHECK item for the human.

Edge problems (another voice caught at a cut, a clip ending mid-sentence, dead air > 1 s) → `<clip>/edges.json`, `seg` = index in `stt_review.json` `segments`:

```json
[{"seg": 0, "in_s": 673.4, "why": "start in the dip before « ce »"},
 {"seg": 4, "out_s": 779.467, "why": "host's « Fait que » starts 779.63"},
 {"seg": 2, "cut": [680.1, 680.6], "why": "host backchannel mid-take"},
 {"seg": 3, "drop": true, "why": "dead air"}]
```

Shrink, drop or split only: extended audio has no transcript. `in_s`/`out_s` may move outward ≤ ~0.3 s when the word is already in the transcript (a clipped onset or last word). `--apply` remaps words.json onto the new ranges (each word through its own range, so a cold open replaying a later range stays put) and logs dropped words; `run_batch.py all ID` then re-plans and re-masters. Find the other voice by pitch on `<clip>/clip_16k.wav` (written by the paid review; two speakers in the measured batch sat at ~95–105 Hz and 155–210 Hz); STT speaker labels are unreliable at turn edges. The master fades every cut (8 ms inside the clip, 40 ms at its first in and last out).

Done when every clip has `stt_fixes.json` and its words.json reads right.

## Step 4. Final: captions, check, render, QC

Owner: agent. One render chain at a time (RAM, see setup). Command:

```
python "$VC/run_batch.py" final [IDS…]       # LOOKS="signal editorial" to render other looks
python "$VC/run_batch.py" sheets [IDS…]      # renders/sheet.jpg: one row of frames per look
python "$VC/qc_report.py"                    # every clip x look -> problems only, _review/qc.json
```

Per clip and look: `9x16-<look>/` (HyperFrames project), `check-<look>.json`, `renders/<ID>_<look>.mp4`, `renders/raea-<look>.json` when raea is installed. Render ≈ 45 s per minute of clip per look. A render waits for `MIN_FREE_GB` (default 3) of free RAM, then skips.

QC, in this order:
- `check-<look>.json` `"ok": true`. Lint warnings about unread caption lines (`line_unread`) and Studio editability are expected with speech captions.
- `BAND WARNING` lines (also in `build-<look>.json`): a face sits within 24 px of the caption text block at that moment → look at that frame.
- raea: loudness −14 ±0.5 LUFS, true peak ≤ −1 dBTP. "Sound stops dead" on a natural pause inside a take = false alarm; at a cut = listen.
- `renders/sheet.jpg` + `plan_sheet.jpg`: hook legible, no caption on a face, main speaker in the top panel, text sitting on the seam.
- Soft crops mean a low-resolution source (a 720p file is a ~2.7x upscale in 9:16): get the original recording.

## Step 5. Review page and covers

Owner: agent builds, human reviews.

```
python "$VC/covers.py" [IDS…]                # renders/<ID>_<look>_cover.jpg: hook over the longest single shot
python "$VC/review_page.py" [--start 2026-01-05]   # <out>/index.html + _review/schedule.json
```

The page (open from disk): every clip × look with a look switch, hook, topic, QC, CHECK words first, every text fix, post text with a copy button, the posting calendar (one lane per clip `account`, `schedule.per_day` a day, episodes alternate by rank, a tag never repeats within 3 slots). A clip's cover frame defaults to the middle of its longest single shot (the first frame often shows the other person listening); `cover_at` in the clip overrides it.

Gate: the human picks the look per clip and clears the CHECK words. Apply their answers as new `stt_fixes.json` entries, rerun `final` for those clips, then set `"approved": true` on each approved clip in `clips.json`.

## Step 6. Export

Owner: agent, after approval; posting stays with the human.

```
python "$VC/export_finals.py" signal [EP1-03=editorial …] [--publish-dir <project>/artifacts/videos] --dry
python "$VC/export_finals.py" signal [EP1-03=editorial …] [--publish-dir <project>/artifacts/videos]
```

`<out>/_deliver/<date>-<lane>-<n>-<id>.mp4` + `.txt` (post text) + `-cover.jpg` + `-share.mp4` (small copy for texting) + `README.md`. With `--publish-dir` (or config `publish.dir`) every approved clip also gets its own `<slug>/` in the videos plugin's published layout: `<slug>-9x16.mp4`, `-cover.jpg`, `-share.mp4`, `release.md` (title, slug, campaign, content, source, formats, platforms, post_date, status, posted, loudness of the shipped file, qc; ## Caption / ## Hashtags / ## Notes) and its row in `index.md`. An existing `posted:` value is kept. Only clips with `"approved": true` export unless `--all`.

## Layout rules (clip_plan.py) and why

- Shots = relative frame-difference cut detector (64×36 gray mean abs diff ≥ 6× its ±1 s median and ≥ 4) plus range seams. Absolute thresholds missed every cut in a dark set (ffmpeg scene score peaked at 0.27).
- False cuts inside one camera shot (flash, fast move) → adjacent same-layout shots within 10 % of the crop width merge; they made the frame jump a few px for 4–6 frames.
- Faces = YuNet 2023mar, 3 frames per shot. A face touching the left/right 1.5 % edge is dropped (an over-the-shoulder half face turned singles into false stacks); a face centred in the bottom 45 % is dropped (hands and mics read as faces, three false stacks in one batch).
- Single = full-height 9:16 window on the person or pair. Stack = two people too far apart for one window: MAIN SPEAKER ON TOP (`main_side`), top panel 1080 × seam (1040), bottom 1080 × 880 on the other person, face ≈ 24 % of the panel height, eyes at 40 % (top) / 36 % (bottom, above the platform caption area).
- Sticky crop for one-person singles: a tracker that followed every lean looked nervous, and a fixed crop cut off a speaker leaning 600 px in 2 s. Faces sampled at 3 fps; holds = longest runs whose face boxes fit the crop minus 10 % per side → one fixed x each; a hold < 1 s between two pans merges into a neighbour when the face fits with 5 % margins; 0.6 s smoothstep pan between holds, ending 0.2 s after the face crossed.
- Low faces: a single whose face box ends below `low_face` (seam − 124 = 916 px) gets a tighter bottom-anchored crop, the smallest zoom that clears the caption band, ≤ 1.2× (~25 of 279 shots: the host listening, a guest sitting low). A seam of 1000 put captions on the host's mouth.
- 0 faces → centred, flagged `review` in plan.json and on the sheet.

## Captions and hook (build_vertical.py) and why

- ONE text band, seam ± 110 px (930–1150 at seam 1040), on every shot: on stacks the text covers the hard edge between panels, and it never jumps between shots (per-shot bands under the chin read as jumpy). While a stack is on screen a 300 px dark gradient runs across the seam. Singles' chins end ~850–910 px (p50–p90 of 279 shots), above the band.
- Platform UI: Reels, Shorts and TikTok overlay the top bar and the bottom post text, buttons and audio row; y 168–1330 px stays clear on 1080×1920. The band and the hook sit inside it.
- Hook = the thumbnail: frame 0, held `hook_s` (2.8 s) in the band, then captions take over. `*tone-2*` marks the accent phrase; 2 lines max, the break goes before the tone-2 phrase unless that shrinks the type > 15 %.
- Grouping: speech-paced groups (≤ `max_chars`, ≥ `min_s`, closed at sentence ends and pauses ≥ 0.4 s); a group never ends on a weak word (articles, prepositions, pronouns: `vc_lang.WEAK`), lone words merge forward, else backward (gap < 0.8 s). Fillers are dropped (`vc_lang.CAPTION_DROP`).
- Apostrophes get side bearing: at weight 800 « L'IA » read « LIA ».

Looks (`python "$VC/build_vertical.py" --list`; config `look_presets` adds your own, `{"base": "signal", "accent": "#00E0A0"}`):

| Look | Captions | Hook |
|---|---|---|
| `signal` (default) | Bricolage 800 white, spoken word yellow, small pop | Bricolage 800, yellow tone-2 |
| `signal-unlock` | Unlock 400, same colours | as signal |
| `editorial` | Bricolage 300 large, words fill in, orange | Bricolage 300 118 px |
| `hybrid` | as signal | as editorial |
| `drench` | white on a solid colour slab, words fill in | slab, bold white tone-2 |

## Grade

A LUT is per set: lighting differs per shoot, so never reuse one episode's LUT on another; an episode without a grade stays ungraded (no `lut`). Export the grade as a 65-point `.cube` from the grading app (DaVinci Resolve scripting: `timeline_item.ExportLUT(resolve.EXPORT_LUT_65PTCUBE, path)` on the adjustment clip or graded clip). A LUT is exact only for colour-only nodes (balance, log wheels, curves, HSL qualifiers); windows, masks, blur, noise reduction, grain and vignettes do not survive it. Measured: ffmpeg + LUT vs the app's own still, mean abs 1.0/255 (4.9 without); render vs master 0.9. The LUT goes on the source frame before the crop (bt709 limited → 16-bit RGB full → tetrahedral lut3d → bt709 limited), masters are tagged bt709. A re-exported LUT (same name, newer file) re-masters and re-renders on the next run.

## Traps

- Never rerun a review once `stt_fixes.json` exists: hunks renumber and the fixes land on the wrong words. `run_batch` skips the review when `stt_review.json` exists; delete both to start a clip's text over.
- A cleaned audio file that covers only some ranges is digital silence elsewhere: the review and the master stop on a silent clip (once, ten paid calls came back as the prompt echoed over silence). Clean the new ranges first.
- Loudness: measured dual mono (a mono master on a stereo track reads +3.01 dB); the limiter is 4× oversampled; the gain is solved on the clip itself, because a short excerpt of a source mastered as a whole read −15.8.
- HyperFrames traps (one root per project, GSAP tag, keyframes, check stderr, Windows shim, render lock): [setup](${CLAUDE_PLUGIN_ROOT}/references/setup.md).
