## What runs with what

| Script | Run with | Needs |
|---|---|---|
| `ears.py`, `run_peaframe.py`, `run_respiro.py` | the GPU venv below (`$EARS_PYTHON`) | ffmpeg, CUDA GPU (CPU works, slowly) |
| `run_scribe.py` | `uv run` (PEP 723: requests) | ffmpeg, ffprobe, `ELEVENLABS_API_KEY` |
| `union.py`, `score.py` | `uv run` (PEP 723: numpy) or the venv | ffmpeg |
| `scribe_to_words.py`, `key_server.py`, `ears_job.py` | `python` or `uv run` (stdlib) | ffmpeg for key_server's page audio |

ffmpeg / ffprobe are found on PATH; `FFMPEG` / `FFPROBE` env vars override.

## GPU venv (Python 3.11)

Tested set: Python 3.11, torch / torchaudio 2.8.0+cu128, transformers 5.22.2, numpy 2.4, soundfile 0.14, librosa 0.11, intervaltree 3.2.

```bash
VENV="$HOME/.venvs/agent-ears"          # any folder; machine paths stay out of the repo
uv venv --python 3.11 "$VENV"
PY="$VENV/bin/python"                   # Windows: "$VENV/Scripts/python.exe"
uv pip install --python "$PY" torch==2.8.0 torchaudio==2.8.0 \
  --index-url https://download.pytorch.org/whl/cu128 \
  --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match
uv pip install --python "$PY" transformers==5.22.2 numpy soundfile librosa intervaltree
export EARS_PYTHON="$PY"                # or {"python": "<path>"} in the project's ears.local.json
```
`librosa` and `intervaltree` are only for Respiro-en (its `modules.py` imports them).

### Traps

- **CUDA wheel index.** Without all three index flags uv takes torch from PyPI: a newer CPU-only build lands and the scripts run on CPU. `unsafe-best-match` lets uv pick each package from whichever index has the best version (the PyTorch index also carries old numpy and friends). cu128 wheels need a recent NVIDIA driver; RTX 50-series cards need cu128 or later. Check: `"$PY" -c "import torch; print(torch.__version__, torch.cuda.is_available())"` → `2.8.0+cu128 True`.
- **transformers `get_text_audio_embeds()`** (5.22.2) calls the text model without `output_hidden_states=True`, so `hidden_states` is None and the call fails. `ears.py` (`PeFrame`) takes forward()'s own path instead: `text_model(..., output_hidden_states=True).hidden_states[-1][:, 0]` → `text_audio_head`. A newer transformers may fix or move this: after an upgrade, rerun the excerpt check below.
- **torchcodec on Windows.** The model card's `perception_models` route (`core.audio_visual_encoder`) imports torchcodec, whose DLLs fail to load on Windows. The scripts use transformers' `PeAudioFrameLevelModel` / `PeAudioProcessor` and decode audio with ffmpeg: don't install torchcodec or perception_models.
- **Weights.** facebook/pe-a-frame-large = 5.5 GB downloaded to the Hugging Face cache on first run (not gated, no `HF_TOKEN` needed). `--hf-home <dir>` (or `HF_HOME`) keeps them on a data drive.

### Resources (measured, RTX 5090)

- VRAM 5.4 GiB (fp32 model + 20 s windows). RAM: keep ≥ 4 GB free before a GPU step (the 5.5 GB weights pass through memory while loading; peak not measured); the decoded take adds ~11 MB per minute at 48 kHz float.
- Speed: ~1 s of GPU time per minute of audio (1.1 s for 1 min, 8.2 s for a 10-min take, 17.6 s for 22 min). Fixed cost per run: ~20 s of Python / torch import plus 4–6 s of model load from a warm cache, so batch files into one call. The 10-prompt `run_peaframe.py` costs the same per variant; `run_respiro.py` ~1–5 s per minute.
- Startup noise to ignore: torch's "Redirects are currently not supported in Windows" and transformers' "`PeAudioProcessor` defines `feature_extractor_class` … deprecated".

### Check an install

Cut 60 s from a take and run `ears.py` on it twice (the second run must say "up to date"). Expect ~9–18 breaths per minute of continuous speech (median 14 over 20 takes). When a full-take `.ears.json` from a known-good install exists, events inside the excerpt (away from its first and last 10 s, where the 20 s windows differ) match it within one 40 ms frame.

## Respiro-en (bake-off only, optional)

```bash
git clone https://github.com/ydqmkkx/Respiro-en   # modules.py + respiro-en.pt (34 MB) are in the repo
export RESPIRO_DIR="$PWD/Respiro-en"             # or pass --respiro-dir
```
16 kHz, 10 ms frames, trained on English LibriTTS-R. Its README's `min_length = 20` counts frames (200 ms), not ms.

## Scribe (paid)

`ELEVENLABS_API_KEY` (sent as `xi-api-key`), `ELEVENLABS_BASE_URL` (default `https://api.elevenlabs.io`, without `/v1`; the script appends `/v1/speech-to-text`). A gateway that adds the key itself can take a placeholder key. Billed per audio minute on the ElevenLabs plan; results are cached as `<stem>.scribe.json`.

## Licenses (checked 2026-11-11; this plugin redistributes no weights)

| Component | License | Source |
|---|---|---|
| PE-A-Frame `facebook/pe-a-frame-large` | Apache-2.0 | Hugging Face model card (metadata + License section); not gated |
| Respiro-en code + `respiro-en.pt` | MIT (© 2024 DongYANG) | `LICENSE` in github.com/ydqmkkx/Respiro-en; trained on LibriTTS-R |
| ElevenLabs Scribe | commercial API, ElevenLabs terms | nothing bundled |
| agent-ears scripts | MIT | this repo |
