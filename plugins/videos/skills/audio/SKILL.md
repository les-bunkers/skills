---
name: audio
description: Audio stage of a video — routes to the right tool: whole-timeline speech cleanup and −14 LUFS mastering (audio-cleanup plugin), two-speaker split, loudness solved on the finished short that ships, or damaged-audio repair; holds the loudness targets. Use when cleaning, enhancing, leveling, compressing, normalizing or mastering a video's audio, fixing loudness of a render, or running /videos:audio.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`.

## Route

- **Whole shoot or timeline** (speech cleanup + master, placed back in the NLE, A/B listening page) → `/audio-cleanup` from the `audio-cleanup` plugin (`/plugin install audio-cleanup@les-bunkers`). Two voices on one source → its `--split` step (needs a transcript with speaker ids from `/videos:transcribe`).
- **Finished short or any render that ships** → solve loudness on that file, video copied bit for bit:
  `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/loudness.py" fix <render.mp4> <final.mp4> --json <dir>/loudness.json`
  (`--full` for raw, unmastered audio; `measure <files…>` reads only). Exit 1 = a check failed (packets, duration, lag): read the report.
- **Clip batches** → masters per clip inside `/vertical-clips`.
- **Damaged audio** (dropouts, missing words, a voice to rebuild) → stop and tell the human: that is a repair job with its own tools and, usually, paid models.

## Targets and why

- −14.0 LUFS integrated (±0.5), true peak ≤ −1.5 dBTP on the master, ≤ −1.0 dBTP after AAC encoding.
- Lag 0 samples against the original at several probes: a cleanup that shifts audio breaks lip sync.
- Measure as dual mono on a stereo track when the NLE plays a mono file on a stereo track (+3.01 dB vs the mono file).
- Integrated loudness with ffmpeg `ebur128`; `loudnorm` reads ~0.3 LU low on files ending in silence.
- Always re-solve on the short: a 10 s excerpt of a source mastered over its whole cut reads about −15.8.

## Record

`videos.py set <slug> audio "done <date> -14.0/-1.9"` or `"review <human>: A/B page http://127.0.0.1:<port>/"`.
