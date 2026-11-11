Where the agent-ears defaults come from. One bake-off, one voice: re-test with `/ears-bakeoff` before trusting a number on other material.

## Breath bake-off

Material: one 10-min French talking-head take (one speaker, studio mic, quiet room). 359 candidates = union of three detector families: a DSP gap classifier (silence floor, periodicity, spectral centroid between transcript words), Respiro-en, and PE-A-Frame with 9 sound prompts. 116 blind labels by the editor (breath 62, euh 20, speech 28, nothing 16, noise 8; several marks per band allowed), stratified sample, 25 % holdout. P / R stratum-weighted; recall relative to what the union found.

| Detector | Breath P / R | Holdout P / R | Speech cut | Edge err start / end |
|---|---|---|---|---|
| PE-A-Frame "a person inhaling sharply" @ 0.95, raw audio, no word filter | 0.98 / 0.93 | 0.91 / 0.91 | 0 (1 touch) | 40 / 20 ms |
| same @ 0.98 | 1.00 / 0.91 | F1 0.95 | 0 (0 touch) | |
| Respiro-en raw @ 0.7 | 0.89 / 0.84 | 0.80 / 0.85 | 0 cut / 2 touch | 50 / 40 ms |
| DSP gap classifier | 0.91 / 0.21 | 1.00 / 0.25 | 0 | 70 / 80 ms |
| any of the 3 (1-of-3 vote) | 0.88 / 0.97 | 0.82 / 1.00 | 0 (2 touch) | 55 / 60 ms |
| 2-of-3 vote | 0.97 / 0.82 | 0.90 / 0.82 | 0 (1 touch) | 60 / 60 ms |
| Respiro AND (DSP OR PE) | 0.97 / 0.81 | 0.89 / 0.76 | 0 (1 touch) | 40 / 40 ms |

- Threshold plateau 0.95–0.98 for PE. 0.95 = the default (more recall); 0.98 when a missed breath costs less than a wrong cut.
- No vote beat PE alone (best vote F1 0.92 vs 0.95).
- High-pass 80 Hz: PE identical (F1 0.95 both), Respiro worse (0.81 vs 0.86, and one speech cut).
- Word-contiguity filter (drop the part of a span touching a word): PE recall 0.93 → 0.69, Respiro 0.84 → 0.53. Breaths run straight into the next word's onset; the cut rule (near-silent dips) is the guard, not a filter on the detector.
- PE "a man speaking" (control prompt) reads 1.0 even in silent gaps: useless as a speech guard. PE "a filled pause, someone saying uh" fires on breaths: best euh P 0.42 / R 0.10.
- Respiro (trained on English LibriTTS-R) claims breath on French speech at every threshold: always at least 2 speech touches.
- Gap level: removing PE breaths also removes 15 of the 20 labelled euh (a breath and an euh share the gap), 58 / 62 breaths, 0 speech, 1 of 22 nothing / noise.
- Breath density: 9–18 PE breaths per minute (median 14) over 20 takes of 1–22 min (talking head, podcast, demos).

## « euh » (fillers)

| Source | Labelled euh heard (of 20) | Notes |
|---|---|---|
| ElevenLabs Scribe v3, verbatim | 19 raw hits; 20 under the score.py claim rule | weighted P 0.84 / R 1.00 but 2 speech claims (a 0.12 s band right after an euh; an euh inside a 1.85 s band marked speech) |
| Grok speech-to-text transcript fillers | 12 raw | weighted P 0.67 / R 0.34, 5 speech claims |
| DSP held-vowel rule | | P 1.00 / R 0.10 |
| PE euh prompt | | P ≤ 0.42 |

- Counts: Scribe 47 euh on the 10-min take (Grok 16), 139 on a 22-min take.
- Timing: Scribe words land within ±4 ms (median; p10 / p90 −0.02 / +0.03 s) of Grok's on the same audio: same time base. Scribe's euh spans are short (median 0.14 s), sit inside the vowel and end 0.1–0.2 s early; the neighbouring words' start / end are right (checked on spectrograms). So an euh is removed from the previous word's end to the next word's start, never by its own span. Grok's filler times are off by up to 0.34 s (several 0.06 s "euh").
- Scribe audio events seen: `[rire]`, `[rire léger]`, `[toux]`, `[soupir]`, `[reniflement]`, `[claquement de langue]`, `[clappement]`. None tags a breath. Event spans are loose (a `[toux]` over 2.4 s including silence).
- Scribe spells « hein » (Québec tag word, kept) and « eu » (avoir) apart from « euh ». `no_verbatim` strips fillers AND events.
- Speed: ~9 s for 10 min of audio.

## What a tighten pass got out of it (one 19-min French edit)

- « euh »: 127 Scribe euh inside kept segments. 26 were already cut from the other transcript; cutting previous-word end → next-word start on a clean or soft dip took 46 more; 46 glided voiced into the next word (« Euh, je ») with no dip. A splice inside the voicing (level valley ≥ 6 dB deep within −60 / +20 ms of the next word's start, else maximum spectral flux) with a 1-frame equal-power cross fade passed a blind listening test ("clean") against hard cuts, which clicked. Result: 118 of 127 out; 9 kept (7 where the two transcripts disagree on a word, 2 with unexplained voice beside them).
- Breaths: 214 of 331 PE breaths in the kept segments stayed: ~185 glued to a word with no near-silent dip, 12 inside stretched transcript words, the rest at segment edges, word tails or beside unexplained voice. Open lever: cut at the voicing onset minus a margin calibrated on the labeller's dragged edges, with unvoiced word-initial consonants (s, f, ch, p, t, k) as the risk.

## Models considered

- Not tried yet: NVIDIA Nemotron 3.5 Transcribe (its tokenizer has `<ev:breath>` and filler tags; needs vLLM + NeMo, ~21 GB weights; check its license first), MOSS-Audio-8B as a clip judge, ZIPA phone recognition (swallowed syllables), BEATs AudioSet tags as a second vote. Score any of them with a `spans_<name>.json` file.
- Rejected: CrisperWhisper, FrWhisper, Audio Flamingo Next, DiariZen (non-commercial licenses); Qwen3.5-Omni (no open weights); Voxtral Transcribe V2 (closed); PANNs (clip-level tags); SAM Audio (separation, not detection); Montreal Forced Aligner (aligns the phones you expect, can't find unexpected ones).
