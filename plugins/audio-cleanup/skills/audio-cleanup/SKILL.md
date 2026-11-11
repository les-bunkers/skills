---
name: audio-cleanup
description: Clean up, enhance, denoise, level and master the speech of a DaVinci Resolve edit. Reads what the timelines actually play, optionally splits two speakers sharing one source, runs NVIDIA RE-USE speech enhancement on the GPU, masters to -14 LUFS (compressor + oversampled limiter), checks loudness, true peak, AAC, sync and dropouts, places "RE-USE" and "MASTER −14 LUFS" tracks back on the timelines and builds an A/B listening page. Use when asked to clean up, enhance, denoise, compress, normalize or master dialogue or podcast audio on a Resolve timeline, make a cut -14 LUFS, split two speakers before cleanup, enhance one recording with RE-USE, or build the audio A/B page.
argument-hint: "[job folder] [timeline name ...]"
---

Speech cleanup for any shoot edited in DaVinci Resolve. Everything runs locally (no paid calls); source media is
never modified; every output lands in a job folder.

Plugin files: `${CLAUDE_PLUGIN_ROOT}/scripts/` (Claude Code substitutes the path, forward slashes on Windows). If
the text still shows `${CLAUDE_PLUGIN_ROOT}` literally, Glob `**/audio-cleanup/scripts/cleanup.py` and use its
folder. References live in `${CLAUDE_PLUGIN_ROOT}/references/`: install, GPU and speed figures in
[setup](../../references/setup.md), file formats and every manifest field in [job files](../../references/job-files.md).

## Before the first run on a machine

Needs: ffmpeg on PATH, uv, an NVIDIA GPU with a CUDA venv, the RE-USE download, pyannote community-1 (only for the
speaker split), DaVinci Resolve Studio with Blackmagic's Resolve MCP. Steps in [setup](../../references/setup.md).
The scripts find the GPU pieces through env vars, `<job>/job.local.json` keys, or one home folder:

| Env var (job.local.json key) | What | Default |
|---|---|---|
| `AUDIO_CLEANUP_HOME` (`home`) | folder holding `venv-gpu/` and `models/` | none |
| `AUDIO_CLEANUP_REUSE_PYTHON` (`reuse_python`) | python of the GPU venv | `<home>/venv-gpu/…/python` |
| `AUDIO_CLEANUP_DIAR_PYTHON` (`diar_python`) | python with pyannote.audio | the RE-USE python |
| `AUDIO_CLEANUP_REUSE_DIR` (`reuse_dir`) | `hf download nvidia/NVIDIA-NemotronLabs-RE-USE-laughter` folder | `<home>/models/RE-USE` |
| `AUDIO_CLEANUP_PYANNOTE` (`pyannote`) | community-1 folder or HF id | `<home>/models/speaker-diarization-community-1`, else the HF id + `HF_TOKEN` |
| `AUDIO_CLEANUP_GPU_LOCK` | lock dir shared with other GPU jobs | no lock |

Precedence: CLI flag, `job.local.json`, `job.json`, env, home defaults. Quick check:
`"<gpu python>" "${CLAUDE_PLUGIN_ROOT}/scripts/reuse_file.py" --selfcheck` prints `"ok": true`, and the Resolve MCP
tool `get_resolve_status` answers. Missing pieces → walk the user through setup.md; never install CUDA drivers or
accept a model license on their behalf.

## Run

1. **Job folder**, one per edit, on a drive with room: ≈ 2 × the 48 kHz float size of every source used (an hour
   of stereo source = 1.4 GB) plus ~690 MB per hour per output file (RE-USE, master, one per split speaker).
   Example: `<project>/audio-cleanup`.
2. **Plan** in Resolve (MCP `run_script_unsafe`; reads the project, changes nothing):
   ```python
   ACTION = "plan"; JOB = r"<job>"; TIMELINES = ["<name or substring>"]; TRACK = 1
   exec(open(r"${CLAUDE_PLUGIN_ROOT}/scripts/resolve_cleanup.py", encoding="utf-8").read())
   ```
   Writes `<job>/plan.json`: every item of audio track TRACK (the dialogue track) with file, record frame and
   source in/out at the clip fps. The result lists items, skipped, sources and used seconds per timeline.
   Skipped > 0 = multicam, compound or generated items (no file path): plan the timeline cut from the plain
   files, or decompose the multicam first. Re-planning a timeline replaces only that timeline.
