"""Bake-off detector: Respiro-en frame-wise breath detector (github.com/ydqmkkx/Respiro-en, MIT, trained on English
LibriTTS-R, 16 kHz, 10 ms frames) on a job's take.

Needs a clone of that repo (its modules.py + respiro-en.pt weights): --respiro-dir DIR or env RESPIRO_DIR.
Long takes run in 20 s chunks every 15 s (LibriTTS utterances are short; mel dB is normalised per chunk by
power_to_db(ref=max), as in training), triangular overlap-add of the frame probabilities, raw and high-passed 80 Hz.
Its README's `min_length = 20` counts FRAMES (20 x 10 ms = 200 ms), not ms. Spans here need > 8 frames (>= 90 ms).
Writes <job>/probs_respiro.npz: one array per variant [1, frame] + "meta" (sweep thresholds, union thresholds
0.064 = the README's validation pick, and 0.5).

usage: <venv>/python run_respiro.py <job> --respiro-dir DIR [--variants raw hp80] [--force]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ears_job as J  # noqa: E402

SR = 16000
THRS = [0.064, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9]
UNION_THRS = [0.064, 0.5]
MIN_FRAMES = 8
HIGHPASS = {"raw": None, "hp80": 80}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Respiro-en breath probabilities for a bake-off job -> probs_respiro.npz.")
    ap.add_argument("job", help="bake-off job folder (ears_job.py init)")
    ap.add_argument("--respiro-dir", default=os.environ.get("RESPIRO_DIR"), help="clone of ydqmkkx/Respiro-en")
    ap.add_argument("--variants", nargs="+", default=["raw", "hp80"], choices=list(HIGHPASS))
    ap.add_argument("--device", choices=["cuda", "cpu"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not a.respiro_dir:
        ap.error("give --respiro-dir (or set RESPIRO_DIR): git clone https://github.com/ydqmkkx/Respiro-en")
    rdir = Path(a.respiro_dir).expanduser().resolve()
    if not (rdir / "modules.py").is_file() or not (rdir / "respiro-en.pt").is_file():
        sys.exit(f"{rdir}: needs modules.py and respiro-en.pt from github.com/ydqmkkx/Respiro-en")
    cfg = J.load_job(a.job)
    dst = cfg["dir"] / "probs_respiro.npz"
    audio = cfg["audio"]
    if dst.is_file() and not a.force and dst.stat().st_mtime >= audio.stat().st_mtime:
        print(f"{dst}: up to date (skip; --force recomputes)")
        return

    import numpy as np
    import torch

    import ears_dsp as D

    sys.path.insert(0, str(rdir))
    from modules import DetectionNet, feature_extractor  # Respiro-en's own code

    dev = torch.device(a.device or ("cuda" if torch.cuda.is_available() else "cpu"))
    net = DetectionNet().to(dev)
    ck = torch.load(rdir / "respiro-en.pt", map_location=dev, weights_only=True)
    net.load_state_dict(ck["model"])
    net.eval()
    hop = int(SR * 0.01)
    arrays, t_all = {}, time.time()
    for v in a.variants:
        t = time.time()
        x = D.decode(audio, SR, HIGHPASS[v])
        n_frames = 1 + len(x) // hop
        chunks = []
        for s0, s1 in D.windows(len(x), 20 * SR, 15 * SR):
            feat, length = feature_extractor(x[s0:s1], SR)
            with torch.inference_mode():
                p = net(feat.to(dev), length.to(dev))[0].float().cpu().numpy()
            chunks.append((s0 // hop, p))
        p = D.overlap_add(n_frames, chunks)
        arrays[v] = p[None, :].astype(np.float32)
        counts = ", ".join(f"@{thr} {len(D.spans(p, thr, 0.01, MIN_FRAMES))}" for thr in UNION_THRS)
        print(f"{v}: {n_frames} frames in {time.time() - t:.0f} s; spans {counts}", flush=True)
    meta = {"det": "respiro", "model": "Respiro-en (ydqmkkx/Respiro-en)", "audio": str(audio), "hop_s": 0.01,
            "min_frames": MIN_FRAMES, "prompts": [{"name": "breath", "text": None, "type": "breath"}], "thrs": THRS,
            "union_thrs": UNION_THRS, "variants": a.variants, "highpass": {v: HIGHPASS[v] for v in a.variants},
            "made": time.strftime("%Y-%m-%d %H:%M"), "secs": round(time.time() - t_all, 1)}
    tmp = dst.with_name(dst.stem + ".tmp.npz")
    np.savez(tmp, meta=np.array(json.dumps(meta, ensure_ascii=False)), **arrays)
    os.replace(tmp, dst)
    print(dst)


if __name__ == "__main__":
    main()
