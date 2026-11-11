# Setup

One-time install per machine. Everything is local and free. Model licenses: NVIDIA NemotronLabs RE-USE-laughter
(default enhancer) OpenMDW-1.1, commercial use allowed; pyannote community-1 (split only) gated, CC-BY-4.0.

| Script | Runs in | Needs |
|---|---|---|
| `resolve_cleanup.py` | DaVinci Resolve Studio, through the Resolve MCP `run_script_unsafe` | Resolve Studio + MCP |
| `cleanup.py` | `uv run` (PEP 723: numpy, soundfile, praat-parselmouth) | ffmpeg; calls the two below |
| `reuse_file.py` | GPU venv | CUDA GPU, RE-USE download |
| `diarize.py` | GPU venv (or any venv with pyannote.audio) | CUDA GPU, community-1 (split only) |
| `ab_page.py` | `uv run` (PEP 723: faster-whisper) | ffmpeg; Whisper weights download on first use |
| `check_job.py` | any Python 3.11+ | stdlib |

## 1. uv and ffmpeg

- uv: Windows `winget install --id astral-sh.uv -e`; macOS / Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- ffmpeg 6 or newer with libsoxr, libx264 and libmp3lame (full builds have all three): Windows
  `winget install --id Gyan.FFmpeg -e`; macOS `brew install ffmpeg`; Debian / Ubuntu `sudo apt install ffmpeg`.
  Check: `ffmpeg -hide_banner -filters` lists `loudnorm`, `acompressor`, `alimiter`; `ffmpeg -hide_banner -h
  encoder=libx264` answers. Another binary: env `FFMPEG`.
- Python 3.11 is fetched by uv; nothing else goes into the system Python.

## 2. GPU venv (RE-USE + pyannote)

An NVIDIA GPU with a driver for CUDA 12.8 (R570 or newer; RTX 50xx needs the cu128 wheels below) and ≥ 4 GB
free VRAM. Pick a home folder and point `AUDIO_CLEANUP_HOME` at it, e.g. the plugin's data folder
(`${CLAUDE_PLUGIN_DATA}`, survives plugin updates) or any folder of your own.

```bash
H="<home folder>"
uv venv "$H/venv-gpu" --python 3.11
PY="$H/venv-gpu/Scripts/python.exe"          # Linux: PY="$H/venv-gpu/bin/python"
uv pip install --python "$PY" "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128" "pyannote.audio==4.0.7" \
  einops safetensors huggingface-hub pyyaml soundfile numpy "triton-windows==3.4.*; sys_platform == 'win32'" \
  --index-url https://download.pytorch.org/whl/cu128 --extra-index-url https://pypi.org/simple \
  --index-strategy unsafe-best-match
```

Index trap: without the PyTorch index, Windows gets a CPU-only torch from PyPI (`torch.cuda.is_available()` →
False). With it but without `--index-strategy unsafe-best-match`, uv takes every package the PyTorch index
mirrors (numpy, sympy, jinja2, …) from that index only, and resolution fails or picks stale versions. The
`+cu128` pins keep uv from swapping in a CPU build on a later install. On Linux the triton marker skips
`triton-windows` (torch brings `triton`). Measured: the command resolves 99 packages, ~9 s from a warm uv cache,
~3 GB on disk.

Check:
```bash
"$PY" -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
"$PY" "${CLAUDE_PLUGIN_ROOT}/scripts/reuse_file.py" --selfcheck     # "ok": true (measured rel_error 1.7e-7)
```

mamba_ssm is not installed: building its CUDA kernels needs nvcc and a C++ toolchain. The plugin ships a
pure-PyTorch stand-in with a fused Triton scan (`scripts/mamba_shim`, derived from state-spaces/mamba,
Apache-2.0) that loads the official weights unchanged; `--selfcheck` compares its Triton and PyTorch scans.
Without Triton it falls back to the PyTorch loop (much slower). Env `REUSE_NO_TRITON=1` forces that path.

macOS: no CUDA, and the official RE-USE inference has no CPU mode, so `reuse` cannot run there. Planning,
placing, mastering an existing RE-USE result and the A/B page work.

## 3. RE-USE weights (default: NVIDIA NemotronLabs RE-USE-laughter, commercial use allowed)

