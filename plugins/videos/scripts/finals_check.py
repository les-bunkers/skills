# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Check a rendered master as the delivered file: video stream, frame count, loudness, and what the sound really is.

  video     codec / size; frames counted from packets = the cut list's length (--cuts) or --frames
  loudness  integrated loudness, true peak, LRA with ffmpeg ebur128 on the file's own audio (loudnorm reads ~0.3 LU
            low on a file with a silent tail); pass = --lufs +- --tol and true peak <= --tp
  --ref     instead of the cut-list rebuild, compare with a file already on the timeline clock (the mastered track
            the audio stage made): render = a*ref at lag 0, a ~ 1 when that track alone plays
  content   (--cuts + --project) rebuild the expected sound for a window from the cut list and the source audio
            (sub-frame edges included), least-squares fit render = a*expected [+ b*--ref2] at lags +-200 samples:
            expect a ~ 1, lag 0. Catches a wrong track enabled, a mix of two tracks, a shifted edit. A render
            that passed through a loudness master is scaled and compressed: expect a stable a != 1 there, lag 0.
in:  the master file; out: one JSON line per check + PASS/FAIL; --json writes them
usage: uv run finals_check.py master.mp4 --cuts cut/tight.cuts.json --project cut/edit.json [--lufs -14 --tol 0.5 --tp -1]
       [--at 300 --window 60] [--ref master.wav] [--ref2 processed.wav] [--no-loudness]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
SR = 48000


def decode(path, t0=None, dur=None, sr=SR):
    cmd = [E.need_ffmpeg(), "-v", "error"]
    if t0 is not None:
        cmd += ["-ss", f"{max(0.0, t0):.6f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.6f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32).astype(np.float64)


def loudness(path):
    p = subprocess.run([E.need_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af",
                        "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = p.stderr[p.stderr.rfind("Summary:"):]
    i = re.search(r"I:[ ]+(-?[\d.]+) LUFS", tail)  # "I:" then spaces (a backslash right after the colon trips leak_check)
    lra = re.search(r"LRA:\s+(-?[\d.]+) LU", tail)
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    f = lambda m: float(m.group(1)) if m and m.group(1) != "-inf" else None  # noqa: E731
    return f(i), f(tp), f(lra)


def video(path):
    pr = json.loads(subprocess.run([os.environ.get("FFPROBE", "ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_packets",
                                    "-show_entries", "stream=codec_name,width,height,nb_read_packets,r_frame_rate", "-of", "json",
                                    str(path)], capture_output=True, text=True, check=True).stdout)
    return (pr.get("streams") or [{}])[0]


def expected(P, cut, t0, dur):
    """Expected sound of the cut for timeline [t0, t0 + dur) at SR, from each part's sound range."""
    fps = E.norm_fps(cut["fps"])
    n = int(round(dur * SR))
    out = np.zeros(n)
    for s in cut["segments"]:
        ai, ao, ar = s.get("a_in_f", s["in_f"]), s.get("a_out_f", s["out_f"]), s.get("a_rec_f", s["rec_f"])
        r0, r1 = ar / fps, (ar + ao - ai) / fps
        a, b = max(r0, t0), min(r1, t0 + dur)
        if a >= b:
            continue
        i0, i1 = int(round((a - t0) * SR)), int(round((b - t0) * SR))
        src = P.p(P.src(s["src"]).get("audio")) or P.file(s["src"])
        x = decode(src, ai / fps + (a - r0), (i1 - i0) / SR + 0.01)
        m = min(i1 - i0, len(x))
        out[i0:i0 + m] = x[:m]
    return out


def fit(got, cols, L=200):
    best = None
    core = slice(L, len(cols[0]) - L)
    X = np.stack([c[core] for c in cols], 1)
    for lag in range(-L, L + 1):
        x = got[core.start + lag:core.stop + lag]
        if len(x) != len(X):
            continue
        coef, *_ = np.linalg.lstsq(X, x, rcond=None)
        err = float(np.sum((x - X @ coef) ** 2) / (np.sum(x ** 2) + 1e-12))
        if best is None or err < best[0]:
            best = (err, lag, coef)
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("master")
    ap.add_argument("--cuts")
    ap.add_argument("--project")
    ap.add_argument("--frames", type=int)
    ap.add_argument("--lufs", type=float, default=-14.0)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--tp", type=float, default=-1.0)
    ap.add_argument("--no-loudness", action="store_true", help="skip loudness (e.g. an unmastered render)")
    ap.add_argument("--at", type=float, help="content window start, timeline s (default: middle of the cut)")
    ap.add_argument("--window", type=float, default=60.0)
    ap.add_argument("--ref", help="expected sound as a file on the timeline clock (mastered track) instead of the cut-list rebuild")
    ap.add_argument("--ref2", help="second candidate track (e.g. processed speech) rendered on the same timeline clock")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows, ok_all = [], True
    cut = E.read_json(a.cuts) if a.cuts else None
    v = video(a.master)
    want = a.frames or (cut and cut.get("total_f") or E.total_f(cut["segments"]) if cut else None)
    got_f = int(v.get("nb_read_packets", 0) or 0)
    ok = bool(v.get("codec_name")) and (want is None or got_f == want)
    rows.append({"check": "video", "codec": v.get("codec_name"), "size": f"{v.get('width')}x{v.get('height')}",
                 "fps": v.get("r_frame_rate"), "frames": got_f, "expected": want, "ok": ok})
    ok_all &= ok
    if not a.no_loudness:
        i, tp, lra = loudness(a.master)
        ok = i is not None and abs(i - a.lufs) <= a.tol and tp is not None and tp <= a.tp
        rows.append({"check": "loudness", "I": i, "TP": tp, "LRA": lra, "target": a.lufs, "ok": ok})
        ok_all &= ok
    if cut and a.project:
        P = E.Project(a.project)
        fps = E.norm_fps(cut["fps"])
        total = E.total_f(cut["segments"]) / fps
        dur = min(a.window, total)
        t0 = a.at if a.at is not None else max(0.0, total / 2 - dur / 2)
        exp = decode(a.ref, t0, dur) if a.ref else expected(P, cut, t0, dur)
        got = decode(a.master, t0, dur)
        n = min(len(exp), len(got))
        cols = [exp[:n]]
        if a.ref2:
            cols.append(decode(a.ref2, t0, dur)[:n])
        err, lag, coef = fit(got[:n], cols)
        ok = abs(lag) <= 2 and err < 0.05 and (not a.ref or abs(coef[0] - 1) < 0.05) and (len(coef) < 2 or abs(coef[1]) < 0.05 * max(abs(coef[0]), 1e-9))
        rows.append({"check": "content", "window_s": [round(t0, 2), round(t0 + dur, 2)], "lag_samples": lag,
                     "coef": [round(float(c), 4) for c in coef], "residual": f"{err:.1e}", "ok": ok})
        ok_all &= ok
    for r in rows:
        print(("OK   " if r["ok"] else "FAIL ") + json.dumps(r, ensure_ascii=False))
    print("PASS" if ok_all else "FAIL")
    if a.json:
        E.write_json(a.json, {"file": a.master, "checks": rows, "pass": ok_all})
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
