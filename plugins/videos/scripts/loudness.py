# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Measure or fix the loudness of the file that ships (-14.0 LUFS integrated, true peak <= -1.5 dBTP by default).

Why on the shipped file: audio mastered over a whole source cut reads lower on a short excerpt (a 10 s excerpt of a
cut mastered to -14.0 read -15.75), and the shipped file also carries the end card, music or anything a renderer
mixed in. So the video stream is copied bit for bit and only the audio is re-processed and re-encoded once (AAC 320k),
with the gain solved (secant) on the ENCODED output until a BS.1770 meter (ffmpeg ebur128) reads the target.

Chains:
  trim (already mastered audio, plain gain within --trim-max dB): gain -> 4x oversampled limiter at -2 dBFS
  full (raw audio): highpass 60 Hz -> gain -> compressor (-10 dBFS, 3:1, knee 4, attack 5 ms, release 120 ms, peak)
       -> the same limiter. A -20 dBFS threshold crushed speech and ran the gain to +51 dB in testing, hence the guard:
       the solved gain may exceed plain gain-to-target by at most --max-extra dB.
Integrated loudness comes from ebur128 (gated, silence-proof); ffmpeg loudnorm's input_i under-reads files with a
silent tail (-15.59 vs -15.91 with 2.6 s of digital zeros), so loudnorm is used only for true peak and LRA.

usage:
  uv run loudness.py measure <file> [<file> ...]
  uv run loudness.py fix <in.mp4> <out.mp4> [--target -14] [--tp -1.5] [--full] [--json report.json]
Checks on fix: video packet count unchanged, audio duration within 30 ms, lag vs the input = 0 (+-1 sample) at the 3
loudest 2 s windows. Exit 1 when a check fails. Needs ffmpeg + ffprobe on PATH (env FFMPEG / FFPROBE override).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
SR = 48000
LIMIT_DB = -2.0
COMP = "acompressor=threshold=0.316:ratio=3:knee=4:attack=5:release=120:detection=peak"
TOL_LU = 0.05


def limiter():
    return (f"aresample=192000,alimiter=limit={10 ** (LIMIT_DB / 20):.5f}:attack=2:release=60:level=false:latency=true,"
            f"aresample={SR}:resampler=soxr")


def trim_chain(g_db):
    return f"volume={g_db:.4f}dB,{limiter()}"


def full_chain(g_db):
    return f"highpass=f=60,volume={g_db:.4f}dB,{COMP},{limiter()}"


def integrated(path):
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af",
                        "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.I:file=-", "-f", "null", "-"],
                       capture_output=True, text=True, check=True)
    return float(p.stdout.strip().rsplit("lavfi.r128.I=", 1)[1].split()[0])


def peak_lra(path):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af", "loudnorm=print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True, check=True)
    m = json.JSONDecoder().raw_decode(p.stderr[p.stderr.rfind("{"):])[0]
    return float(m["input_tp"]), float(m["input_lra"])


def loudness(path):
    tp, lra = peak_lra(path)
    return integrated(path), tp, lra


def encode(src, dst, filt):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0?", "-map", "0:a:0",
                    "-c:v", "copy", "-af", filt, "-c:a", "aac", "-b:a", "320k", "-ar", str(SR), "-map_metadata", "0",
                    "-movflags", "+faststart", str(dst)], check=True)


