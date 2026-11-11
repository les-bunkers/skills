# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26", "soundfile>=0.12"]
# ///
"""Vertical master for one or more clips: 1080x1920 at the episode fps from plan.json, audio at -14 LUFS solved on
the clip itself.

usage: uv run clip_master.py [-c clips.json] ID [ID…]
in:  <clip>/plan.json (clip_plan.py), episode video, episode "audio" (speech-cleaned track, same time origin as the
     video; else the video's own audio), episode "lut" (.cube, optional)
out: <clip>/assets/master.mp4 (H.264 CRF 14, keyframe every second so HyperFrames seeks never freeze, AAC 320k dual
     mono), <clip>/master.json (frames, loudness, true peak, gain)

Video: one ffmpeg run per shot (accurate seek to (in_f - 0.25)/fps, exactly out_f - in_f frames, frame count checked),
episode grade LUT if any (look_filter), crop or stack per plan.json, lanczos upscale + light unsharp, tagged Rec.709,
then concat (stream copy).
Audio: highpass 60 Hz -> gain G -> compressor -> 4x oversampled limiter at -2 dBFS; G solved (secant) so the clip
reads -14.0 LUFS integrated (dual mono, as a stereo track plays it), true peak <= -1.5 dBTP; equal-power fades at every
cut (8 ms) and at the clip's own in/out (40 ms). Only the kept ranges +-1 s are read and processed.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import FFMPEG, FFPROBE, OUT_H, OUT_W, Batch, fps_arg  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
SCALE = "flags=lanczos"
SHARP = "unsharp=5:5:0.45:5:5:0.0"
SR = 48000
TARGET_I, TP_MAX, LIMIT_DB = -14.0, -1.5, -2.0
GAP = 5 * SR  # zeros between processed spans so compressor/limiter state never crosses spans
# Threshold sits on the loudest ~10 % of syllables at the target level (gain comes first). Measured at plain gain to
# -14 LUFS: 10 ms peaks p90 -3, p99 +0.5..+2.9, max +6..+11 dBFS, 5-8 % of speech windows over -2 dBFS -> broad, not
# rare spikes. A -20 dBFS threshold (first try) crushed all speech and the solver ran the gain to +51 dB.
COMP = "acompressor=threshold=0.316:ratio=3:knee=4:attack=5:release=120:detection=peak"


def chain(g_db, limiter=True):
    f = f"highpass=f=60,volume={g_db:.4f}dB,{COMP}"
    if limiter:
        f += (f",aresample=192000,alimiter=limit={10 ** (LIMIT_DB / 20):.5f}:attack=2:release=60:level=false"
              f":latency=true,aresample=48000:resampler=soxr")
    return f


def ffmpeg_filter(x, filt, tmp):
    a, b = tmp / "in.wav", tmp / "out.wav"
    sf.write(str(a), x, SR, subtype="FLOAT")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(a), "-af", filt,
                    "-ar", str(SR), "-c:a", "pcm_f32le", str(b)], check=True)
    y, _ = sf.read(str(b), dtype="float32")
    if len(y) != len(x):  # resampler edge: pad/trim the tail only, never the head (time origin)
        y = np.pad(y, (0, max(0, len(x) - len(y))))[:len(x)]
    return y


def measure(x, tmp):
    """Integrated loudness, true peak, LRA as a stereo track plays a mono master: dual-mono 2ch (+3.01 dB vs mono)."""
    a = tmp / "measure.wav"
    sf.write(str(a), np.stack([x, x], 1), SR, subtype="FLOAT")
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(a), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, check=True)
    m = json.JSONDecoder().raw_decode(p.stderr[p.stderr.rfind("{"):])[0]
    return float(m["input_i"]), float(m["input_tp"]), float(m["input_lra"])


class Source:
    """Mono 48 kHz samples of the episode audio, read only on the processing spans. WAV/FLAC at 48 kHz are read in
    place; anything else (the video's own audio, mp3, 44.1 kHz) is decoded once into the batch cache."""

    def __init__(self, path, cache):
        self.path = Path(path)
        try:
            info = sf.info(str(self.path))
            ok = info.samplerate == SR
        except RuntimeError:
            ok = False
        if not ok:
            cache.mkdir(parents=True, exist_ok=True)
            wav = cache / f"{self.path.stem}.48k.wav"
            if not wav.exists() or wav.stat().st_mtime < self.path.stat().st_mtime:
                print(f"decoding audio once -> {wav}")
                subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(self.path), "-vn", "-ac", "1", "-ar", str(SR),
                                "-c:a", "pcm_f32le", str(wav)], check=True)
            self.path = wav
        self.n = sf.info(str(self.path)).frames

    def read(self, a, b):
        x, _ = sf.read(str(self.path), start=a, stop=b, dtype="float32", always_2d=True)
        x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
        return np.pad(x, (0, max(0, (b - a) - len(x))))


