"""Enhance one whole recording with NVIDIA RE-USE (universal speech enhancement), streamed in constant memory.

Runs in the GPU venv (references/setup.md): torch (CUDA), einops, huggingface-hub, safetensors, pyyaml, soundfile,
numpy, triton (triton-windows on Windows). RE-USE's own model code and weights are NOT in this plugin: download
them yourself and point --reuse-dir / env AUDIO_CLEANUP_REUSE_DIR at that folder. Default checkpoint:
`hf download nvidia/NVIDIA-NemotronLabs-RE-USE-laughter --local-dir <dir>` (OpenMDW-1.1, commercial use allowed).
Same layout and code path: nvidia/NVIDIA-NemotronLabs-RE-USE (OpenMDW-1.1, drops more words on two-speaker audio)
and nvidia/RE-USE (NVIDIA One-Way Noncommercial License: non-commercial use only). This adapter imports `models.*`
and `utils.util` from that folder; `mamba_ssm` comes from the bundled pure-PyTorch + Triton stand-in (scripts/mamba_shim), so no CUDA toolchain
is needed to build mamba kernels.

Processing = the official chunked inference geometry: 5 s chunks, hop 0.5, Hann overlap-add (window clamped at
1e-7), the last chunk aligned to the end of the file, frames whose magnitude is more than half zeros silenced
(the official "sweep artifact" guard). fp32. The file is read chunk by chunk and every finished sample is written
at once, so hour-long recordings need no more memory than a few chunks.

Input:  any 48 kHz WAV/FLAC (channels averaged to mono); other rates/containers are decoded to 48 kHz mono by
        ffmpeg (env FFMPEG overrides).
Output: 48 kHz mono float WAV, same sample count and time origin as the input, plus <out>.json (x realtime, chunk
        times, peak VRAM). Float output can exceed 0 dBFS: measure true peak before encoding.
Optional env AUDIO_CLEANUP_GPU_LOCK = lock directory (see gpu_lock.py).

usage: python reuse_file.py INPUT --out OUT.wav [--reuse-dir DIR] [--seconds S]
       python reuse_file.py --selfcheck           (Triton scan vs pure-PyTorch scan, GPU)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "mamba_shim"))
sys.path.insert(1, str(HERE))

SR = 48000
CHUNK_S = 5.0
HOP_PORTION = 0.5
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


def make_even(v):
    v = int(round(v))
    return v if v % 2 == 0 else v + 1


def chunk_starts(n, chunk, hop):
    """Chunk start offsets: regular hops, the last chunk aligned to the end (as the official chunked inference)."""
    if n <= chunk:
        return [0]
    count = -(-(n - chunk) // hop) + 1
    return [i * hop for i in range(count - 1)] + [n - chunk]


def gpu_used_mib():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        name, used = r.stdout.strip().splitlines()[0].split(", ")
        return name, int(used)
    except Exception:
        return None, None


def find_recipe(reuse_dir):
    named = reuse_dir / "recipes" / ("USEMamba_30x1_lr_00002_norm_05_vq_065_nfft_320_hop_40_NRIR_012_pha_0005_com_04"
                                     "_early_001.yaml")
    if named.exists():
        return named
    found = sorted((reuse_dir / "recipes").glob("*.yaml"))
    if len(found) != 1:
        sys.exit(f"expected one recipe yaml in {reuse_dir / 'recipes'}, found {len(found)}")
    return found[0]


def selfcheck():
    import torch
    from mamba_ssm import selective_scan_blocked
    from mamba_ssm.triton_scan import selective_scan_triton

    torch.manual_seed(0)
    dev = "cuda"
    b, d, n, l = 5, 256, 16, 300
    u, dt, z = (torch.randn(b, d, l, device=dev) for _ in range(3))
    A = -torch.exp(torch.randn(d, n, device=dev))
    B, C = torch.randn(b, n, l, device=dev), torch.randn(b, n, l, device=dev)
    D, bias = torch.randn(d, device=dev), torch.randn(d, device=dev)
    r = selective_scan_blocked(u, dt, A, B, C, D, z, bias, True)
    t = selective_scan_triton(u, dt, A, B, C, D, z, bias)
    rel = ((r - t).norm() / r.norm()).item()
    print(json.dumps({"gpu": torch.cuda.get_device_name(0), "torch": torch.__version__, "rel_error": rel,
                      "ok": rel < 1e-5}))
    sys.exit(0 if rel < 1e-5 else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=Path, nargs="?")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--reuse-dir", type=Path, default=os.environ.get("AUDIO_CLEANUP_REUSE_DIR"),
                    help="folder of the RE-USE download, default checkpoint nvidia/NVIDIA-NemotronLabs-RE-USE-laughter "
                         "(env AUDIO_CLEANUP_REUSE_DIR)")
    ap.add_argument("--seconds", type=float, default=None, help="only the first S seconds (quick bench)")
    ap.add_argument("--selfcheck", action="store_true", help="compare the Triton scan with the PyTorch scan")
    args = ap.parse_args()
    if args.selfcheck:
        return selfcheck()
    if not args.input or not args.out:
        ap.error("INPUT and --out are required")
    if not args.reuse_dir or not (Path(args.reuse_dir) / "models").is_dir():
        sys.exit("RE-USE folder not found: pass --reuse-dir or set AUDIO_CLEANUP_REUSE_DIR "
                 "(hf download nvidia/NVIDIA-NemotronLabs-RE-USE-laughter --local-dir <dir>, see references/setup.md)")
    reuse_dir = Path(args.reuse_dir).resolve()
    sys.path.insert(0, str(reuse_dir))

    import numpy as np
    import soundfile as sf
    import torch
    from gpu_lock import gpu_lock
    from models.generator_SEMamba_time_d4 import SEMamba
    from models.stfts import mag_phase_istft, mag_phase_stft
    from utils.util import load_config, pad_or_trim_to_match

    if not torch.cuda.is_available():
        sys.exit("RE-USE needs a CUDA GPU (the official inference has no CPU mode)")

    src, out = args.input.resolve(), args.out.resolve()
    if out == src:
        sys.exit("refusing to overwrite the input")
    out.parent.mkdir(parents=True, exist_ok=True)

    cfg = load_config(str(find_recipe(reuse_dir)))
    s = cfg["stft_cfg"]
    n_fft, hop_s, win_s = (make_even(s[k] * SR // s["sampling_rate"]) for k in ("n_fft", "hop_size", "win_size"))
    compress = cfg["model_cfg"]["compress_factor"]

    def enhance(x):
        mag, pha, _ = mag_phase_stft(x, n_fft=n_fft, hop_size=hop_s, win_size=win_s, compress_factor=compress,
                                     center=True, addeps=False)
        amp, ph, _ = model(mag, pha)
        lin = torch.expm1(torch.relu(amp))
        amp[:, :, ((lin == 0).sum(1) / lin.shape[1] > 0.5)[0]] = 0  # sweep-artifact guard
        y = mag_phase_istft(amp, ph, n_fft, hop_s, win_s, compress)
        return pad_or_trim_to_match(x.detach(), y, pad_value=1e-8)

    tmp = None
    t0 = time.perf_counter()
    try:
        readable = sf.info(str(src)).samplerate == SR
    except RuntimeError:
        readable = False
    if not readable:
        tmp = out.with_suffix(".src48k.wav")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR),
                        "-c:a", "pcm_f32le", str(tmp)], check=True)
    decode_s = time.perf_counter() - t0
    path = tmp or src
    chunk, hop = int(CHUNK_S * SR), int(HOP_PORTION * CHUNK_S * SR)
    gpu_name, gpu_before = gpu_used_mib()
    print(f"{src.name} -> {out.name}  (GPU {gpu_name}: {gpu_before} MiB in use before start)", flush=True)

    try:
        with gpu_lock("RE-USE " + src.name) as touch, sf.SoundFile(str(path)) as fin, \
                sf.SoundFile(str(out), "w", SR, 1, subtype="FLOAT") as fout:
            n = fin.frames if args.seconds is None else min(fin.frames, int(args.seconds * SR))
            audio_s = n / SR
            starts = chunk_starts(n, chunk, hop)
            length = min(n, chunk)
            window = torch.clamp(torch.hann_window(chunk), min=1e-7)[:length].numpy()
            print(f"{audio_s:.1f} s audio, {len(starts)} chunks of {CHUNK_S:g} s", flush=True)
            device = torch.device("cuda")
            t = time.perf_counter()
            model = SEMamba.from_pretrained(str(reuse_dir), cfg=cfg).to(device).eval()
            torch.cuda.synchronize()
            load_s = time.perf_counter() - t

            def read(at):
                fin.seek(at)
                return fin.read(length, dtype="float32", always_2d=True).mean(axis=1)

            with torch.no_grad():
                t = time.perf_counter()  # warm-up on the chunk shape (Triton compile, cuDNN autotune), not timed
                enhance(torch.from_numpy(read(starts[0])[None]).to(device))
                torch.cuda.synchronize()
                warmup_s = time.perf_counter() - t
                torch.cuda.reset_peak_memory_stats()
                acc, wsum = np.zeros(length, np.float32), np.zeros(length, np.float32)  # acc[0] = sample `base`
                base, chunk_ms, out_peak, next_report = 0, [], 0.0, 0.1
                t_run = time.perf_counter()
                for i, st in enumerate(starts):
                    x = torch.from_numpy(read(st)[None]).to(device)
                    t = time.perf_counter()
                    y = enhance(x)[0].cpu().numpy()
                    chunk_ms.append((time.perf_counter() - t) * 1000)
                    shift = st - base  # starts only move forward: slide the buffers
                    acc = np.concatenate([acc[shift:], np.zeros(shift, np.float32)])
                    wsum = np.concatenate([wsum[shift:], np.zeros(shift, np.float32)])
                    base = st
                    acc += y * window
                    wsum += window
                    done = (starts[i + 1] if i + 1 < len(starts) else n) - base  # final up to the next start
                    final = acc[:done] / wsum[:done]
                    fout.write(final)
                    out_peak = max(out_peak, float(np.abs(final).max(initial=0)))
                    touch()
                    frac = (i + 1) / len(starts)
                    if frac >= next_report or frac == 1:
                        el = time.perf_counter() - t_run
                        print(f"  {frac:4.0%}  {frac * audio_s / el:.2f}x realtime  ETA {el / frac - el:.0f} s",
                              flush=True)
                        next_report += 0.1
                run_s = time.perf_counter() - t_run
            peak = torch.cuda.max_memory_allocated() / 2 ** 30
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    ms = np.array(chunk_ms)
    summary = {"input": str(src), "output": str(out), "audio_s": round(audio_s, 2), "chunks": len(starts),
               "decode_s": round(decode_s, 2), "load_s": round(load_s, 2), "warmup_s": round(warmup_s, 2),
               "run_s": round(run_s, 2), "x_realtime": round(audio_s / run_s, 3),
               "chunk_ms_p50": round(float(np.percentile(ms, 50)), 1),
               "chunk_ms_p95": round(float(np.percentile(ms, 95)), 1), "peak_vram_gib": round(peak, 2),
               "gpu": gpu_name, "gpu_used_mib_before": gpu_before, "out_peak_abs": round(out_peak, 4)}
    out.with_suffix(".json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
