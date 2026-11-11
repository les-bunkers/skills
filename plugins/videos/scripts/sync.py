# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Audio sync offsets between recordings of one take: how much later the program file hears the same sound.

offset + = the sound comes later in the program (reference) file, so: other-file time + offset = program time, and
angle frame = program frame - round(offset * fps). Measured by cross-correlation of 8 s windows at 15/40/65/85 % of
the take (mono 16 kHz, search +-1 s), kept when the correlation peak > 0.6, median. Never assume an offset: on a
studio shoot the camera -> program offset varied 0.004-0.144 s from take to take.
in:  --ref program.mp4 other.mp4 ..., or --project edit.json [--src A] (words_media + every angle vs the file)
out: offsets on stdout; with --project --write: edit.json words_offset_s and angles.<name>.offset_s updated (backup
     beside it). Also prints duration / fps / frames per file (a take's files should agree within 0.5 s).
usage: uv run sync.py --project cut/edit.json [--src A] [--write]     uv run sync.py --ref Output.mp4 Face.mp4
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
SR = 16000


def probe(p):
    r = subprocess.run([os.environ.get("FFPROBE", "ffprobe"), "-v", "error", "-show_entries",
                        "stream=codec_type,r_frame_rate,width,height,nb_frames:format=duration", "-of", "json", str(p)],
                       capture_output=True, text=True, encoding="utf-8")
    return json.loads(r.stdout or "{}")


def audio(p, t0, dur):
    r = subprocess.run([E.need_ffmpeg(), "-v", "error", "-ss", str(t0), "-t", str(dur), "-i", str(p), "-map", "0:a:0", "-ac", "1",
                        "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def lag(other, ref, t0, W=8.0, S=1.0):
    """(offset s, corr) of `other` against `ref` around ref time t0: + = ref hears it later."""
    x = audio(other, t0, W)
    y = audio(ref, max(0, t0 - S), W + 2 * S)
    if len(x) < SR or np.sqrt(np.mean(x ** 2)) < 1e-3:
        return None
    s = min(S, t0)
    m = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    c = np.fft.irfft(np.fft.rfft(y, m) * np.conj(np.fft.rfft(x, m)), m)[:int((s + S) * SR) + 1]
    k = int(np.argmax(c))
    seg = y[k:k + len(x)]
    corr = float(np.dot(x, seg) / (np.linalg.norm(x) * np.linalg.norm(seg) + 1e-12))
    return (k - s * SR) / SR, corr


def offset(other, ref, search):
    dur = float(probe(ref).get("format", {}).get("duration", 0) or 0)
    lags = []
    for frac in (0.15, 0.4, 0.65, 0.85):
        r = lag(other, ref, max(1.0, dur * frac), S=search)
        if r and r[1] > 0.6:
            lags.append(r)
    if not lags:
        return None, lags
    return round(float(np.median([a for a, _ in lags])), 4), lags


def describe(p):
    pr = probe(p)
    v = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), {})
    return f"{float(pr.get('format', {}).get('duration', 0) or 0):8.2f} s  {v.get('width', '-')}x{v.get('height', '-')} " \
           f"{v.get('r_frame_rate', '-')} fps  {v.get('nb_frames', '?')} frames  {Path(p).name}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="with --ref: the files to measure against it")
    ap.add_argument("--ref", help="program / reference file")
    ap.add_argument("--project")
    ap.add_argument("--src", action="append")
    ap.add_argument("--write", action="store_true", help="store the offsets in edit.json")
    ap.add_argument("--search", type=float, default=1.0, help="search +- seconds (widen when a peak is missed)")
    a = ap.parse_args()
    if a.ref:
        print("ref " + describe(a.ref))
        for f in a.files:
            off, lags = offset(f, a.ref, a.search)
            print(f"{off if off is not None else 'NO SYNC':>8}  {describe(f)}  windows {[(round(x, 4), round(c, 2)) for x, c in lags]}")
        return
    if not a.project:
        ap.error("--ref or --project")
    P = E.Project(a.project)
    cfg = E.read_json(P.path)  # write back into edit.json itself (not the .local.json merge)
    for sid in a.src or list(P.sources):
        ref = P.file(sid)
        print(f"{sid}: program {describe(ref)}")
        s = P.src(sid)
        wm = P.p(s.get("words_media"))
        if wm and os.path.normcase(wm) != os.path.normcase(ref):
            off, lags = offset(wm, ref, a.search)
            print(f"   words_offset_s {off}  ({Path(wm).name}, windows {[(round(x, 4), round(c, 2)) for x, c in lags]})")
            if off is not None and sid in cfg.get("sources", {}):
                cfg["sources"][sid]["words_offset_s"] = off
        for name, ang in P.angles(sid).items():
            if os.path.normcase(ang["file"]) == os.path.normcase(ref):
                continue
            off, lags = offset(ang["file"], ref, a.search)
            print(f"   angle {name}: {off}  {describe(ang['file'])}  windows {[(round(x, 4), round(c, 2)) for x, c in lags]}")
            if off is not None and name in (cfg.get("sources", {}).get(sid, {}).get("angles") or {}):
                entry = cfg["sources"][sid]["angles"][name]
                if isinstance(entry, dict):
                    entry["offset_s"] = off
                else:
                    cfg["sources"][sid]["angles"][name] = {"file": entry, "offset_s": off}
    if a.write:
        shutil.copy(P.path, P.path.with_name(f"{P.path.stem}.before-sync-{time.strftime('%Y%m%d-%H%M%S')}.json"))
        E.write_json(P.path, cfg)
        print(f"written {P.path}")


if __name__ == "__main__":
    main()