3. **job.json** (optional, `<job>/job.json`):
   ```json
   {"split": ["<path substring of a two-speaker source>"],
    "stt": [{"match": "<path substring>", "words": "<file>.words.json", "names": {"0": "Host", "1": "Guest"}}],
    "language": "en"}
   ```
   - `split`: a source that carries two voices on one track (a finished podcast mix, one room mic). `"all"` =
     every source. One voice, or one mic per voice on separate tracks → no split: plan each mic's track instead.
     `--split <substr> …` on the command line overrides it.
   - `stt`: a transcript with speaker ids in the shared Words format (`*.words.json`, source seconds, any STT
     that labels speakers) → real speaker names and the split accuracy check. Without it the tracks are
     "speaker A" (most talk) and "speaker B" and the split is judged by ear only.
   - `language`: Whisper language for the A/B page (else auto-detect). `target_lufs`: default -14.
4. **Clean** (background for anything over a few minutes, log appended to `<job>/run.log`):
   ```bash
   uv run "${CLAUDE_PLUGIN_ROOT}/scripts/cleanup.py" "<job>" all
   ```
   Steps in order: prepare → diarize → split → quality → reuse → finish → master → scan; any one alone:
   `cleanup.py <job> <step>`. Reruns skip finished work: extraction (source unchanged), region files (same
   ranges), diarization (`work/*.diar.json`), split (same turns), RE-USE outputs (delete `work/*.reuse.wav` to
   force). RTX 5090: RE-USE ≈ 4.6× realtime per track over the used ranges only; diarization of a whole 68 min
   source 80 s; 3 sources with 1.9–5.6 min used took 2.6–3.0 min end to end; a rerun with nothing changed 0.6 min. Long runs: check the log every ~15 min, since a
   stalled GPU job never sends a completion notice.
5. **Check**: `python "${CLAUDE_PLUGIN_ROOT}/scripts/check_job.py" "<job>"` → every line PASS (INFO lines are
   counts). Fail actions:
   - `lag` ≠ 0: RE-USE output moved against the source. Delete that source's `work/*.reuse.wav`, rerun `reuse`
     onward; never place a lagging track.
   - `loudness` / `aac` / `timeline`: rerun `master`. The master step aborts when the solved gain runs > 6 dB past
     plain gain-to-target: the compressor bites too hard for this material (long quiet passages, very dynamic
     speech) → review `COMP` in cleanup.py with the user.
   - `split` > 4 % wrong-track words (reference runs 0.5–2.6 %) → [speaker split](../../references/speaker-split.md).
   - `dropouts`: RE-USE dips inside the cut, with timeline timecodes in `scan.json`; listen to them on the A/B page.
6. **Place** in Resolve (changes the project: tracks + one bin). Run it when the user wants the tracks on the
   timelines:
   ```python
   ACTION = "place"; JOB = r"<job>"; ENABLE = "none"   # optional: TIMELINES = [...], SPLIT_TRACKS = True
   exec(open(r"${CLAUDE_PLUGIN_ROOT}/scripts/resolve_cleanup.py", encoding="utf-8").read())
   ```
   Adds stereo tracks "RE-USE" and "MASTER −14 LUFS" (+ "RE-USE — <speaker>" with SPLIT_TRACKS) mirroring the
   plan's TRACK items; media in bin "Audio cleanup — <job folder name>". The report must show
   `position_mismatches` 0 and `max_source_time_error_s` 0.0 for every track. `ENABLE="none"` (default) leaves
   every existing toggle alone and adds the new tracks disabled, so toggle-based render checks by others keep
   working. `ENABLE="master"` turns the master on and every other audio track off: only when the user wants the
   master audible. Restores the open timeline and page. Re-running replaces those tracks. The MCP call caps at
   60 s: long timelines → one timeline per call (`TIMELINES=[…]`).
7. **A/B page**:
   ```bash
   uv run "${CLAUDE_PLUGIN_ROOT}/scripts/ab_page.py" "<job>" [--max-spots 60]
   python -m http.server 8790 --bind 127.0.0.1 --directory "<job>/ab-page"     # run in the background
   ```
   Give the user `http://127.0.0.1:8790/` and the server PID; stop it by that PID only. Every step of each spot
   plays on its own and switching steps keeps the playhead. The page holds the speakers' audio: keep it local
   unless the user decides otherwise. First build downloads Whisper large-v3 (~3 GB) and spends ~10 s per
   transcription on CPU (3 per spot, cached); `--no-asr` builds in seconds without text.
