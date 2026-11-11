"""Bake-off detector: PE-A-Frame (facebook/pe-a-frame-large, Apache-2.0) with several sound prompts on a job's take.

One model load, text embeddings once, audio in 20 s windows every 10 s (ears.py's PeFrame), raw and high-passed
80 Hz audio. Writes <job>/probs_peaframe.npz: one array per variant [prompt, frame] (40 ms frames) plus "meta"
(JSON: prompts with their scored type, hop_s, sweep thresholds, union threshold 0.3 = the model card's low end).
union.py turns it into candidate spans; score.py sweeps every threshold without rerunning the model.
Prompt types: breath and euh are scored; mouth / handling / laugh / throat ride along in candidates as context;
"speech" is a control (it reads 1.0 even in silent gaps, so it is useless as a speech guard) and is never used.
--prompts FILE = JSON [{"name", "text", "type"}] to test other prompts.

usage: <venv>/python run_peaframe.py <job> [--variants raw hp80] [--prompts FILE] [--hf-home DIR] [--force]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ears_job as J  # noqa: E402

PROMPTS = [
    {"name": "breath_in", "text": "a person inhaling sharply", "type": "breath"},
    {"name": "breath", "text": "a person breathing", "type": "breath"},
    {"name": "euh", "text": "a filled pause, someone saying uh", "type": "euh"},
    {"name": "lip_smack", "text": "lip smack", "type": "mouth"},
    {"name": "mouth_click", "text": "mouth click", "type": "mouth"},
    {"name": "chair", "text": "chair creaking", "type": "handling"},
    {"name": "mic_bump", "text": "microphone bump", "type": "handling"},
    {"name": "laugh", "text": "laughter", "type": "laugh"},
    {"name": "throat", "text": "throat clearing", "type": "throat"},
    {"name": "speech", "text": "a man speaking", "type": "speech"},
]
THRS = [0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 0.97, 0.98, 0.99]
HIGHPASS = {"raw": None, "hp80": 80}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="PE-A-Frame probabilities for a bake-off job -> probs_peaframe.npz.")
    ap.add_argument("job", help="bake-off job folder (ears_job.py init)")
    ap.add_argument("--variants", nargs="+", default=["raw", "hp80"], choices=list(HIGHPASS))
    ap.add_argument("--prompts", help='JSON file [{"name", "text", "type"}] replacing the default prompt set')
    ap.add_argument("--name", default="peaframe", help="detector name (output probs_<name>.npz)")
    ap.add_argument("--hf-home", help="Hugging Face cache (sets HF_HOME for this process)")
    ap.add_argument("--device", choices=["cuda", "cpu"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.hf_home:
        os.environ["HF_HOME"] = str(Path(a.hf_home).expanduser().resolve())
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    cfg = J.load_job(a.job)
    dst = cfg["dir"] / f"probs_{a.name}.npz"
    audio = cfg["audio"]
    if dst.is_file() and not a.force and dst.stat().st_mtime >= audio.stat().st_mtime:
        print(f"{dst}: up to date (skip; --force recomputes)")
        return
    prompts = J.read_json(a.prompts) if a.prompts else PROMPTS

    import numpy as np

    import ears_dsp as D
    from ears import HOP, REPO_ID, SR, PeFrame

    pe = PeFrame([p["text"] for p in prompts], a.device)
    arrays, t_all = {}, time.time()
    for v in a.variants:
        t = time.time()
        p = pe.prob(D.decode(audio, SR, HIGHPASS[v]))
        arrays[v] = p.astype(np.float32)
        counts = ", ".join(f"{q['name']} {len(D.spans(p[i], 0.3, HOP / SR, 1))}" for i, q in enumerate(prompts))
        print(f"{v}: {p.shape[1]} frames in {time.time() - t:.0f} s; spans @0.3: {counts}", flush=True)
    meta = {"det": a.name, "model": REPO_ID, "audio": str(audio), "hop_s": HOP / SR, "min_frames": 1,
            "prompts": prompts, "thrs": THRS, "union_thrs": [0.3], "variants": a.variants,
            "highpass": {v: HIGHPASS[v] for v in a.variants}, "made": time.strftime("%Y-%m-%d %H:%M"),
            "secs": round(time.time() - t_all, 1)}
    tmp = dst.with_name(dst.stem + ".tmp.npz")
    np.savez(tmp, meta=np.array(json.dumps(meta, ensure_ascii=False)), **arrays)
    os.replace(tmp, dst)
    print(dst)


if __name__ == "__main__":
    main()
