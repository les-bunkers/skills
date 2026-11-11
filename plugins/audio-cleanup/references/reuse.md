# RE-USE: behaviour, limits, license

NVIDIA RE-USE (multilingual universal speech enhancement, [paper](https://arxiv.org/abs/2603.02641)) restores
degraded speech (noise, reverb, clipping, band limits, codec artifacts, poor mics) while aiming to keep the words,
voice, accent and emotion unchanged. It targets low distortion at a given perceptual quality, i.e. fewer
hallucinations than generative restorers.

## Checkpoints

| Repo | License | Use |
|---|---|---|
| [nvidia/NVIDIA-NemotronLabs-RE-USE-laughter](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE-laughter) | OpenMDW-1.1 | **default** since 0.2.0: fewest dropouts in the three-checkpoint test below; keeps laughter (card: "better preserves laughter"; VocalSound added to training) |
| [nvidia/NVIDIA-NemotronLabs-RE-USE](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE) | OpenMDW-1.1, "ready for commercial use" | alternative (default in 0.1.x); drops more words on two-speaker audio |
| [nvidia/RE-USE](https://huggingface.co/nvidia/RE-USE) | NSCLv1, non-commercial only | non-commercial work; the listening test and behaviour notes below were made with it |

All three ship the same SEMamba d4 code (the files differ only in their license headers), the same recipe and a
38,592,940-byte `model.safetensors`; the weights differ. The runner handles them identically.

### Three checkpoints on real edits (measured)

French speech, RTX 5090, same runner, each output RMS-matched to its source: 5 one-voice studio takes and 2
two-speaker podcast episodes (mixed to one track, 15.6 min inside the edit). Dropout = 50 ms frames where the
source is louder than −30 dBFS but the output sits ≥ 15 dB under it, merged, ≥ 100 ms, counted inside the edit only.

| | non-commercial | NemotronLabs | NemotronLabs -laughter |
|---|---|---|---|
| dropouts, podcast A (9.0 min) | 13 | 6 | **0** |
| dropouts, podcast B (6.6 min) | 6 | 4 | **2** |
| dropouts, 5 studio takes | 0 | 0 | 1 (short) |
| lag vs source | 0 | 0 | 0 |
| speed | 4.5–5× realtime | 5.7× | 5.6× |

After a blind listening page (original + the three shuffled per spot, 49 spots: every dropout region plus random
moments) the studio chose -laughter; the plain NemotronLabs output was judged "not so good" on the podcasts.

### Plain NemotronLabs vs non-commercial, same input (measured)

59 s of one-voice studio speech, RTX 5090, same runner:

| | non-commercial | NemotronLabs |
|---|---|---|
| speed / peak VRAM | 4.6× realtime / 2.83 GiB | 5.7× realtime / 2.83 GiB |
| lag vs source (3 probes) | 0, 0, 0 | 0, 0, 0 (also 0 against each other) |
| raw RMS vs source (−29.1 dBFS) | −1.1 dB | −0.4 dB |
| band levels vs source after RMS match, 20 Hz–16 kHz | within ±0.15 dB | within ±0.7 dB: +0.4 dB at 3–8 kHz, +0.7 dB at 8–16 kHz (a touch brighter), −0.6 dB above 16 kHz |
| pause floor (10th percentile, 50 ms frames; source −67.8) | −84.7 dBFS | −81.6 dBFS (gates pauses 3 dB less deep) |
| speech level (50th / 90th percentile) | −38.8 / −24.7 dBFS | −38.5 / −24.6 dBFS |
| dropout frames (source > −30 dBFS, output ≥ 15 dB under) | 3 | 3 |

The two outputs correlate at 0.991 after RMS matching (difference signal −17.5 dB under the result), and no 50 ms
frame differs by more than 6 dB between them. On this short one-voice excerpt plain NemotronLabs is a drop-in
replacement; on longer two-speaker material it drops more (table above). The behaviour notes below come from the
non-commercial checkpoint and have not been re-measured on the others.

## Why RE-USE

Listening test (non-commercial checkpoint) on French studio speech (boom and lavalier clips) against Sidon, LavaSR v2, Resemble Enhance
(full and denoise only), a MossFormer2 → Resemble → DeepFilterNet chain, ClearerVoice MossFormer2_SE_48K,
VoiceRestore, DeepFilterNet 3 and DPDFNet: RE-USE sounded best. The generative chains turned noisy sources into
speech-like garble; Sidon and LavaSR only see ≤ 8 kHz and synthesise the whole top band (voice identity can
drift); LavaSR leaves a steady hiss above 8 kHz in pauses.

## How the plugin runs it

5 s chunks, hop 0.5, Hann overlap-add, last chunk aligned to the end, fp32, 48 kHz in and out (the official
chunked inference). Frames whose magnitude is more than half zeros are silenced (the official "sweep artifact"
guard). The runner streams: constant memory, hour-long files work, output sample count and time origin = input.
Measured lag against the source: 0–1 sample over whole files, 0 in the cleanup pipeline's probes.

## Behaviour to expect

- **Dropouts.** RE-USE drops 50 ms to 0.8 s (down to −60 dB) at 10–15 spots inside the cut per podcast episode,
  in every variant tried; mostly non-speech, and Whisper still hears every word in most of them. They depend on
  context: the same spot dipped −6.6 dB when centred in a lone 5 s chunk and −54 dB in the whole-file run. The
  sweep-artifact guard is not the cause (identical with it off). `scan.json` lists them; the A/B page plays them.
- **Words can disappear.** In one test RE-USE lowered a single word by 22.8 dB at matched playback level; three
  ASR engines heard it before and none after, while sync, length and loudness all passed. Only listening (and
  the A/B page's Whisper marks) catches this.
- **Onset overshoot after digital silence**: +10–15 dB for ~100 ms. The cleanup keeps 2 s handles around every
  used range and ducks (never mutes) the other speaker for that reason.
- **A second voice over the first is removed** as interference (see speaker-split.md).
- **Pauses gate hard**: a word end falls to near silence (−12 → −60 dB in ~50 ms). QC tools that flag "sound stops
  dead" fire inside kept segments; none of those flags sat at edits.
- **Output can exceed 0 dBFS** (float). Measure true peak, apply scalar gain (the cleanup caps at −2 dBTP), then
  check the encoded file.
- Dual-mono or stereo input is averaged to mono.

## Speed

RTX 5090 ≈ 4.5–5× realtime; RTX 4070 Ti ≈ 1.0× realtime (2.4 s per 5 s chunk, p95 2.5 s) with another app holding
5 GB VRAM; 2.6 GiB peak VRAM; warm-up ~5 s. Batching chunks (B > 1) hung in the Mamba stand-in; B = 1 only. The
speed lever is the hop: 0.5 processes every sample twice.

## License

- **Default and -laughter: OpenMDW-1.1.** Use, modification and distribution of the Model Materials (weights,
  code, docs) free of charge, commercial use included; no restriction or obligation on outputs. If you distribute
  any part of the Model Materials: include the agreement and keep the copyright and origin notices (including the
  third-party notice list in `LICENSE`). Suing anyone for patent or copyright infringement over the Model
  Materials ends your rights (counterclaims excepted). As is, no warranty; clearing third-party rights is on you.
- **nvidia/RE-USE: NVIDIA One-Way Noncommercial License** (NSCLv1, https://github.com/NVlabs/HMAR/blob/main/LICENSE):
  non-commercial research and educational use only, weights and code; derivatives keep the limit; breaking it
  terminates the license.
- Users download the checkpoint themselves; this plugin redistributes no NVIDIA file and only imports the model
  code from the download. A real-time variant exists (`nvidia/Real-time_RE-USE`, NSCLv1, not used here).
- Permissively licensed enhancers from the listening test (Sidon, MIT; LavaSR, Apache-2.0) are not wired into
  this pipeline.
