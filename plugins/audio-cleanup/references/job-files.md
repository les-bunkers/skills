# Job files

Everything a job writes lives under `<job>/`. Source media is only read.

```
<job>/
  job.json, job.local.json     settings (you write them; .local = machine paths, never shared)
  plan.json                    resolve_cleanup.py plan
  run.log                      cleanup.py output, appended per run
  work/                        intermediates (safe to delete after approval; rerun rebuilds them)
    jobs.json                  one entry per source (layout, split, speakers, turns)
    <key>.src.wav              source audio, first stream, 48 kHz float, original channel count
    <key>.mix.regions.wav      used ranges (+2 s handles) glued with 5 s digital silence, mono
    <key>.diar.json            diarize.py output (split sources)
    <key>.split.json           split result + signature (rerun skip)
    <key>.s0/s1.regions.wav    per-speaker tracks (split sources)
    <key>.*.reuse.wav (+.json) RE-USE outputs + speed/VRAM benchmark
    <key>.reuse.final.regions.wav, <key>.master.regions.wav
  reuse/<name> RE-USE.wav (+ <name> RE-USE <speaker>.wav) and .<fps>fps.mov wrappers
  master/<name> master -14 LUFS.wav and .<fps>fps.mov wrapper
  manifest.json, split_quality.json, scan.json
  ab-page/index.html, clips/*.mp3, whisper_cache.json
```

`<key>` = source file stem (non-alphanumerics → `-`, 40 chars) + 6 hex of the path hash. `<name>` = the stem
(a hash suffix is added when two sources share a stem).

## job.json

| Key | Type | Default | Meaning |
|---|---|---|---|
| `split` | list of path substrings, or `["all"]` | `[]` | two-speaker sources; CLI `--split` overrides |
| `stt` | list of `{"match", "words", "names"}` | none | per source (path substring): a Words file with `speaker` ids and the display name per id |
| `language` | string | auto | Whisper language for the A/B page |
| `target_lufs` | number | -14 | master target; the compressor threshold follows it (target + 4 dB) |
| `home`, `reuse_python`, `diar_python`, `reuse_dir`, `pyannote` | paths | env | tool locations; put them in `job.local.json` |

Relative paths resolve against the job folder. The `words` file uses the repo's shared Words format
(`{"words": [{"text", "start", "end", "speaker", "type"}]}`, seconds from the start of the source file); words
without `speaker` and `type: "event"` are ignored. A legacy `<job>/stt.json` holding the same list (key `stt`
for the file) is read when job.json has no `stt`.

## plan.json (resolve_cleanup.py plan)

```json
{"project": "<Resolve project>", "made": "2026-01-31T10:00:00",
 "timelines": [{"name": "...", "fps": 23.976023976023978, "start_frame": 86400, "track": 1,
   "items": [{"path": "<source file>", "rec_f": 0, "dur_f": 159, "src_in_f": 35, "src_out_f": 194,
              "clip_fps": 23.976023976023978, "src_in_s": 1.46, "src_out_s": 8.09,
              "api_source_start_s": 1.46, "api_source_end_s": 8.09}],
   "skipped": [{"index": 3, "name": "...", "type": "Multicam", "reason": "no file path (multicam / compound / generated)"}]}]}
```
`rec_f` = record frame from the timeline start; `src_out_f` exclusive; source frames at the clip fps
(23.976 / 29.97 / 59.94 … stored exactly as n×1000/1001). One timeline per entry; re-planning one replaces it.

## manifest.json (finish + master)

| Field | Meaning | Pass mark |
|---|---|---|
| `target_lufs`, `master_track` | target and the Resolve track name `place` uses | |
| `sources[].key/path/name/clip_fps/samples` | identity | |
| `sources[].split`, `speakers`, `stt_agreement`, `overlap_speech_s` | split info; `stt_agreement` = share of diarized speech time matching the STT speaker | ≥ 0.95 typical |
| `sources[].reuse_gain_tp_cap_db` | gain applied so the RE-USE result peaks ≤ −2 dBTP | ≤ 0 |
| `sources[].lag_samples` | RE-USE vs original at 20 / 50 / 80 % of the used audio (4 s FFT cross-correlation, ±100 ms) | all 0 |
| `sources[].outputs.reuse/master/speakers.<name>` | `{"wav", "resolve_media"}` (full length; mov wrapper for Resolve) | |
| `sources[].master.gain_db`, `plain_gain_db`, `solve` | solved gain, gain-to-target without dynamics, solver history `[gain, LUFS]` | gain ≤ plain + 6 dB (else abort) |
| `sources[].master.chain` | the exact ffmpeg filter chain | |
| `sources[].master.played_before/after` | `{"I", "TP", "LRA"}` of this source's audio as played on the timelines (dual mono) | after: I = target ± 0.2, TP ≤ −1.5 (`tp_ok`) |
| `sources[].master.aac_256k` | the played audio after an AAC 256k round trip | TP ≤ −1.0 |
| `timelines.<name>` | `{"I", "TP", "LRA"}` of each timeline as it will play from the masters (several sources → one reading) | I = target ± 0.5, TP ≤ −1.0 |

`check_job.py <job>` applies these marks.

## split_quality.json (quality)

Per split source with STT words: `pitch_median_hz` per speaker, `agreed_words` (words where Praat pitch and the
STT speaker agree), `wrong_words`, `wrong_share` (pass ≤ 0.04; reference runs 0.005–0.026). `skipped` with a
reason when there is no STT or the two pitch medians are < 40 Hz apart (similar voices: judge by ear).

## scan.json (scan)

Per source: `events[]` = RE-USE dropouts against the original (50 ms frames where the original is above
−30 dBFS and the RMS-matched RE-USE result is ≥ 15 dB under; frames ≤ 300 ms apart merged): `src_s`, `dur_s`,
`orig_db`, `drop_db`, `in_cut` = `[{"timeline", "tc"}]` (mm:ss.ss on each timeline that plays it; empty = in a
handle, inaudible). The A/B page builds its spots from the in-cut events, deepest first.

## work/jobs.json (prepare, then diarize / split)

Per source: `key`, `path`, `name`, `clip_fps`, `src_wav`, `src_samples`, `src_channels`, `layout`
(`[{"src_start", "src_end", "cat_start"}]` in samples: where each region sits in the source and in the region
file), `cat_samples`, `mix`, `split`, `stt`, `stt_names`; after diarize: `diar_excl` / `diar_full` (turns in
region-file seconds), `label_names`, `speakers` (most talk first), `stt_agreement`, `overlap_speech_s`,
`diar_seconds`; after split: `turns`, `split_cuts`, `split_sum_error` (max |speaker 0 + speaker 1 − mix|, ~1e-8).

## work/<key>.diar.json (diarize.py)

`{"source", "model", "num_speakers", "audio_s", "seconds", "peak_vram_gib", "exclusive": [[start, end, label]],
"full": [[start, end, label]]}` in source seconds. `exclusive` = one speaker at a time (drives the split),
`full` keeps overlaps (measures overlapped speech).
