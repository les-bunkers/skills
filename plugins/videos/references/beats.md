# Music beat map (beats.py)

For music beds under music-led pieces (bumper, offer video, hook card, motion-only piece). Voice-led videos keep cueing graphics on transcript words. Times = seconds from the start of the file, 3 decimals.

```
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/beats.py" analyze <audio|video> [--out beats.json] [--start s --end s] [--meter 4] [--bpm n] [--downbeat s] [--grid-mode auto|tracked|steady] [--hit-min 0.5] [--force]
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/beats.py" snap <beats.json> (--times 1.2,3.4 | --json spec.json --path a.*.cues --key t[,end]) [--grid downbeat,beat|half|hit] [--max-shift 0.15] [--fps 30] [--offset s] [--min-strength 0.8] [--out snapped.json]
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/beats.py" click <audio> <beats.json> <out.wav> [--grid beat|downbeat|half|hit] [--music-db -6]
```

## beats.json

- `bpm`, `bpm_confidence` (share of tracked beat intervals within 5 % of the median), `tempo_candidates` [{bpm, strength}] (half / double tempo shows here; `--bpm` overrides).
- `beat_source`: `steady` = one straight line (`fit.t0`, `fit.period`) extended over the whole range; `tracked` = librosa's beats refined to the onset peak. Auto picks steady when a robust fit explains ≥ 70 % of tracked beats, spread over ≥ 80 % of the piece, within 15 ms rms. `fit.off_grid` = [[start, end]] where tracker and grid disagree (tracker slips, or a real tempo change: listen).
- `meter`, `bar_phase` (index of the first downbeat in `beats`), `downbeats`, `downbeat_confidence` (share of 400 bar resamples that keep the phase), `downbeat_alt` (first downbeat of the runner-up phase).
- `hits` [{t, strength 0–1 vs the 98th percentile of onset peaks, on: downbeat|beat|half|off}], kept from `--hit-min` (0.5).
- `sections` [{start, end, bars, level_db, rise_db, novelty}] for ≥ 16 bars of music; boundaries on downbeats. A section with a large `rise_db` is the drop.
- `start` / `end` = analysed range (`start` = file time of the first decoded sample, so a video whose audio starts late is handled), `analyzer` = versions, params, seconds. Rerun with the same params and a newer output = no-op (`--force`); a change to the script forces a recompute.

## How it hears (defaults and why)

- Onset envelope: log-mel flux, n_fft 1024, hop 256 (11.6 ms), no centering shift. librosa's default (2048, centered) put beats 21–41 ms late on a drum stem and a synth loop; this setting is within ±7 ms.
- Tempo: the period whose 1/2/4/8-beat lags all repeat in the onset autocorrelation, times a log-normal prior centered on 110 bpm. librosa alone read 132.5 on a 100 bpm loop whose arp runs in dotted eighths. The prior settles half vs double tempo, so a 170 bpm track reads 85: the candidates are printed, pass `--bpm` (sample-pack names usually carry it).
- Downbeat score per beat: low-band (< 200 Hz) onset + chord change (2 beats before vs after) − 0.5 × high-band (> 2 kHz) onset. Snares and claps sit on 2 and 4; a broadband term pulled the choice onto the backbeat. Confidence below 0.9 prints the runner-up and the `--downbeat` override.
- Steady fit: beats numbered from the global period, least squares on inliers (≤ max(3 MAD, 10 ms)), iterated; one pass let 8 slipped outro beats tilt a 78 bpm grid by 28 ms.

## snap

`--grid` is a priority list (`downbeat,beat` = a downbeat within `--max-shift`, else the nearest beat); `half` adds midpoints; `hit` honors `--min-strength`. `--path` is dotted, ints index lists, `*` fans out; `--key t` or `start,end`; bare numbers in the list work. `--offset` = timeline time of music second 0 (a bed trimmed on the timeline). `--fps` rounds to the nearest frame (≤ 16.7 ms at 30 fps; with the ±7 ms beat error the total stays under the 45 ms audio-ahead-of-video detection threshold, ITU-R BT.1359). Writes a new file, refuses to overwrite inputs, warns when two cues land on one point. Cues more than `--max-shift` from the grid stay and are listed.

Snap the frame where a graphic lands (impact, final counter number, cut), never the start of its ease-in. Big entrances on downbeats, counters and chips on beats, accents on `--grid hit --min-strength 0.8`.

## click

Music at −6 dB + 1 kHz beat clicks, 1.6 kHz accented downbeats, 700 Hz half beats, noise ticks for hits scaled by strength; 48 kHz stereo 16-bit WAV over the analysed range. Clicks land within 0.02 ms of the beat times. Listen before building on a grid ("downbeat uncertain" or `fit.off_grid` in the analyze output = listen there).

## Measurements (2026-11)

| Material | Truth | Measured | Beat error |
|---|---|---|---|
| 100 bpm sample-pack loop, 19.2 s | 100 | 100.00 | +5.0 ms mean, max 5.0; downbeat right (conf 0.73 on 8 bars) |
| 100 bpm drum stem, 162 s | 100 | 100.00 | −2.5 ms mean, max 3.0; downbeat right (0.99 on 68 bars) |
| same loop inside an mp4, audio delayed 0.5 s | 100 | 100.00 | +4.0 ms |
| isolated arp stem | 100 | 133 vs 100 tie | +2.0 ms with `--bpm 100` |
| two library beds (wav, mp3; 148 s, 141 s) | ? | 85.00, 78.00 steady | 2.6 / 1.9 ms rms on 77 % / 98 % of beats; slipped stretches flagged |

HyperFrames 0.9.19 has its own tracker (`hyperframes beats <file> --json`, `hyperframes audio <file> --only beats,downbeats,bpm,sections --json`), local "signal" provider on the same truth: loop 100.1 bpm, beats +21.5 ms late; drum stem 100, +12.3 ms, downbeats one beat off; arp stem 133.3; the mp4 whose audio starts at 0.5 s → no beats. Fine for a quick bpm read; use beats.py for cue timing. Its neural provider (`HYPERFRAMES_BEAT_THIS_PYTHON`, beat-this) was not tested.

A `--start 20 --end 60` window matches the full-file grid within 1.0 ms. Runtime: 2–5 s per track, cached rerun 0.45 s; first run ~17–35 s (wheels ~100 MB, numba JIT). Python 3.11 resolves librosa 0.11, 3.12+ librosa 1.0; results identical on 3.11, 3.13, 3.14. Numba raises on Windows when a cache path passes 260 characters: beats.py moves its cache to the temp folder when the install path is long. Section boundaries are not yet checked against ground truth.
