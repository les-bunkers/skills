# Setup

Tested on Windows 11 with ffmpeg 8.0.1, uv 0.12, Python 3.13, Node 24, HyperFrames 0.9.19. Nothing here is Windows-only.

## ffmpeg

On PATH, or set `FFMPEG` / `FFPROBE`. Use a full build: the master needs `lut3d`, `alimiter`, `acompressor`, `ebur128`, `loudnorm` and the `soxr` resampler (`--enable-libsoxr`). A build without soxr fails the audio step.

## Python and uv

- Python 3.11+. `vc_config`, `stt_reconcile`, `build_vertical`, `run_batch`, `covers`, `review_page`, `qc_report`, `export_finals` are stdlib only: `python script.py` works.
- `clip_plan.py` (OpenCV, numpy) and `clip_master.py` (numpy, soundfile) carry PEP 723 metadata; `run_batch.py` runs them with `uv run --script`, which builds a cached env on the first call (~40 MB for OpenCV). Install uv: https://docs.astral.sh/uv/ (`winget install astral-sh.uv`, `brew install uv`, or the install script). `UV` overrides the command.
- OpenCV prints `setPreferableTarget … Targets are not supported by the new graph engine`: harmless.

## Face model (YuNet)

`clip_plan.py` downloads `face_detection_yunet_2023mar.onnx` (227 KB, OpenCV Zoo, Apache-2.0) on first use into `$VERTICAL_CLIPS_CACHE`, else config `cache_dir`, else `~/.cache/vertical-clips/`, and checks its SHA-256 (`8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`). Offline machine: download it elsewhere and drop it in that folder.

```
curl -L -o ~/.cache/vertical-clips/face_detection_yunet_2023mar.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
```

## HyperFrames (captions, hook, render, covers)

```
npm install -g hyperframes@0.9.19
hyperframes doctor            # required checks must pass; optional whisper/TTS/music are not needed
hyperframes telemetry disable # optional
```

Pinned on purpose: three releases shipped within 24 h around 0.9.19. Upgrade between batches only, then render one clip and compare it with an earlier render. The first render downloads a headless Chrome. `HYPERFRAMES` overrides the command (e.g. `npx hyperframes@0.9.19`).

Traps, all handled by the scripts; keep them in mind when editing a project by hand:
- One root composition per project folder (two root HTML files → `multiple_root_compositions`): one folder per look, assets hard-linked into each.
- The root document loads GSAP itself (`<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js">`); without it the check fails with "uses GSAP but no GSAP script is loaded".
- The master needs a keyframe every second (`-g <fps>`), or seeks inside the render freeze on stale frames. `clip_master.py` encodes it that way.
- `hyperframes check --json` writes progress noise to stderr: parse stdout only.
- On Windows the npm install is a `.cmd` shim: call it through the shell from Python (`subprocess.run("hyperframes …", shell=True)`).
- The render output must sit outside the project folder (`--output ../renders/…`), or the render fails.
- Renders take a per-user lock: another session's render queues yours ("Waiting for another render to finish"), which also skews timing.
- `line_unread` (text shown too briefly to read) fires on fast speech captions: expected. Studio-editability warnings are expected too; `check` still returns `ok: true`.

Speed: ~45 s of render per minute of clip per look on a desktop GPU; `hyperframes snapshot` (covers) ~10 s per image.

## RAM

Run one render chain at a time. Two HyperFrames chains plus a running VM once left ~2 GB free and new processes started failing. `run_batch.py` waits up to 10 min for `MIN_FREE_GB` (default 3) of available memory before each render, then skips it.

## raea (optional QC)

When a `raea` binary is found (`$RAEA`, PATH, or `~/.raea/bin/`), `final` runs `raea check <render> --platform reels -o renders/raea-<look>.json` on each render: loudness, true peak and platform flags such as audio that stops dead (exit code 1 = flags raised, still a finished check). Without it QC still has the HyperFrames check, frame counts and the master's loudness.

## OpenAI (paid, optional)

`OPENAI_API_KEY`, and `OPENAI_BASE_URL` for a compatible endpoint (default `https://api.openai.com/v1`; the script posts to `$OPENAI_BASE_URL/audio/transcriptions` with `Authorization: Bearer $OPENAI_API_KEY`). Model `gpt-4o-transcribe`, one call per clip, cached in `<clip>/openai_stt.json`. Without a key, review with `--two-sources`.

## Fonts

Bundled in `scripts/fonts/`, both under the SIL Open Font License 1.1 (license text beside each file):
- Bricolage Grotesque (Mathieu Triay; `BricolageGrotesque-latin.woff2`, the Google Fonts latin subset of the variable font, weights 200–800). Missing from the subset: `Ÿ`.
- Unlock (Eduardo Tunni; `Unlock-Regular.ttf`, unmodified upstream file: the font has a Reserved Font Name, so it ships as published).

Your own fonts: config `fonts` + a preset's `font` / `cap_font`; ship their license with them.
