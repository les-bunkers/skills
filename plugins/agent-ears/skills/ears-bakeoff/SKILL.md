---
name: ears-bakeoff
description: Test a breath or filler detector, threshold, prompt, model or new take against a human's ears — union of detector candidates, blind labelling page on localhost, stratified scoring with a holdout, one table of precision / recall / speech cuts / edge error. Use when asked whether a detector (PE-A-Frame, Respiro, Scribe, a DSP rule, a new model) finds breaths or « euh » well enough, to compare detectors or thresholds, to check agent-ears on a new language, voice or room, to label an answer key, or to re-score after more labels.
argument-hint: "<take> [job folder]"
---

A claim like "X finds breaths better" holds only against labels a human made blind. This kit makes ~100 labels enough: label a stratified sample of what any detector would cut, weight each label by its stratum, keep a holdout out of the pick. Formats of every file: [bakeoff files](${CLAUDE_PLUGIN_ROOT}/references/bakeoff_files.md). Measured results so far: [measurements](${CLAUDE_PLUGIN_ROOT}/references/measurements.md).

`S="${CLAUDE_PLUGIN_ROOT}/scripts"`. GPU scripts run with `$EARS_PYTHON` ([setup](${CLAUDE_PLUGIN_ROOT}/references/setup.md)); the rest with `uv run` (or that venv).

## Do

1. **Job folder** = one take, in the project (not the media folder). The take needs a Words transcript first (`/agent-ears` step 4 makes one with Scribe; any `*.words.json` works).
   ```bash
   python "$S/ears_job.py" init <job> --audio <take> --words <take>.scribe.words.json --name "a 10-min French take"
   ```
   Writes `bakeoff.json` (name, strata, weak euh sources) and `bakeoff.local.json` (the machine paths; never commit it).
2. **Detectors.** Each one leaves a file in the job:
   - PE-A-Frame, 10 prompts (breath ×2, euh, mouth ×2, handling ×2, laugh, throat, a speech control): `"$EARS_PYTHON" "$S/run_peaframe.py" <job>` → `probs_peaframe.npz`. `--prompts my.json` tests other prompts.
   - Respiro-en (optional, English-trained): `"$EARS_PYTHON" "$S/run_respiro.py" <job> --respiro-dir <clone>` → `probs_respiro.npz`.
   - The Words file's fillers join automatically as detector `words` (type euh).
   - Anything else (a DSP rule, another model, an API): write `spans_<name>.json` in the [spans format](${CLAUDE_PLUGIN_ROOT}/references/bakeoff_files.md). Type `speech?` = voice heard with no transcript word: those candidates get labelled in full as the safety check.
   Same GPU rule as `/agent-ears`: ≥ 4 GB free RAM, ≥ 8 GB free VRAM.
3. **Candidates.** `uv run "$S/union.py" <job>` → `candidates.json`. Every breath / euh / speech? span at each detector's low union threshold, merged when within 30 ms, split above 2 s at the quietest hop. Prints counts per source combination and per stratum. Recall later = share of what this union found, so include every detector you want to judge.
4. **Labels.**
   - Free port first: `netstat -ano | grep :8791` (Windows: `findstr`). Busy → another `--port`. The server refuses a busy port instead of sharing it.
   - Preview: `uv run "$S/key_server.py" <job> --preview 20 --port 8791` (run in the background), give the human `http://127.0.0.1:8791/`. They mark ~20 and confirm the format works.
   - Then the sample: stop the server by its printed PID (never by process name), restart with `--sample`, and tell the human to **reload the tab**: an open old tab keeps posting its old set. `--sample` = every `euh_claimed` and `speech?` candidate plus 15 weak-euh-only, 10 / 30 / 20 breath candidates flagged by 3+ / 2 / 1 detector families (bakeoff.json `strata`), already-labelled ids counting toward their quota. `--ids 3,8,12` = a targeted top-up.
   - Page: blind (no detector shown), spectrogram 0–8 kHz with the words lane. Keys: Space play with 1 s of context, B band only, 1–5 = breath / euh / other noise / speech / nothing (several allowed), Enter / → next, ← back, U next unmarked. Dragging an orange band edge records where the sound really is (R resets). Autosaves to `key.json`.
   - The human labels in their own time; don't wait in a loop. Time-ordered blocks are fine to score.
