# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Join check: how loud is the sound right at every tighten join? A clean cut lands in silence; a cut that clips a word
or a breath reads loud. Light on memory (dB only, chunked): the full tighten analysis costs GBs on a 25-min source.

Level = same dB hops as tighten (high-pass 80 Hz, 16 kHz, 30 ms Hann every 10 ms, floor = 5th percentile), centred
on the cut: the last 30 ms of the outgoing sound and the first 30 ms of the incoming sound, in dB over the floor.
Joins cut inside the voice ("filler (timed, in the voice)", 1-frame cross fade) are loud by design: listed apart.
Pass mark (engine.md): no join > 35 dB over floor (speech sits ~55-68); the reference cut measured p50 8 / 5, max 31.

in:  --project edit.json, --cuts tight.cuts.json
out: summary on stdout; --json PATH also writes every join
usage: uv run join_check.py --project cut/edit.json --cuts cut/tight.cuts.json [--json checks/joins.json] [--top 12]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
SR, HOP, WIN = 16000, 160, 480


def db_track(path):
    pcm = subprocess.run([E.need_ffmpeg(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-af", "highpass=f=80",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, np.float32)
    w = np.hanning(WIN).astype(np.float32) ** 2
    n = (len(x) - WIN) // HOP
    out = np.empty(n, np.float32)
    for c0 in range(0, n, 20000):
        c1 = min(n, c0 + 20000)
        fr = np.lib.stride_tricks.sliding_window_view(x[c0 * HOP:(c1 - 1) * HOP + WIN], WIN)[::HOP]
        out[c0:c1] = 20 * np.log10(np.sqrt(((fr ** 2) * w).mean(1)) + 1e-9)
    return out, float(np.percentile(out, 5)), float(np.percentile(out, 90))


def i(t, n):
    return max(0, min(n - 1, int(round(t * 100))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--cuts", required=True)
    ap.add_argument("--json")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--fail-db", type=float, default=35.0, help="a join louder than this over floor fails (default 35)")
    a = ap.parse_args()
    P = E.Project(a.project)
    r = E.read_json(a.cuts)
    fps = E.norm_fps(r["fps"])
    segs = r["segments"]
    tracks, rows, xrows = {}, [], []
    for x, y in zip(segs, segs[1:]):
        if x["src"] != y["src"] or x.get("alt") != y.get("alt") or y["in_f"] - x["out_f"] <= 0 or x.get("J") is None:
            continue
        if x["src"] not in tracks:
            s = P.src(x["src"])
            tracks[x["src"]] = db_track(P.p(s.get("audio")) or P.file(x["src"]))
        db, fl, sp = tracks[x["src"]]
        n = len(db)
        to, ti = x["out_f"] / fps, y["in_f"] / fps
        lo = float(db[i(to - 0.015, n)] - fl)  # level centred on the cut (30 ms window), outgoing side
        hi = float(db[i(ti - 0.015, n)] - fl)  # incoming side
        row = (round(max(lo, hi)), round(lo), round(hi), x["src"], round(to, 2), round(ti, 2),
               round(x["rec_f"] / fps + (x["out_f"] - x["in_f"]) / fps, 2))
        (xrows if x.get("xf") else rows).append(row)
    if not rows:
        print("no tighten joins (is this a tight cut list?)")
        return
    speech = np.median([sp - fl for _, fl, sp in tracks.values()])
    cuts = {round(c["rec_s"], 2): c for c in r.get("cuts", [])}
    print(f"{len(rows)} tighten joins. speech ~{speech:.0f} dB over floor, silence < 10")
    for name, k in (("outgoing last 30 ms", 1), ("incoming first 30 ms", 2)):
        v = np.array([x[k] for x in rows])
        print(f"  {name}: p50 {np.median(v):.0f}  p90 {np.percentile(v, 90):.0f}  >25 dB: {(v > 25).sum()}  >35 dB: {(v > 35).sum()}")
    ph = [x for x in rows if any(kk.startswith("phrase") for kk in cuts.get(x[6], {}).get("kinds", []))]
    if ph:
        v = np.array([x[0] for x in ph])
        print(f"  phrase-trim joins: {len(ph)}, worse side p50 {np.median(v):.0f}  >25 dB: {(v > 25).sum()}  >35 dB: {(v > 35).sum()}")
    if xrows:
        v = np.array([x[0] for x in xrows])
        print(f"  cut inside the voice + 1-frame cross fade (not in the pass mark): {len(xrows)}, worse side p50 {np.median(v):.0f}  max {v.max():.0f}")
    fails = [x for x in rows if x[0] > a.fail_db]
    rows.sort(key=lambda x: -x[0])
    print("loudest (max, out, in, source, out s, in s):")
    for x in rows[:a.top]:
        c = cuts.get(x[6], {})
        print(f"   {x[:6]}  TC {E.tc(x[6])}  {', '.join(c.get('kinds', []))}  …{c.get('before', '')} ✂ {c.get('after', '')}…")
    print(f"PASS: no join over {a.fail_db:.0f} dB" if not fails else f"FAIL: {len(fails)} joins over {a.fail_db:.0f} dB")
    if a.json:
        keys = ["max_db", "out_db", "in_db", "src", "out_s", "in_s", "timeline_s"]
        E.write_json(a.json, {"joins": [dict(zip(keys, x)) for x in rows], "voice_joins": [dict(zip(keys, x)) for x in xrows],
                              "fail_db": a.fail_db, "fails": len(fails)})


if __name__ == "__main__":
    main()
