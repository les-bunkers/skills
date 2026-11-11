---
name: agent-ears
description: Hear breaths and filler words (« euh », "um", "uh") in a speech recording so an edit can cut them. Breaths come from PE-A-Frame on the GPU (free, local); fillers and audio events (laughter, cough) come from an ElevenLabs Scribe verbatim transcript (paid, asks first, cached). Writes <stem>.ears.json and <stem>.scribe.words.json for the videos tighten stage. Use when asked to detect, find, count, list or remove breaths, inhales, breathing sounds, « euh » / fillers / filled pauses / ums in a talking-head video, podcast, voice-over or any speech file, to run Scribe on a take, or to explain why a breath or euh was or wasn't heard.
argument-hint: "<speech file(s)> [output folder]"
---

Two detectors, one time base: seconds from the start of the file you give them (both decode it from t=0).

| Hears | Tool | Output | Cost |
|---|---|---|---|
| breaths (inhales) | `scripts/ears.py`, PE-A-Frame "a person inhaling sharply" @ 0.95 | `<stem>.ears.json` | free, GPU, ~5.4 GB VRAM |
| « euh » / "um", words, audio events | `scripts/run_scribe.py`, ElevenLabs Scribe v3 verbatim | `<stem>.scribe.json` (raw) + `<stem>.scribe.words.json` | paid per audio minute |

Why these two: a blind bake-off against 116 human labels picked PE-A-Frame for breaths (P 0.98 / R 0.93, 0 speech cut), and Scribe heard 19–20 of the 20 labelled « euh » (depending on the overlap rule) where a Grok transcript heard 12. Numbers and traps: [measurements](${CLAUDE_PLUGIN_ROOT}/references/measurements.md).

## Do

1. **Inputs.** The speech files (audio or video, anything ffmpeg reads) and an output folder: the project or job folder, never the media folder (sources stay untouched). Several files → pass them all in one call: one model load serves every file.
2. **Setup check.** `$EARS_PYTHON` = the GPU venv's python (else read `"python"` from `ears.local.json` in the project). Missing → follow [setup](${CLAUDE_PLUGIN_ROOT}/references/setup.md) (Python 3.11, torch 2.8 cu128, transformers 5.22.2; first run downloads 5.5 GB of weights). Before the GPU step: ≥ 4 GB free RAM and ≥ 8 GB free VRAM (`nvidia-smi`), else wait.
3. **Breaths.**
   ```bash
   "$EARS_PYTHON" "${CLAUDE_PLUGIN_ROOT}/scripts/ears.py" <file …> --out-dir <folder> [--hf-home <weights cache>]
   ```
   Prints breaths per file (expect ~9–18 per minute of continuous speech: median 14 over 20 takes). ~1 s of GPU time per minute of audio plus the model load. Reruns skip up-to-date files. Another threshold → rerun with `--thr 0.98` (stricter: P 1.00 / R 0.91): it re-thresholds from the stored `prob`, no model load.
4. **Fillers: ask first.** Scribe is billed per audio minute. Run without `--yes` to get the length:
   ```bash
   uv run "${CLAUDE_PLUGIN_ROOT}/scripts/run_scribe.py" <file …> --out-dir <folder> --lang fra
   ```
   It prints minutes and upload size and exits 3. Tell the user the minutes and that the result is cached; on a yes, rerun with `--yes`. Needs `ELEVENLABS_API_KEY` (and `ELEVENLABS_BASE_URL` when a gateway sits in front). `--lang` = `fra`, `eng`, … (omit = detect). Several speakers → `--diarize`. An existing `<stem>.scribe.json` is never re-requested. A cached raw file from elsewhere → `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/scribe_to_words.py" <x.scribe.json>`.
5. **Report.** Per file: breaths, fillers (count, runs), events, and the converter's timestamp check line. Name the output files. Cutting is the tighten stage's job (below); don't hand-cut from these lists.

## Outputs

`<stem>.ears.json` (Ears events format):
```json
{"audio": "take.mp4", "model": "facebook/pe-a-frame-large", "prompt": "a person inhaling sharply", "thr": 0.95,
 "hop_s": 0.04, "made": "2026-11-10 14:36", "secs": 8.2,
 "events": [{"t0": 1.8, "t1": 2.24, "type": "breath", "conf": 0.9975}], "prob": [0.012, 0.009]}
```
`events` = spans with prob > `thr` for more than one 40 ms frame (≥ 80 ms); `conf` = peak prob. `prob` = one value per `hop_s`, 3 decimals: move the threshold without the model.

`<stem>.scribe.words.json` (Words format):
```json
{"source": "take.mp4", "language": "fr", "model": "scribe_v3", "time_base": "source",
 "raw": "take.scribe.json", "duration_s": 580.78,
 "words": [{"text": "mais", "start": 12.3, "end": 12.48, "type": "word", "logprob": -0.0012},
           {"text": "euh,", "start": 12.62, "end": 12.78, "type": "filler", "logprob": -0.21},
           {"text": "[toux]", "start": 79.56, "end": 80.78, "type": "event"}]}
```
Fillers: fr « euh heu hum hmm euhm », en "um umm uh uhm er erm hmm hm". « hein » (Québec tag word), « eh », « ben » and « eu » (avoir) stay words: Scribe spells them apart and an editor keeps them. `text` keeps Scribe's punctuation; compare on a lower-cased, accent- and punctuation-stripped form. `speaker` (int) only with `--diarize`. The raw response stays beside it (character timestamps, `_request`).

## What an editor does with them

The `videos` plugin's `/videos:tighten` stage consumes both files. The rules it follows, so you can explain a result:

- **Breaths: cut between near-silent dips, never inside a word.** A breath in a silent gap goes with the gap. A breath glued to the next word (word → inhale → word with no silence) is cut only between two near-silent dips around the PE span edges (≤ floor + 18 dB, within ±80 ms) with almost no voicing between. Most glued breaths have no such dip and stay. Never cut an exhale on a word tail (a final « s » hisses like breath) or a gap where voice was heard but no word transcribed.
- **Don't word-filter PE spans.** Dropping spans that touch a word costs 24 points of recall: real breaths run straight into the next onset. The dip rule above is the guard.
- **« euh »: the boundaries are the neighbouring words' times.** Scribe's euh span is short (median 0.14 s, inside the vowel, ends 0.1–0.2 s early), but the previous word's `end` and the next word's `start` are right. Remove previous-word end → next-word start, cut on a clean dip (else a soft dip). An euh that glides voiced into the next word (« Euh, je ») needs a splice inside the voicing with a 1-frame cross fade. Skip it when another transcript hears a real word between the neighbours.
- **Events are context, not cuts.** Scribe's `[rire]` / `[toux]` spans are loose (a cough tagged over 2.4 s including silence) and never tag breaths: breaths always come from PE.

## Traps

- Time base: both outputs are seconds of the file you passed. If picture and transcript come from different files (camera file vs recorder or switcher output), apply the sync offset the ingest stage measured before cutting.
- Never send Scribe `no_verbatim`: it strips fillers AND audio events.
- Upload guard: FLAC 16 kHz mono ≈ 1.6 MiB per minute, refused above 49 MiB (~30 min) because many gateways cap request bodies at 50 MiB. Split longer files, or `--max-mib 4000` when calling ElevenLabs directly (its own limit is 5 GB).
- The PE prompt "someone saying uh" fires on breaths (P ≤ 0.42): fillers come from the transcript.
- A new detector, threshold, language or recording setup → measure before trusting it: `/ears-bakeoff`.
