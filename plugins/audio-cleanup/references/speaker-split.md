# Speaker split: method, settings, audit

Why two voices on one source get split before RE-USE, how the split works, and how it was validated. Measured on
two French podcast episodes (one finished stereo mix each, host + guest, voices ~195 Hz and ~107–112 Hz median
pitch).

## Why split

RE-USE treats a second voice talking over the first as interference and removes it. Cleaning each speaker alone,
then summing, keeps both voices wherever they alternate.

## Method

1. **Diarize the whole source** (pyannote speaker-diarization-community-1, `num_speakers=2`, audio decoded to
   16 kHz mono and passed in memory), then cut the turns to the used regions.
2. **Name the speakers**: with an STT transcript carrying speaker ids, each diarization label takes the STT
   speaker it overlaps most (time-weighted); `stt_agreement` = share of overlap time that matches. Expect
   ≥ 95 %. Without STT: "speaker A" = most talk inside the cut, "speaker B" the other.
3. **Gain-gated lossless split**: one owner per sample from the exclusive diarization; turns < 120 ms absorbed by
   their neighbours; each speaker change cut at the quietest 10 ms frame between the turns (searching ±50 ms
   when the turns touch), with a 20 ms complementary raised-cosine crossfade. Track gains sum to 1, so speaker
   A + speaker B = the mix (measured max error ~3e-8).
4. **Duck the other voice −30 dB, never mute it.** Digital silence before a turn makes RE-USE overshoot each
   onset by 10–15 dB for ~100 ms. Side effect: the other voice is faintly audible when one speaker's track is
   soloed.
5. RE-USE on each speaker track; each result RMS-matched to its own share of the mix; one common gain caps the
   loudest at −2 dBTP; the sum becomes the "RE-USE" track.

## Accuracy check without ground truth (quality step)

Praat pitch (parselmouth autocorrelation, 10 ms, 60–400 Hz) on the region mix; frames under −45 dBFS unvoiced.
Each STT word with ≥ 8 voiced frames gets its median F0. Speaker medians come from those words; cut lines sit at
30 % and 70 % of the gap between the medians. Only words where the pitch side and the STT speaker agree are
counted, and each is scored by the track that holds most of it. `wrong_share` = those words found in the other
speaker's track.

- Pitch alone is noisy (10–17 % of a speaker's frames fall outside their band: octave jumps, vocal fry), so it
  never decides alone; STT alone mislabels backchannels. Agreement of both is the reference.
- Skipped when the two medians are < 40 Hz apart: similar voices → listen instead.
- Reference: 2.6 % and 0.5 % wrong-track words on the two episodes. Pass mark ≤ 4 %.

## Settings and the measurements behind them

- **Whole source, not regions only.** Diarizing only the used regions (no context) put 6.7 % / 5.9 % of agreed
  words in the wrong track (one speaker's speech handed to the other: 190 s against 132 s); the whole episode
  gave 2.4 % / 0.4 %. The difference is context, not configuration: community-1 defaults (VBx threshold 0.6,
  Fa 0.07, Fb 0.8, min_duration_off 0, embedding_exclude_overlap true); asking for 2–3 speakers gives the same
  result; the split post-processing adds ~1 word.
- **Diarization cost**: ~50 s / 30 s on an RTX 5090 for the two episodes (~1 min per hour of source).
- **Overlap and backchannels** ("yes", "right" under the other speaker) stay in the owner's track, and RE-USE may
  drop the second voice there (heard in the A/B). Gating cannot fix it. A possible next lever, not built:
  crossfade to the original mix over the diarization overlap (`diar_full`; ~48 s and ~19 s on the two
  episodes).
- **Riding level into RE-USE does not help.** A compressor (−20 dBFS target) and a fixed gain before RE-USE, each
  inverted afterwards: the compressor lost more words than it saved, the gain changed nothing.
- **Separate mics beat diarization.** When each voice has its own mic track, put each mic on its own timeline
  track and plan that track; no split.

## When the score is bad (> 4 %)

1. Diarization input not the whole source (the region file was diarized instead): check `work/<key>.diar.json`
   `audio_s` = the source length.
2. Labels swapped: `stt_agreement` well under 0.5, or the `names` mapping in job.json does not match the STT ids.
3. A third voice (guest call-in, audience): the split handles two; plan around it or skip the split.
4. Similar voices: the score is unreliable; judge on the A/B page, Split step.