- Default model: [nvidia/NVIDIA-NemotronLabs-RE-USE-laughter](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE-laughter),
  not gated, model card: "better preserves laughter while restoring the quality of the input speech" (VocalSound
  added to the training of [nvidia/NVIDIA-NemotronLabs-RE-USE](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE),
  "ready for commercial use"). Default because it dropped the fewest words in a three-checkpoint test on French
  studio and podcast speech ([reuse](reuse.md)). 9.6 M parameters (bi-directional Mamba, 30 layers),
  8–48 kHz mono, language-agnostic, ~2500 h of training speech. Papers: Fu et al., [arXiv 2603.02641](https://arxiv.org/abs/2603.02641)
  and [arXiv 2606.25621](http://arxiv.org/abs/2606.25621). Its card notes that on overlapping speech it keeps the
  main speaker and suppresses the others (one reason the split exists). Its `config.json` nests the settings
  under `cfg`; the runner reads the recipe yaml and passes it explicitly, so this does not matter (verified).
- License: OpenMDW-1.1 (`LICENSE` in the download, identical in the plain NemotronLabs repo). It grants use, modification
  and distribution of the "Model Materials" (weights, code, docs) free of charge, commercial use included, and
  puts no restriction or obligation on the outputs (your cleaned audio). Obligations: if you distribute any
  portion of the Model Materials, ship a copy of the agreement and keep the copyright and origin notices
  (including the third-party notice list at the end of `LICENSE`); suing anyone for patent or copyright
  infringement over the Model Materials ends your rights (unless you are countersuing). Provided as is; clearing
  any third-party rights is on you. Running it locally needs no notices; this plugin redistributes nothing and
  imports the model code from your download.
- Download (no login, ~37 MB):
  ```bash
  "$H/venv-gpu/Scripts/hf" download nvidia/NVIDIA-NemotronLabs-RE-USE-laughter --local-dir "$H/models/RE-USE"   # Linux: venv-gpu/bin/hf
  ```
  `reuse_file.py` needs `models/`, `utils/`, `recipes/*.yaml` and `model.safetensors` from that folder.

Alternatives, same files and the same code path (download into another folder and point `AUDIO_CLEANUP_REUSE_DIR`
or `reuse_dir` at it):

| Repo | License | Differs in |
|---|---|---|
| `nvidia/NVIDIA-NemotronLabs-RE-USE` | OpenMDW-1.1, commercial | The first commercial release, without the laughter data; card full-set scores close to the default (PESQ 2.57 vs 2.60, NISQA 4.33 vs 4.27). In our test it dropped more words on two-speaker podcast audio (10 vs 2 dropouts in 15.6 min, [reuse](reuse.md)). |
| `nvidia/RE-USE` | NVIDIA One-Way Noncommercial License (NSCLv1, https://github.com/NVlabs/HMAR/blob/main/LICENSE) | **Non-commercial research and educational use only**, covering weights and code; derivatives keep the limit; breaking it ends the license. Earlier checkpoint (`30USEMamba_peak+GAN_tel_mic_1134k`) with other training data; the listening test and the behaviour notes in reuse.md were made with it. Use it only if your work is non-commercial. |

## 4. pyannote speaker-diarization-community-1 (two-speaker split only)

- Model: [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1),
  CC-BY-4.0 (credit pyannote when you publish results), gated: sign in on Hugging Face and accept its conditions
  (they share your contact details with pyannote). Library pyannote.audio: MIT.
- Create a read token (https://huggingface.co/settings/tokens) and expose it as env `HF_TOKEN` from your shell
  or secret store for the download only; never write it into job.json.
- Download once (~32 MB); runs are offline afterwards:
  ```bash
  "$H/venv-gpu/Scripts/hf" download pyannote/speaker-diarization-community-1 \
    --revision 3533c8cf8e369892e6b79ff1bf80f7b0286a54ee --local-dir "$H/models/speaker-diarization-community-1"
  ```
  The revision is the one the split was measured with; drop `--revision` for the latest. Without a local copy,
  `diarize.py` loads the Hugging Face id with `HF_TOKEN` on each run.
- Harmless warnings in the log: torchcodec "not installed correctly … WinError 127" on Windows (audio is passed
  in memory), pyannote's TF32 ReproducibilityWarning, and `std(): degrees of freedom is <= 0` on very short turns.

## 5. Whisper for the A/B page

`uv run ab_page.py` installs faster-whisper (MIT) in a cached throwaway env. `large-v3` (OpenAI Whisper weights,
MIT; CTranslate2 conversion by Systran) downloads ~3 GB into the Hugging Face cache on first use. CPU int8 ≈ 10 s
per 4–6 s clip on a 12-thread desktop CPU, three transcriptions per spot, cached in `ab-page/whisper_cache.json`.
`--device cuda` needs cuBLAS 12 and cuDNN 9 libraries on the library path; the CPU default avoids that.
`--no-asr` skips Whisper entirely.

## 6. DaVinci Resolve Studio and its MCP

- DaVinci Resolve **Studio** 21 (tested 21.2): external scripting and the MCP server ship with Studio.
- Register Blackmagic's MCP server that comes with Studio 21 in Claude Code (Windows: `ResolveMCP.exe` in the
  Resolve install folder; also packaged as `DaVinciResolve.mcpb`), e.g. `claude mcp add davinci-resolve --
  "<Resolve install folder>/ResolveMCP.exe"`. It answers only while Resolve runs; check with its
  `get_resolve_status` tool.
- The plugin uses `run_script_unsafe` (filesystem access; 60 s cap per call). The sandboxed `run_script` cannot
  read or write the job folder.

## 7. Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `AUDIO_CLEANUP_HOME` | cleanup.py | folder with `venv-gpu/` and `models/{RE-USE,speaker-diarization-community-1}` |
| `AUDIO_CLEANUP_REUSE_PYTHON` | cleanup.py | GPU venv python (default `<home>/venv-gpu/Scripts/python.exe` or `bin/python`) |
| `AUDIO_CLEANUP_DIAR_PYTHON` | cleanup.py | python with pyannote.audio (default: the RE-USE python) |
| `AUDIO_CLEANUP_REUSE_DIR` | cleanup.py, reuse_file.py | RE-USE download folder (default checkpoint: NemotronLabs RE-USE-laughter) |
| `AUDIO_CLEANUP_PYANNOTE` | cleanup.py, diarize.py | community-1 folder, or the HF id |
| `AUDIO_CLEANUP_GPU_LOCK` | reuse_file.py, diarize.py | lock directory (atomic mkdir, 15 s retry, own stale locks cleared after 20 min) |
| `HF_TOKEN` | diarize.py, `hf download` | Hugging Face read token (gated pyannote model) |
| `FFMPEG` | all | ffmpeg binary (default: `ffmpeg` on PATH) |

The same keys without the prefix (`reuse_python`, `reuse_dir`, …) can go in `<job>/job.local.json` (never shared).

## Speed, memory, disk (measured)

| What | Figure |
|---|---|
| RE-USE, RTX 5090 | 4.5–5× realtime per track (non-commercial checkpoint), 5.6–5.7× (NemotronLabs and -laughter); only the used ranges ± 2 s are processed |
| RE-USE, RTX 4070 Ti (another app holding 5 GB VRAM) | 1.0× realtime, 2.4 s per 5 s chunk; a 1 h track ≈ 1 h of GPU |
| RE-USE VRAM / RAM | 2.6–2.8 GiB peak; constant RAM (streams chunk by chunk). Batch size 1 only. Hop 0.5 = every sample processed twice |
| Diarization, RTX 5090 | the whole source is diarized, not just the cut: a 68 min source in 33 s (123× realtime), 1.6 GiB peak VRAM, 80 s with model load and decode |
| End to end, RTX 5090 | 3 sources, 5.6 min used: 2.6 min. 3 sources (one a 68 min two-speaker podcast, split), 1.9 min used: 3.0 min; rerun with nothing changed: 0.6 min |
| Whisper (A/B page) | CPU int8 5–10 s per transcription (model cached): 3 spots in 51 s |
| Disk per job | ≈ 2 × the 48 kHz float size of each source used (1 h stereo = 1.4 GB) + ~690 MB per hour per output file |
| RAM (estimate from the code) | finish holds up to ~8 region-length float32 arrays on a split source (30 min of used audio = 345 MB each): plan ~3 GB per 30 min used, about half without a split; master and scan need less |