def spans_for(segs, fps, n):
    """Processing spans = segments +-1 s, merged (dynamics settle before the first kept sample)."""
    spans = []
    for s in sorted(segs, key=lambda s: s["in_f"]):
        a, b = max(0, round((s["in_f"] / fps - 1) * SR)), min(n, round((s["out_f"] / fps + 1) * SR))
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def process(parts, g_db, tmp, limiter=True):
    """Each span's samples through the chain, spans separated by GAP zeros."""
    cat = np.concatenate([q for p in parts for q in (p, np.zeros(GAP, np.float32))])
    y = ffmpeg_filter(cat, chain(g_db, limiter), tmp)
    out, pos = [], 0
    for p in parts:
        out.append(y[pos:pos + len(p)])
        pos += len(p) + GAP
    return out


def timeline(parts, spans, segs, fps):
    """Clip audio: each segment's samples taken from the span that holds it, laid end to end at rec_f."""
    n = round(max(s["rec_f"] + s["out_f"] - s["in_f"] for s in segs) / fps * SR)
    t = np.zeros(n, np.float32)
    for s in segs:
        i0, j0, j1 = (round(s[k] / fps * SR) for k in ("rec_f", "in_f", "out_f"))
        k = next(k for k, (a, b) in enumerate(spans) if a <= j0 < b)
        a = spans[k][0]
        n_seg = min(j1 - j0, n - i0, len(parts[k]) - (j0 - a))
        t[i0:i0 + n_seg] = parts[k][j0 - a:j0 - a + n_seg]
    return t


