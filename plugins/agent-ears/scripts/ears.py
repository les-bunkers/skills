"""ears: speech file -> <stem>.ears.json, the breaths an editor can cut, heard by PE-A-Frame.

Model facebook/pe-a-frame-large (Apache-2.0), text prompt "a person inhaling sharply", threshold 0.95, raw audio,
no word filter. Bake-off winner on 116 human labels (references/measurements.md): breath P 0.98 / R 0.93, holdout
0.91 / 0.91, 0 speech cut, median edge error 40 ms (start) / 20 ms (end). Plateau 0.95-0.98 (0.98 = P 1.00 / R 0.91).

Runs through transformers' PeAudioFrameLevelModel: 48 kHz mono, one frame per 40 ms, 20 s windows every 10 s,
triangular overlap-add. Times = seconds from the start of the input file (ffmpeg decodes from t=0).
Output (Ears events format, CONTRIBUTING.md):
  {"audio", "model", "prompt", "thr", "hop_s": 0.04, "made", "secs",
   "events": [{"t0", "t1", "type": "breath", "conf"}], "prob": [one value per hop_s, 3 decimals]}
A span needs > 1 frame above thr (>= 80 ms). conf = peak probability inside the span.

Reruns: an output newer than its input with the same model and prompt is skipped; with another --thr it is
re-thresholded from its own stored prob (no model load). --force recomputes. One model load serves every input.
Each written file adds a line to ears.log beside it.
Needs the GPU venv (references/setup.md); CPU works, slowly. ~5.4 GB VRAM.

usage: <venv>/python ears.py <audio-or-video ...> --out-dir DIR [--thr 0.95] [--prompt TEXT] [--hf-home DIR]
                             [--device cuda|cpu] [--force]
       <venv>/python ears.py <in> --out <file.ears.json>
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ID = "facebook/pe-a-frame-large"
SR, HOP = 48000, 1920      # 48 kHz, 40 ms frames
PROMPT = "a person inhaling sharply"
THR = 0.95
MIN_FRAMES = 1             # > 1 frame = >= 80 ms
WIN_S, STEP_S = 20, 10


class PeFrame:
    """PE-A-Frame frame-level scorer for a fixed list of text prompts -> prob [prompt, frame] per 40 ms."""

    def __init__(self, prompts, device=None):
        import torch
        from transformers import PeAudioFrameLevelModel, PeAudioProcessor

        self.torch = torch
        self.dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        t = time.time()
        self.proc = PeAudioProcessor.from_pretrained(REPO_ID)
        self.model = PeAudioFrameLevelModel.from_pretrained(REPO_ID).to(self.dev).eval()
        tok = self.proc.tokenizer(list(prompts), return_tensors="pt", padding="longest")
        with torch.inference_mode():
            # transformers 5.22 get_text_audio_embeds() omits output_hidden_states=True (hidden_states = None):
            # take the same path as PeAudioFrameLevelModel.forward()
            to = self.model.text_model(input_ids=tok["input_ids"].to(self.dev),
                                       attention_mask=tok["attention_mask"].to(self.dev),
                                       output_hidden_states=True, return_dict=True)
            self.txt = self.model.text_audio_head(to.hidden_states[-1][:, 0])
        self.scale = float(self.model.text_audio_logit_scale.detach())
        self.bias = float(self.model.text_audio_logit_bias.detach())
        mem = torch.cuda.memory_allocated() / 2**30 if self.dev.type == "cuda" else 0
        print(f"{REPO_ID} on {self.dev} in {time.time() - t:.0f} s" + (f", {mem:.1f} GiB" if mem else ""), flush=True)

    def prob(self, x):
        import ears_dsp as D

        torch = self.torch
        n_frames = -(-len(x) // HOP)
        chunks = []
        for a, b in D.windows(len(x), WIN_S * SR, STEP_S * SR):
            fe = self.proc.feature_extractor(x[a:b], sampling_rate=SR, return_tensors="pt")
            pm = fe["padding_mask"].to(self.dev) if "padding_mask" in fe else None
            with torch.inference_mode():
                au = self.model.get_audio_embeds(fe["input_values"].to(self.dev), pm)
                lg = (au[0] @ self.txt.T).T * self.scale + self.bias  # [prompt, frame]
            chunks.append((a // HOP, lg.sigmoid().float().cpu().numpy()))
        return D.overlap_add(n_frames, chunks)


def result(src, prob, prompt, thr, secs):
    import ears_dsp as D

    ev = [{"t0": a, "t1": b, "type": "breath", "conf": c} for a, b, c in D.spans(prob, thr, HOP / SR, MIN_FRAMES)]
    return {"audio": str(src), "model": REPO_ID, "prompt": prompt, "thr": thr, "hop_s": HOP / SR,
            "made": time.strftime("%Y-%m-%d %H:%M"), "secs": secs, "events": ev,
            "prob": [round(float(v), 3) for v in prob]}


def write(dst, r, note):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False)
    os.replace(tmp, dst)
    with open(dst.parent / "ears.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {dst.name}  {note}\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Breath spans (PE-A-Frame) for speech files -> <stem>.ears.json.")
    ap.add_argument("audio", nargs="+", help="audio or video files (anything ffmpeg reads)")
    ap.add_argument("--out-dir", help="folder for <stem>.ears.json (the project or job folder, not the media folder)")
    ap.add_argument("--out", nargs="*", default=None, help="explicit output path per input (instead of --out-dir)")
    ap.add_argument("--thr", type=float, default=THR, help=f"breath threshold (default {THR}, the bake-off pick)")
    ap.add_argument("--prompt", default=PROMPT, help="text prompt (only the default was measured)")
    ap.add_argument("--hf-home", help="Hugging Face cache for the 5.5 GB weights (sets HF_HOME for this process)")
    ap.add_argument("--device", choices=["cuda", "cpu"], help="default: cuda when available")
    ap.add_argument("--force", action="store_true", help="recompute even when the output is up to date")
    a = ap.parse_args()
    if a.hf_home:
        os.environ["HF_HOME"] = str(Path(a.hf_home).expanduser().resolve())
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    srcs = [Path(x) for x in a.audio]
    if a.out:
        outs = [Path(x) for x in a.out]
    elif a.out_dir:
        outs = [Path(a.out_dir) / f"{s.stem}.ears.json" for s in srcs]
    else:
        ap.error("give --out-dir (or one --out path per input)")
    if len(outs) != len(srcs):
        ap.error("--out needs one path per input")
    for s in srcs:
        if not s.is_file():
            sys.exit(f"missing input: {s}")

    todo = []
    for src, dst in zip(srcs, outs):
        if dst.is_file() and not a.force and dst.stat().st_mtime >= src.stat().st_mtime:
            with open(dst, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("model") == REPO_ID and old.get("prompt") == a.prompt and old.get("prob"):
                if old.get("thr") == a.thr:
                    print(f"{dst}: up to date, {len(old['events'])} breaths (skip)")
                else:
                    r = result(old["audio"], old["prob"], a.prompt, a.thr, old.get("secs"))
                    r["rethresholded_from"] = old.get("thr")
                    note = f"re-thresholded {old.get('thr')} -> {a.thr} from stored prob, {len(r['events'])} breaths"
                    write(dst, r, note)
                    print(f"{dst}: {note}")
                continue
        todo.append((src, dst))
    if not todo:
        return

    import ears_dsp as D

    pe = PeFrame([a.prompt], a.device)
    for src, dst in todo:
        t = time.time()
        x = D.decode(src, SR)
        p = pe.prob(x)[0]
        r = result(src, p, a.prompt, a.thr, round(time.time() - t, 1))
        note = f"{len(r['events'])} breaths in {len(x) / SR / 60:.1f} min of audio, {r['secs']} s, thr {a.thr}, {src}"
        write(dst, r, note)
        print(f"{dst}: {note}", flush=True)


if __name__ == "__main__":
    main()