8. **Approval by ear.** The user listens and approves before any delivery. Finals render from Resolve with the
   MASTER track on. After the edit changes: plan again → `all` → place (the master gain is solved on the cut).

## Enhance one recording (no Resolve)

```bash
"<gpu python>" "${CLAUDE_PLUGIN_ROOT}/scripts/reuse_file.py" in.wav --out out.wav --reuse-dir "<RE-USE dir>"
```
Any input ffmpeg reads; output 48 kHz mono float WAV with the same length and time origin, plus `out.json`
(speed, VRAM). Float output can exceed 0 dBFS: measure true peak and apply gain before encoding.

## Defaults and why

- Plan from Resolve, never from an exported cut list: cut lists go stale (one edit had its timelines trimmed in
  Resolve to 65 and 35 of the 98 and 61 pieces the list still named).
- Source audio = first audio stream, 48 kHz float, channels averaged to mono; sample 0 = stream time 0, same
  sample count → the same in/out frames as the original clip.
- RE-USE only on the used ranges + 2 s handles, merged within 5 s, glued with 5 s digital silence: dynamics never
  cross regions, and RE-USE's onset overshoot after silence falls in the handle, outside the cut. Scalar gain
  only: RMS-matched to the source, capped at −2 dBTP.
- Master chain: highpass 60 Hz → gain → compressor (threshold −10 dBFS at a −14 target, i.e. on the loudest ~10 %
  of syllables, 3:1, knee 4, attack 5 ms, release 120 ms, peak detection) → 4× oversampled limiter −2 dBFS
  (no auto-level, latency compensated). The gain is solved (secant) so the source's audio AS PLAYED on the
  timelines reads the target, measured as dual mono on a stereo track (+3 dB against the mono file), which is
  how Resolve plays it. A −20 dBFS threshold crushed the speech and ran the gain to +51 dB, lifting pauses
  ~50 dB: hence the 6 dB abort. Reference finals on AAC: −14.02 to −14.04 LUFS, −1.89 to −1.93 dBTP, LRA 3.6–5.3.
- Resolve media = mov with a 64×36 black video at the clip fps + dual-mono 24-bit PCM: Resolve reads WAV frame
  numbers at the project rate and refuses `SetClipProperty("FPS")`, and a mono clip on a stereo track plays
  −3 dB. Verified at 23.976 and 30 fps: source-time error 0.
- Two speakers: diarize the WHOLE source (pyannote community-1, 2 speakers), then cut the turns to the regions:
  regions-only diarization put 3–15× more words in the wrong track. Lossless gain-gated split (tracks sum to the
  mix), other voice ducked −30 dB, never muted (silence before a turn makes RE-USE overshoot the onset by
  10–15 dB). Overlapping speech stays in one track. Details: [speaker split](../../references/speaker-split.md).
- Never ride level into RE-USE (compressor or fixed gain before, undone after): tested, it lost more words than it
  saved. RE-USE behaviour, dropouts and license: [RE-USE notes](../../references/reuse.md).

## Limits

- Multicam, compound and generated items are skipped (no file path).
- One audio stream per source, channels averaged: separate mics belong on separate timeline tracks (plan that
  track) instead of a split. The split handles exactly two speakers; overlapping speech and backchannels stay in
  the owner's track and RE-USE may remove the second voice there.
- One fps per source file. RE-USE needs an NVIDIA GPU (no CPU mode); macOS can plan, place and build pages only.
- Default weights = NVIDIA NemotronLabs RE-USE-laughter (OpenMDW-1.1, commercial use allowed, outputs
  unrestricted; it dropped the fewest words in a three-checkpoint test). Plain NemotronLabs RE-USE (same license)
  runs the same way. The older `nvidia/RE-USE` checkpoint is non-commercial only (NSCLv1): point `reuse_dir` at it
  only for non-commercial work. Details: [setup](../../references/setup.md) §3, [reuse](../../references/reuse.md).
- Technical checks never prove the words survived: RE-USE can suppress a word while sync and loudness pass. The
  A/B page and the user's ears are the gate.
