"""Speaker diarization of one WHOLE recording with pyannote speaker-diarization-community-1.

Runs in the GPU venv (references/setup.md): torch (CUDA), pyannote.audio 4.x. cleanup.py calls it per two-speaker
source; it also works alone. Audio is decoded by ffmpeg to 16 kHz mono float and passed in memory (torchcodec's
file loader fails on some Windows installs).

Model: --model = a local folder holding the downloaded pipeline (config.yaml + embedding/ plda/ segmentation/;
runs offline) or the Hugging Face id `pyannote/speaker-diarization-community-1` (gated: accept its terms on
huggingface.co, then env HF_TOKEN). Env AUDIO_CLEANUP_PYANNOTE sets the default. Optional env
AUDIO_CLEANUP_GPU_LOCK = lock directory (see gpu_lock.py). FFMPEG env overrides ffmpeg.

Output JSON: {"source", "model", "num_speakers", "audio_s", "seconds", "peak_vram_gib",
              "exclusive": [[start_s, end_s, label]],   # one speaker at a time (what the split uses)
              "full": [[start_s, end_s, label]]}        # overlapping turns kept
Seconds from the start of the input file.

usage: python diarize.py SRC.wav --out SRC.diar.json [--model DIR_OR_ID] [--speakers 2] [--device cuda]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO_ID = "pyannote/speaker-diarization-community-1"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default=os.environ.get("AUDIO_CLEANUP_PYANNOTE") or REPO_ID)
    ap.add_argument("--speakers", type=int, default=2)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    local = Path(args.model).is_dir()
    if local:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import numpy as np
    import torch
    from gpu_lock import gpu_lock
    from pyannote.audio import Pipeline

    pipe = Pipeline.from_pretrained(str(Path(args.model).resolve()) if local else args.model,
                                    token=None if local else os.environ.get("HF_TOKEN"))
    if pipe is None:
        sys.exit(f"could not load {args.model}: accept the model terms on huggingface.co and set HF_TOKEN, "
                 "or pass a downloaded folder")
    r = subprocess.run([os.environ.get("FFMPEG", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-i",
                        str(args.src), "-ac", "1", "-af", "aresample=16000:resampler=soxr", "-f", "f32le", "-"],
                       check=True, capture_output=True)
    w = torch.from_numpy(np.frombuffer(r.stdout, np.float32).copy())[None]
    audio_s = w.shape[1] / 16000
    print(f"diarize {args.src.name}: {audio_s / 60:.1f} min, {args.speakers} speakers", flush=True)
    with gpu_lock("pyannote " + args.src.name):
        dev = torch.device(args.device)
        pipe.to(dev)
        if dev.type == "cuda":
            torch.cuda.reset_peak_memory_stats()
        t = time.time()
        out = pipe({"waveform": w, "sample_rate": 16000}, num_speakers=args.speakers)
        secs = time.time() - t
        peak = torch.cuda.max_memory_allocated() / 2 ** 30 if dev.type == "cuda" else None

    def rows(ann):
        return [[round(s.start, 3), round(s.end, 3), lab] for s, _, lab in ann.itertracks(yield_label=True)]

    doc = {"source": str(args.src), "model": args.model, "num_speakers": args.speakers, "audio_s": round(audio_s, 2),
           "seconds": round(secs, 1), "peak_vram_gib": round(peak, 2) if peak is not None else None,
           "exclusive": rows(out.exclusive_speaker_diarization), "full": rows(out.speaker_diarization)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=0), encoding="utf-8")
    print(f"  {len(doc['exclusive'])} turns in {secs:.0f} s ({audio_s / max(secs, 1e-6):.0f}x realtime), "
          f"peak VRAM {doc['peak_vram_gib']} GiB", flush=True)


if __name__ == "__main__":
    main()