5. **Score.** `uv run "$S/score.py" <job>` → `score.md` + `score.json`. Show the picks table and say which setting wins, by how much, and whether it beats the current default (PE-A-Frame @ 0.95, raw, no word filter).

## How the score works

- **Claim rule.** A setting claims a type on a candidate when one of its spans overlaps the band by ≥ half of the shorter of span and band. A span only grazing the band (e.g. ending 50 ms inside the next band) is a *touch*, reported apart. The human's dragged band, when moved, is the truth.
- **Speech cuts are disqualifying.** "speech" = voice that must never be cut. The pick per family is the best dev F1 with **zero** speech claims on dev. The second table shows what that rule costs.
- **Stratified weights.** Each labelled candidate counts N_s / n_s for its stratum, so a sample of ~100 stands for all ~350 candidates of a 10-min take. P / R / F1 are weighted; tp / fp / fn are raw counts.
- **Holdout.** 25 % of the labels per stratum (seeded) never take part in picking a setting; the pick is reported on dev, holdout and all. Trust the holdout column: a dev-only win that drops on holdout is noise.
- **Budget.** ~100 marks gives scores ±8–10 points. Two settings closer than that are a tie: label more (`--sample` again tops up) or call it a tie.
- **Edge error.** Median |start| / |end| between the claiming span and the dragged band, breath-only candidates: how far a cut placed on the span would miss.
- **Votes.** k-of-N over the picked breath settings. On the measured take no vote beat PE-A-Frame alone.
- **s-tail FP.** False claims starting within 80 ms of a word ending in s / x / z: the word-final « s » hiss read as breath.

## Measured (one 10-min French take, 116 labels by the editor)

breath 62, euh 20, speech 28, nothing 16, noise 8. P / R stratum-weighted, recall vs the union of detectors.

| Detector | Breath P / R | Holdout P / R | Speech cut | Edge err start / end |
|---|---|---|---|---|
| PE-A-Frame "a person inhaling sharply" @ 0.95, raw, no word filter | 0.98 / 0.93 | 0.91 / 0.91 | 0 (1 touch) | 40 / 20 ms |
| same @ 0.98 | 1.00 / 0.91 | F1 0.95 | 0 (0 touch) | |
| Respiro-en raw @ 0.7 | 0.89 / 0.84 | 0.80 / 0.85 | 0 cut / 2 touch | 50 / 40 ms |
| DSP gap classifier | 0.91 / 0.21 | 1.00 / 0.25 | 0 | 70 / 80 ms |
| 2-of-3 vote (DSP, Respiro, PE) | 0.97 / 0.82 | 0.90 / 0.82 | 0 (1 touch) | 60 / 60 ms |

« euh »: Scribe v3 fillers 20 / 20 under the claim rule (P 0.84, but 2 speech claims, so they never pass the pick rule as a band detector: cut euh between the neighbouring words instead); a Grok transcript 0.67 / 0.34 with 5 speech claims; DSP held-vowel 1.00 / 0.10; PE's euh prompt ≤ 0.42 P. Word filter on PE: recall 0.93 → 0.69. High-pass 80 Hz: no gain for PE, worse for Respiro. More in [measurements](${CLAUDE_PLUGIN_ROOT}/references/measurements.md).

## Rules

- Never show the labeller which detector flagged a candidate, and never pick a setting on holdout labels.
- Report a result with its label count and the take it came from; one take is one voice, one room, one mic.
- A new language or English speaker: check the filler list in `scribe_to_words.py` and re-run the bake-off before trusting the defaults.