def pcm(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-count_packets", "-show_entries",
                          "stream=codec_type,codec_name,nb_read_packets,duration,bit_rate", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def lag_samples(a, b, center, width=SR * 2, search=SR // 10):
    a = a[center:center + width].astype(np.float64)
    b = b[max(0, center - search):center + width + search].astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[:2 * search + 1]
    return int(np.argmax(c)) - search


def speech_probes(x, n=3, width=SR * 2):
    """Start samples of the n loudest 2 s windows, >= 2 s apart (a probe on silence or an end card reads garbage)."""
    hop, margin = SR // 2, SR // 10
    starts = list(range(max(margin, SR // 10 + 1), len(x) - width - margin, hop))
    energy = sorted(((float(np.mean(x[s:s + width] ** 2)), s) for s in starts), reverse=True)
    picked = []
    for _, s in energy:
        if all(abs(s - p) >= width for p in picked):
            picked.append(s)
        if len(picked) == n:
            break
    return sorted(picked)


def fix(src, dst, target, tp_max, force_full, trim_max, max_extra):
    i_src, tp_src, _ = loudness(src)
    g_plain = target - i_src
    full = force_full or g_plain > trim_max
    make = full_chain if full else trim_chain
    hist = []

    def f(g):
        encode(src, dst, make(g))
        i, tp, _ = loudness(dst)
        hist.append([round(g, 3), i, tp])
        print(f"    gain {g:+.2f} dB -> {i:.2f} LUFS, TP {tp:.2f} dBTP", flush=True)
        return i

    g0, i0 = g_plain, f(g_plain)
    g1, i1 = (g0, i0) if abs(i0 - target) < TOL_LU else (g0 + (target - i0), None)
    if i1 is None:
        i1 = f(g1)
    for _ in range(6):
        if abs(i1 - target) < TOL_LU or i1 == i0:
            break
        g0, i0, g1 = g1, i1, g1 + (target - i1) * (g1 - g0) / (i1 - i0)
        if g1 > g_plain + max_extra:
            sys.exit(f"{src.name}: gain {g1:.1f} dB runs > {max_extra} dB past plain {g_plain:.1f} dB "
                     "(compressor too hard for this material)")
        i1 = f(g1)
    if hist[-1][0] != round(g1, 3):
        f(g1)
    i_fin, tp_fin, lra_fin = loudness(dst)
    ps, pd = probe(src), probe(dst)
    a, b = pcm(src), pcm(dst)
    lags = [lag_samples(a, b, n) for n in speech_probes(a)]
    vp = [int(ps.get("video", {}).get("nb_read_packets", 0)), int(pd.get("video", {}).get("nb_read_packets", 0))]
    dur = [float(ps["audio"]["duration"]), float(pd["audio"]["duration"])]
    rec = {"input": str(src), "output": str(dst), "chain": "full" if full else "trim", "filter": make(g1),
           "gain_db": round(g1, 3), "plain_gain_db": round(g_plain, 3),
           "input_audio": {"I": i_src, "TP": tp_src}, "output_audio": {"I": i_fin, "TP": tp_fin, "LRA": lra_fin},
           "video_packets": vp, "audio_duration_s": dur, "lag_samples": lags, "solve_history": hist}
    rec["ok"] = (abs(i_fin - target) < 0.1 and tp_fin <= tp_max and vp[0] == vp[1] and all(abs(x) <= 1 for x in lags)
                 and abs(dur[0] - dur[1]) < 0.03)
    print(f"  {dst.name}: {i_src:.2f} -> {i_fin:.2f} LUFS, TP {tp_src:.2f} -> {tp_fin:.2f} dBTP, gain {g1:+.2f} dB "
          f"({rec['chain']}), video packets {vp}, lags {lags} -> {'OK' if rec['ok'] else 'FAIL'}", flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[1:]))
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("files", nargs="+")
    x = sub.add_parser("fix")
    x.add_argument("src")
    x.add_argument("dst")
    x.add_argument("--target", type=float, default=-14.0)
    x.add_argument("--tp", type=float, default=-1.5, help="max true peak of the output, dBTP")
    x.add_argument("--full", action="store_true", help="force the full chain (raw, unmastered audio)")
    x.add_argument("--trim-max", type=float, default=4.0, help="plain gain above this = raw audio = full chain")
    x.add_argument("--max-extra", type=float, default=6.0)
    x.add_argument("--json", help="append the report to this JSON file (keyed by output name)")
    a = ap.parse_args()
    if a.cmd == "measure":
        for f in a.files:
            i, tp, lra = loudness(f)
            print(f"{f}: {i:.2f} LUFS, TP {tp:.2f} dBTP, LRA {lra:.1f} LU")
        return
    src, dst = Path(a.src).resolve(), Path(a.dst).resolve()
    if src == dst:
        sys.exit("output must differ from input (the input is never modified)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    rec = fix(src, dst, a.target, a.tp, a.full, a.trim_max, a.max_extra)
    if a.json:
        jp = Path(a.json)
        rep = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else {}
        rep[dst.name] = rec
        jp.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    sys.exit(0 if rec["ok"] else 1)


if __name__ == "__main__":
    main()