def edge_fades(y, segs, fps, inner_s=0.008, outer_s=0.04):
    """Equal-power fades at every cut (8 ms: no click at a hard join) and at the clip's own in/out (40 ms: softens a
    syllable of the other speaker sitting on the edge)."""
    y = y.copy()
    for k, s in enumerate(segs):
        a = round(s["rec_f"] / fps * SR)
        b = round((s["rec_f"] + s["out_f"] - s["in_f"]) / fps * SR)
        for edge, n_s, rising in ((a, outer_s if k == 0 else inner_s, True),
                                  (b, outer_s if k == len(segs) - 1 else inner_s, False)):
            n = min(round(n_s * SR), (b - a) // 4)
            ramp = np.sin(np.linspace(0, np.pi / 2, n, dtype=np.float32)) if rising else \
                np.cos(np.linspace(0, np.pi / 2, n, dtype=np.float32))
            if rising:
                y[edge:edge + n] *= ramp
            else:
                y[edge - n:edge] *= ramp
    return y


def audio(b, clip_id, plan, tmp):
    ep, fps, segs = b.episode_of(clip_id), plan["fps"], plan["segments"]
    src = Source(ep["audio_src"], b.out / "_cache")
    spans = spans_for(segs, fps, src.n)
    raw = [src.read(a, z) for a, z in spans]
    i_src = measure(timeline(raw, spans, segs, fps), tmp)[0]
    assert i_src > -60, (f"{clip_id}: audio is silent on these ranges ({i_src} LUFS): the cleaned track does not "
                         "cover them; clean them first")

    def f(g):
        return measure(timeline(process(raw, g, tmp), spans, segs, fps), tmp)

    g0 = TARGET_I - i_src
    i0 = f(g0)[0]
    g1 = g0 + (TARGET_I - i0)
    i1 = f(g1)[0]
    for _ in range(6):
        if abs(i1 - TARGET_I) < 0.05 or i1 == i0:
            break
        g0, i0, g1 = g1, i1, g1 + (TARGET_I - i1) * (g1 - g0) / (i1 - i0)
        i1 = f(g1)[0]
    y = edge_fades(timeline(process(raw, g1, tmp), spans, segs, fps), segs, fps)
    i, tp, lra = measure(y, tmp)
    wav = tmp / "audio.wav"
    sf.write(str(wav), np.stack([y, y], 1), SR, subtype="FLOAT")
    return wav, {"I": i, "TP": tp, "LRA": lra, "gain_db": round(g1, 2), "I_before": i_src, "samples": len(y),
                 "tp_ok": tp <= TP_MAX}


def track_expr(keys):
    """Piecewise-linear crop x(t) through [[t_rel, x], …] (t = seconds from the shot's first frame), held at the ends."""
    e = str(keys[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        e = f"if(lt(t,{t1}),{x0}+({x1 - x0})*(t-{t0})/{t1 - t0:.4f},{e})"
    return f"'if(lt(t,{keys[0][0]}),{keys[0][1]},{e})'"


def shot_filter(s):
    if s["layout"] == "single":
        c = s["crop"]
        x = track_expr(c["track"]) if c.get("track") else c["x"]
        return f"crop={c['w']}:{c['h']}:{x}:{c['y']},scale={OUT_W}:{OUT_H}:{SCALE},{SHARP},setsar=1"
    a, b = s["panels"]
    seam = s.get("seam", OUT_H // 2)  # top panel height (main speaker); captions sit centred on it
    pa = f"crop={a['w']}:{a['h']}:{a['x']}:{a['y']},scale={OUT_W}:{seam}:{SCALE}"
    pb = f"crop={b['w']}:{b['h']}:{b['x']}:{b['y']},scale={OUT_W}:{OUT_H - seam}:{SCALE}"
    return f"split=2[t][b];[t]{pa}[t2];[b]{pb}[b2];[t2][b2]vstack,{SHARP},setsar=1"


def look_filter(lut):
    """Episode grade as a 3D LUT, applied to the source frame before crop/scale: Rec.709 limited YUV -> 16-bit RGB full
    range -> lut3d (tetrahedral) -> back to Rec.709 limited. '' when the episode has none. Exact only for colour-only
    grades (balance, curves, qualifiers); windows, masks, blur, NR, grain and vignettes do not survive a LUT."""
    if not lut:
        return ""
    path = str(lut).replace("\\", "/").replace(":", r"\:")
    return (f"scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb48le,lut3d=file='{path}':interp=tetrahedral,"
            f"scale=in_range=pc:out_color_matrix=bt709:out_range=tv,format=yuv420p,")


def video(plan, tmp, lut=None):
    fps, files = plan["fps"], []
    rate, gop = fps_arg(fps), str(int(round(float(fps))))
    look = look_filter(lut)
    for k, s in enumerate(plan["shots"]):
        f = tmp / f"shot{k:03d}.mp4"
        n = s["out_f"] - s["in_f"]
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{(s['in_f'] - 0.25) / fps:.5f}", "-i", plan["video"],
                        "-an", "-frames:v", str(n), "-filter_complex", f"[0:v]fps={rate},{look}{shot_filter(s)}[v]",
                        "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p",
                        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                        "-g", gop, "-r", rate, str(f)], check=True)
        got = int(subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                  "stream=nb_read_frames", "-of", "csv=p=0", str(f)],
                                 capture_output=True, text=True, check=True).stdout.strip())
        assert got == n, (k, got, n)
        files.append(f)
    lst = tmp / "shots.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    out = tmp / "video.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)],
                   check=True)
    return out


def build(b, clip_id):
    d = b.clip_dir(clip_id)
    plan = json.load(open(d / "plan.json", encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix=f"{clip_id}-") as t:
        tmp = Path(t)
        v = video(plan, tmp, b.episode_of(clip_id).get("lut"))
        a, info = audio(b, clip_id, plan, tmp)
        (d / "assets").mkdir(exist_ok=True)
        out = d / "assets" / "master.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(v), "-i", str(a), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-movflags", "+faststart",
                        "-shortest", str(out)], check=True)
    frames = sum(s["out_f"] - s["in_f"] for s in plan["shots"])
    info.update(frames=frames, duration_s=round(frames / plan["fps"], 3), file="assets/master.mp4")
    (d / "master.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    print(f"{clip_id}: {frames} frames ({frames / plan['fps']:.2f} s), audio {info['I_before']:.1f} -> {info['I']:.2f} LUFS, "
          f"TP {info['TP']:.2f} dBTP, gain {info['gain_db']:+.1f} dB -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("ids", nargs="+", help="clip ids")
    a = ap.parse_args()
    batch = Batch(a.config)
    for cid in a.ids:
        build(batch, cid)
