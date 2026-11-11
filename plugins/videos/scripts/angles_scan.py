# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Camera scan of one source, 2 samples/s on the program file's clock, for tighten's camera-switch planner.

  out_corr[angle]: correlation of the program frame with that angle's frame (64x36 gray, synced by its offset):
                   high = the live switcher shows that camera; low for all = a screen recording or keyed composite
  act:             screen activity = share of program pixels (160x90 gray) changing > 12 levels since the previous
                   sample (0 = static screen; an app showing live camera thumbnails never reaches 0)
  green[angle]:    share of strongly green pixels per angle (raw green screen in view -> never cut to that angle)
in:  --project edit.json, --src A (program file + angles with offset_s, from sync.py)
out: the source's "scan" path from edit.json (else <project>/scans/<src>.scan.json); rerun skips it unless --force
usage: uv run angles_scan.py --project cut/edit.json --src A [--src B] [--hwaccel auto|cuda|none] [--force]
Decode ~1 min per 10 min of 4K per angle with GPU decoding; a sample every 0.5 s keeps it light.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
STEP, W, H = 0.5, 160, 90


def decode(path, shift, hw):
    """RGB frames at program times k*STEP (angle shifted by its offset: angle time = program time - shift)."""
    pre = ["-ss", f"{shift:.3f}"] if shift > 0 else []
    vf = f"fps={1 / STEP},scale={W}:{H}:flags=area,format=rgb24"
    if shift < 0:  # angle starts after the program: pad the head
        vf = f"tpad=start_duration={-shift:.3f}:color=black," + vf
    hwa = ["-hwaccel", hw] if hw and hw != "none" else []
    cmd = [E.need_ffmpeg(), "-v", "error", *hwa, *pre, "-i", str(path), "-an", "-vf", vf, "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3)


def scan(P, sid, hw):
    prog = P.file(sid)
    out = decode(prog, 0.0, hw)
    n = len(out)
    og = out.astype(np.float32).mean(-1)
    act = np.r_[0, (np.abs(np.diff(og, axis=0)) > 12).mean((1, 2))]
    res = {"step": STEP, "n": n, "act": act.round(4).tolist(), "out_corr": {}, "green": {}, "file": {}, "offset_s": {}}
    for name, ang in P.angles(sid).items():
        if os.path.normcase(ang["file"]) == os.path.normcase(prog):
            continue
        d = ang["offset_s"] or 0.0
        v = decode(ang["file"], d, hw)[:n]
        if len(v) < n:
            v = np.concatenate([v, np.zeros((n - len(v), H, W, 3), np.uint8)])
        r, g, b = (v[..., k].astype(np.int16) for k in range(3))
        res["green"][name] = ((g > r + 40) & (g > b + 40) & (g > 90)).mean((1, 2)).round(3).tolist()
        vg = v.astype(np.float32).mean(-1)
        x = og[:, ::2, ::2] - og[:, ::2, ::2].mean((1, 2), keepdims=True)
        c = vg[:, ::2, ::2] - vg[:, ::2, ::2].mean((1, 2), keepdims=True)
        corr = (x * c).sum((1, 2)) / (np.sqrt((x * x).sum((1, 2)) * (c * c).sum((1, 2))) + 1e-6)
        res["out_corr"][name] = corr.round(3).tolist()
        res["file"][name] = ang["file"]
        res["offset_s"][name] = d
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--src", action="append", required=True)
    ap.add_argument("--hwaccel", default="auto", help="ffmpeg -hwaccel value (auto, cuda, d3d11va, videotoolbox, none)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    P = E.Project(a.project)
    for sid in a.src:
        dst = Path(P.p(P.src(sid).get("scan")) or P.dir / "scans" / f"{sid}.scan.json")
        if dst.is_file() and not a.force:
            print(f"{sid}: exists {dst} (--force to redo)")
            continue
        if not P.angles(sid):
            print(f"{sid}: no angles in edit.json, nothing to scan")
            continue
        t0 = time.time()
        res = scan(P, sid, a.hwaccel)
        E.write_json(dst, res, indent=None)
        cams = list(res["out_corr"])
        mx = np.max([res["out_corr"][c] for c in cams], axis=0)
        gr = np.max([res["green"][c] for c in cams], axis=0)
        print(f"{sid}: {res['n']} samples in {time.time() - t0:.0f} s; program = a camera (corr > 0.7) {np.mean(mx > 0.7):.0%}; "
              f"static screen (act < 0.002) {np.mean(np.array(res['act']) < 0.002):.0%}; raw green on some angle "
              f"{np.mean(gr > 0.2):.0%} -> {dst}")


if __name__ == "__main__":
    main()
