# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["numpy>=1.26", "librosa>=0.11,<2"]
# ///
"""Music beat map for music-led motion graphics: tempo, beats, downbeats, strong hits and sections of a music bed,
then snap graphic cue times onto that grid and render a click track to check it by ear.

For music-led pieces only (bumpers, offer videos, hook cards, motion-only pieces). Voice-led videos cue on transcript
words instead.

analyze  decodes the first audio stream with ffmpeg (mono 22.05 kHz) and runs librosa: onset envelope (log-mel flux,
         n_fft 1024, hop 256 = 11.6 ms, no centering shift), metrical tempo pick (see estimate_tempo), librosa's
         dynamic-programming beat tracker at that tempo, beat times refined to the onset peak (parabolic, +-2 frames).
         Constant-tempo music (a robust straight-line fit explains >= 70 % of the tracked beats, spread over >= 80 % of
         the piece, within 15 ms rms) gets a steady grid: that line, extended over the whole range, so the first beat of
         a loop at 0.000 and beats through breakdowns are kept and tracker slips are repaired (--grid-mode
         tracked|steady forces either; fit.off_grid lists the stretches where grid and tracker disagree).
         Downbeats: the bar phase (--meter, 4/4 default) whose beats score highest on low-band (< 200 Hz) onset +
         chord change - 0.5 x high-band (> 2 kHz) onset; downbeat_confidence = share of bar bootstrap resamples that
         keep that phase, downbeat_alt = first downbeat of the runner-up phase. Hits: onset peaks with strength =
         onset / 98th percentile of all peaks (1.0 = the track's strongest accents), kept from --hit-min (0.5), each
         tagged on downbeat / beat / half / off. Sections (>= 16 bars of music): Foote novelty on beat-synchronous
         MFCC + chroma, boundaries on downbeats, with each section's level (dBFS) and rise vs the previous one (a big
         rise = a drop).
         Measured (100 bpm Splice loop and a 162 s drum stem, ground truth k x 0.600 s): 100.00 bpm, beats +5.0 and
         -2.5 ms off the true grid (max 5 ms), downbeat phase right on both (confidence 0.73 on 8 bars, 0.99 on 68).
snap     moves event times to the nearest grid point within --max-shift (grids in priority order: downbeat,beat =
         a downbeat when one is in reach, else a beat), rounds to frames with --fps, prints old/new/shift/grid, writes
         a NEW file. Events farther than --max-shift stay where they are and are listed.
click    mixes clicks onto the music (beat 1 kHz, downbeat 1.6 kHz accented, half-beat 700 Hz soft, hit = noise tick
         scaled by strength) so a person or agent-ears can hear whether the grid sits right.

Times are seconds from the start of the file (3 decimals). Inputs are never modified; analyze caches: a rerun with the
same parameters and an output newer than the audio does nothing (--force recomputes).

in:  any audio or video file ffmpeg reads; snap: beats.json + --times or a JSON spec with a dotted --path to a list
out: <stem>.beats.json (analyze, default in the current folder), <spec>.snapped.json (snap), a WAV (click)
usage:
  uv run beats.py analyze bed.wav [--out bed.beats.json] [--start s --end s] [--meter 4] [--bpm 100] [--downbeat 0.0]
                                  [--grid-mode auto|tracked|steady] [--hit-min 0.5] [--force]
  uv run beats.py snap bed.beats.json --times 1.2,3.4 [--grid beat] [--max-shift 0.15] [--fps 30] [--out snapped.json]
  uv run beats.py snap bed.beats.json --json comp.json --path scenes.*.cues --key t [--grid downbeat,beat]
                                  [--offset 0] [--fps 30] [--out comp.snapped.json]
  uv run beats.py click bed.wav bed.beats.json bed.click.wav [--grid beat|downbeat|half|hit] [--music-db -6]
--offset (snap): timeline time of music-file second 0 = (where the bed sits on the timeline) - (its in point).
Needs ffmpeg + ffprobe on PATH (env FFMPEG / FFPROBE override).
"""
import argparse
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
SR = 22050          # analysis rate (librosa default)
HOP = 256           # 11.6 ms onset frames
NFFT = 1024         # 46 ms window (see the onset timing note in analyze)
NMELS = 64          # 128 bands leave empty mel filters at n_fft 1024
CLICK_SR = 48000
VERSION = 2


def r3(x):
    return round(float(x), 3)


def import_librosa():
    """librosa, with numba's JIT cache moved to a short folder when the install path is deep: on Windows numba
    raises (not warns) when a cache file path passes 260 characters (seen with a uv cache in a deep temp folder)."""
    if os.name == "nt" and not os.environ.get("NUMBA_CACHE_DIR"):
        spec = importlib.util.find_spec("librosa")
        if spec and spec.origin and len(spec.origin) > 150:
            os.environ["NUMBA_CACHE_DIR"] = str(Path(tempfile.gettempdir()) / "numba-cache")
    import librosa
    return librosa


# ---------------------------------------------------------------- media

def probe(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=start_time:format=duration,start_time", "-of", "json", str(path)],
                       capture_output=True, text=True, encoding="utf-8")
    d = json.loads(r.stdout or "{}")
    if r.returncode or not d.get("streams"):
        sys.exit(f"no audio stream in {path} ({(r.stderr or '').strip()[:200]})")
    fmt = d.get("format", {})

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    # audio that starts after the container (some video files): file time = sample time + offset
    offset = max(0.0, num(d["streams"][0].get("start_time")) - num(fmt.get("start_time")))
    return {"duration": num(fmt.get("duration")), "audio_offset": offset}


def decode(path, sr, channels, start, end):
    cmd = [FFMPEG, "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{end - start:.6f}"]
    cmd += ["-map", "0:a:0", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        sys.exit(f"ffmpeg failed on {path}: {r.stderr.decode('utf-8', 'replace')[-400:]}")
    y = np.frombuffer(r.stdout, dtype="<f4").astype(np.float32)
    return y.reshape(-1, channels).T.copy() if channels > 1 else y


def window(a, info):
    """(start, end, origin): analysed range in file seconds and the file time of decoded sample 0."""
    start = max(0.0, a.start or 0.0)
    end = min(a.end, info["duration"]) if a.end else info["duration"]
    if end <= start:
        sys.exit(f"empty range: start {start} >= end {end}")
    return start, end, max(start, info["audio_offset"])


# ---------------------------------------------------------------- analysis helpers

def refine(env, frames, radius=2):
    """Sub-frame onset peak near each frame (parabolic), in fractional frames."""
    out = []
    n = len(env)
    for f in frames:
        lo, hi = max(1, int(f) - radius), min(n - 2, int(f) + radius)
        if hi < lo:
            out.append(float(f))
            continue
        k = lo + int(np.argmax(env[lo:hi + 1]))
        a_, b_, c_ = env[k - 1], env[k], env[k + 1]
        den = a_ - 2 * b_ + c_
        d = 0.5 * (a_ - c_) / den if den < 0 else 0.0
        out.append(k + float(np.clip(d, -0.5, 0.5)))
    return np.array(out)


def fit_grid(bt):
    """Robust straight line through beat times: (t0, period, residuals, inlier mask).
    Beats are numbered from the global period (not by counting intervals: a tracker that slips half a beat and back
    would shift every later number by one), then least squares on inliers (|residual| <= max(3 MAD, 10 ms)),
    renumbering and refitting until stable. A single pass let 8 slipped outro beats tilt a steady 78 bpm grid by 28 ms."""
    period = float(np.median(np.diff(bt)))
    t0 = float(np.median(bt - period * np.round((bt - bt[0]) / period)))
    keep = np.ones(len(bt), bool)
    for it in range(10):
        idx = np.round((bt - t0) / period)
        res = bt - (t0 + period * idx)
        mad = float(np.median(np.abs(res[keep])))
        new = np.abs(res) <= max(3 * mad, 0.010)
        if new.sum() < 2:
            break
        A = np.vstack([idx[new], np.ones(int(new.sum()))]).T
        (period, t0), *_r = np.linalg.lstsq(A, bt[new], rcond=None)
        period, t0 = float(period), float(t0)
        if it and np.array_equal(new, keep):
            break
        keep = new
    res = bt - (t0 + period * np.round((bt - t0) / period))
    return t0, period, res, keep


def estimate_tempo(env, lo=50.0, hi=220.0, center=110.0):
    """Metrical tempo: the beat period P whose 1, 2, 4 and 8-beat lags all repeat in the onset autocorrelation,
    times a log-normal prior (center 110 bpm, 1 octave). librosa's own estimate picked 132.5 on a 100 bpm loop whose
    arp runs in dotted eighths (4/3 of the beat); requiring the bar (4P) and 2-bar (8P) lags to repeat rejects that.
    Half/double tempo both pass the metrical test, so the prior decides; --bpm overrides. -> (bpm, candidates)"""
    from scipy.ndimage import maximum_filter1d
    fps = SR / HOP
    n = len(env)
    max_lag = min(n - 2, int(8 * 60 / lo * fps) + 4)
    x = env - env.mean()
    m = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(x, m)
    ac = np.fft.irfft(f * np.conj(f), m)[:max_lag + 1] / (n - np.arange(max_lag + 1))
    ac = maximum_filter1d(ac / (ac[0] + 1e-12), 3)
    idx = np.arange(len(ac))
    T = np.arange(lo, hi + 1e-9, 0.05)
    P = 60.0 / T * fps
    lv = np.stack([np.interp(k * P, idx, ac, right=np.nan) for k in (1, 2, 4, 8)])
    cnt = np.sum(~np.isnan(lv), axis=0)
    raw = np.where(cnt >= 2, np.nansum(lv, axis=0) / np.maximum(cnt, 1), -1.0)
    w = np.clip(raw, 0, None) * np.exp(-0.5 * np.log2(T / center) ** 2)
    order = [i for i in range(1, len(w) - 1) if w[i] >= w[i - 1] and w[i] >= w[i + 1] and w[i] > 0]
    order.sort(key=lambda i: -w[i])
    cands = []
    for i in order:
        if all(abs(T[i] / c - 1) > 0.04 for c, _ in cands):
            cands.append((float(T[i]), float(w[i])))
        if len(cands) == 3:
            break
    if not cands:
        return None, []
    top = cands[0][1]
    return cands[0][0], [{"bpm": round(t, 1), "strength": round(s / top, 2)} for t, s in cands]


def zs(x):
    s = float(np.std(x))
    return (x - np.median(x)) / s if s > 1e-9 else np.zeros_like(x)


def beat_scores(fr, env_low, env_high, chroma):
    """Per-beat downbeat evidence (z-scored): low-band onset (kick, bass note) + chord change across the beat (2 beats
    before vs 2 after) - 0.5 x high-band onset (> 2 kHz: snare and clap sit on the backbeat). A broadband term pulled
    the choice toward the backbeat: strong hits weighed 2-3x more on beats 2 and 4 of a drum stem with a known bar."""
    n = len(fr)
    at = lambda e: np.array([e[max(0, f - 2):f + 3].max() for f in fr])  # noqa: E731
    chg = np.zeros(n)
    for j in range(n):
        a0, b1 = fr[max(0, j - 2)], fr[min(n - 1, j + 2)]
        if fr[j] - a0 < 1 or b1 - fr[j] < 1:
            continue
        u, v = chroma[:, a0:fr[j]].mean(1), chroma[:, fr[j]:b1].mean(1)
        chg[j] = 1 - float(u @ v / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9))
    return zs(at(env_low)) + zs(chg) - 0.5 * zs(at(env_high))


def bar_phase(score, meter, forced=None):
    """(phase, confidence, runner-up phase). Phase = beat index of the first downbeat. Confidence = share of 400
    bootstrap resamples of the bars in which that phase still has the highest mean score."""
    n = len(score)
    nb = n // meter
    if nb < 2:
        return (forced or 0) % meter, 0.0, None
    B = score[:nb * meter].reshape(nb, meter)
    means = B.mean(0)
    order = np.argsort(-means)
    phase = forced % meter if forced is not None else int(order[0])
    alt = int(order[1]) if order[0] == phase else int(order[0])
    rs = B[np.random.default_rng(0).integers(0, nb, (400, nb))].mean(1)
    conf = float(np.mean(np.argmax(rs, 1) == phase))
    return phase, conf, alt


def find_sections(fr, phase, meter, S_db, chroma, y, origin, end, librosa):
    n = len(fr)
    if n < 16 * meter:   # a loop of a few bars has no sections worth naming
        return []
    feats = np.vstack([librosa.feature.mfcc(S=S_db, n_mfcc=13), chroma])
    N = feats.shape[1]
    X = np.stack([feats[:, fr[j]:max(fr[j] + 1, fr[j + 1] if j + 1 < n else N)].mean(1) for j in range(n)], axis=1)
    X = (X - X.mean(1, keepdims=True)) / (X.std(1, keepdims=True) + 1e-9)
    X /= np.linalg.norm(X, axis=0, keepdims=True) + 1e-9
    ssm = X.T @ X
    L = 4 * meter if n >= 32 * meter else 2 * meter
    w = np.exp(-0.5 * ((np.arange(-L, L) + 0.5) / (L / 2)) ** 2)
    sgn = np.where(np.arange(-L, L) < 0, -1.0, 1.0)
    K = np.outer(w * sgn, w * sgn)
    nov = np.zeros(n)
    for i in range(L, n - L + 1):
        nov[i] = float(np.sum(K * ssm[i - L:i + L, i - L:i + L]))
    nov = np.clip(nov, 0, None)
    if nov.max() <= 0:
        return []
    nov /= nov.max()
    peaks = [i for i in range(L, n - L + 1)
             if nov[i] >= 0.3 and nov[i] == nov[max(0, i - L // 2):i + L // 2 + 1].max()]
    downs = np.arange(phase, n, meter)
    bounds = []
    for i in peaks:
        d = int(downs[np.argmin(np.abs(downs - i))])
        if (not bounds or d - bounds[-1][0] >= L) and d >= L and n - d >= L:
            bounds.append((d, float(nov[i])))
    times = [origin] + [origin + fr[d] * HOP / SR for d, _ in bounds] + [end]
    nv = [None] + [v for _, v in bounds]
    out, prev = [], None
    for k in range(len(times) - 1):
        seg = y[int((times[k] - origin) * SR):int((times[k + 1] - origin) * SR)]
        db = 20 * math.log10(float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) + 1e-9) if len(seg) else -120.0
        bars = (bounds[k][0] if k < len(bounds) else n) - (bounds[k - 1][0] if k else 0)
        out.append({"start": r3(times[k]), "end": r3(times[k + 1]), "bars": round(bars / meter, 1),
                    "level_db": round(db, 1), "rise_db": None if prev is None else round(db - prev, 1),
                    "novelty": None if nv[k] is None else round(nv[k], 2)})
        prev = db
    return out


# ---------------------------------------------------------------- analyze

def analyze(a):
    src = Path(a.input)
    if not src.is_file():
        sys.exit(f"not found: {src}")
    info = probe(src)
    start, end, origin = window(a, info)
    out = Path(a.out) if a.out else Path.cwd() / f"{src.stem}.beats.json"
    if out.resolve() == src.resolve():
        sys.exit("--out would overwrite the input")
    params = {"start": r3(start), "end": r3(end), "meter": a.meter, "bpm": a.bpm, "downbeat": a.downbeat,
              "grid_mode": a.grid_mode, "hit_min": a.hit_min, "refine": not a.no_refine, "sr": SR, "hop": HOP,
              "n_fft": NFFT, "version": VERSION,
              "script": hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10]}   # code change = recompute
    if out.exists() and not a.force and out.stat().st_mtime >= src.stat().st_mtime:
        try:
            if E.read_json(out).get("analyzer", {}).get("params") == params:
                print(f"up to date: {out} (--force recomputes)")
                return
        except (ValueError, OSError):
            pass
    t_start = time.time()
    librosa = import_librosa()  # heavy import (numba); only analyze needs it
    y = decode(src, SR, 1, start, end if a.end else None)
    if len(y) < SR * 2:
        sys.exit("need at least 2 s of audio")
    # onset timing: n_fft 1024 and no centering shift put the grid within +-7 ms of the attacks of a drum stem and a
    # synth loop; librosa's default (2048, centered) put every beat 21-41 ms late.
    S_db = librosa.power_to_db(librosa.feature.melspectrogram(y=y, sr=SR, n_fft=NFFT, hop_length=HOP, n_mels=NMELS,
                                                              fmax=SR / 2), ref=np.max)
    env = librosa.onset.onset_strength(S=S_db, sr=SR, hop_length=HOP, n_fft=NFFT, center=False)
    low = librosa.mel_frequencies(n_mels=NMELS, fmax=SR / 2) < 200
    env_low = librosa.onset.onset_strength(S=S_db[low], sr=SR, hop_length=HOP, n_fft=NFFT, center=False)
    high = librosa.mel_frequencies(n_mels=NMELS, fmax=SR / 2) > 2000
    env_high = librosa.onset.onset_strength(S=S_db[high], sr=SR, hop_length=HOP, n_fft=NFFT, center=False)
    chroma = librosa.feature.chroma_stft(y=y, sr=SR, hop_length=HOP)

    est, cands = estimate_tempo(env)
    track_bpm = a.bpm or est
    kw = {"bpm": track_bpm} if track_bpm else {"start_bpm": 120.0}
    _tempo, bfr = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=HOP, trim=True, units="frames", **kw)
    bfr = np.asarray(bfr, dtype=float)
    if len(bfr) < 4:
        sys.exit(f"only {len(bfr)} beats found: is this music with a pulse?")
    tracked = (refine(env, bfr) if not a.no_refine else bfr) * HOP / SR
    ibi = np.diff(tracked)
    steadiness = float(np.mean(np.abs(ibi / np.median(ibi) - 1) <= 0.05))
    t0, period, res, inl = fit_grid(tracked)
    rms_ms = float(np.sqrt(np.mean(res[inl] ** 2)) * 1000)
    inliers = float(np.mean(inl))
    span = float((tracked[inl][-1] - tracked[inl][0]) / (tracked[-1] - tracked[0]))
    # steady = one straight grid explains the beats from start to end; beats before and after an off-grid stretch
    # sharing that line (within 15 ms) means the tempo did not change there: the stretch is a tracker slip (half-beat
    # phase flips in a breakdown or outro), which the grid repairs. Measured: an 85 bpm bed, 77 % of beats on one
    # line at 2.6 ms rms, two slipped stretches.
    steady = rms_ms <= 15 and inliers >= 0.7 and span >= 0.8
    off_grid, run = [], []
    for t, ok in zip(tracked, inl):
        if not ok:
            run.append(t)
        elif run:
            off_grid.append(run)
            run = []
    if run:
        off_grid.append(run)
    off_grid = [[r3(r[0] + origin), r3(r[-1] + origin)] for r in off_grid if len(r) >= 2]
    use_fit = a.grid_mode == "steady" or (a.grid_mode == "auto" and steady)
    if use_fit:
        k0 = math.ceil((0 - t0) / period - 0.25)          # beats may sit up to 1/4 beat before decoded sample 0
        k1 = math.floor((len(y) / SR - t0) / period + 0.25)
        bt = t0 + period * np.arange(k0, k1 + 1)
        bt = bt[(bt >= -0.02) & (bt <= len(y) / SR - 0.02)].clip(0, None)
    else:
        bt = tracked
    bpm = 60.0 / period if use_fit else 60.0 / float(np.median(ibi))
    fr = np.clip(np.round(bt * SR / HOP).astype(int), 0, len(env) - 1)

    forced = None
    if a.downbeat is not None:
        forced = int(np.argmin(np.abs(bt + origin - a.downbeat)))
    phase, db_conf, alt = bar_phase(beat_scores(fr, env_low, env_high, chroma), a.meter, forced)
    beats = [r3(t + origin) for t in bt]
    downbeats = beats[phase::a.meter]
    alt_downbeat = beats[alt] if alt is not None and alt < len(beats) else None

    # hits: onset peaks, strength vs the track's strong accents
    of = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=HOP, units="frames", backtrack=False)
    hits = []
    if len(of):
        vals = env[of]
        ref = float(np.percentile(vals, 98)) or 1.0
        tol = min(0.05, period / 8)
        half = (bt[:-1] + bt[1:]) / 2
        dbt = bt[phase::a.meter]
        for f, t in zip(of, refine(env, of) * HOP / SR):
            s = min(1.0, float(env[f]) / ref)
            if s < a.hit_min:
                continue
            on = "off"
            for name, g in (("downbeat", dbt), ("beat", bt), ("half", half)):
                if len(g) and np.min(np.abs(g - t)) <= tol:
                    on = name
                    break
            hits.append({"t": r3(t + origin), "strength": round(s, 2), "on": on})

    sections = find_sections(fr, phase, a.meter, S_db, chroma, y, origin, end, librosa)
    secs = time.time() - t_start
    data = {
        "source": str(src.resolve()), "duration": r3(info["duration"]), "start": r3(origin), "end": r3(end),
        "bpm": round(bpm, 2), "bpm_confidence": round(steadiness, 2), "tempo_candidates": cands,
        "meter": a.meter, "bar_phase": phase, "downbeat_confidence": round(db_conf, 2),
        "downbeat_alt": alt_downbeat,
        "beat_source": "steady" if use_fit else "tracked",
        "fit": {"t0": round(t0 + origin, 4), "period": round(period, 5), "rms_ms": round(rms_ms, 1),
                "max_ms": round(float(np.max(np.abs(res[inl]))) * 1000, 1), "inliers": round(inliers, 2),
                "steady": steady, "off_grid": off_grid},
        "beats": beats, "downbeats": downbeats, "hits": hits, "sections": sections,
        "analyzer": {"tool": "beats.py", "librosa": librosa.__version__, "numba": importlib.metadata.version("numba"),
                     "numpy": np.__version__, "params": params, "seconds": round(secs, 2)},
    }
    E.write_json(out, data)
    print(f"{src.name}: {data['bpm']} bpm (confidence {data['bpm_confidence']}, {data['beat_source']} grid, "
          f"fit rms {rms_ms:.1f} ms on {inliers:.0%} of beats), {len(beats)} beats, {len(downbeats)} downbeats "
          f"(first {downbeats[0] if downbeats else '-'} s, confidence {data['downbeat_confidence']}), "
          f"{len(hits)} hits >= {a.hit_min}, {len(sections)} sections  [{secs:.1f} s]")
    alts = ", ".join(f"{c['bpm']} ({c['strength']})" for c in data["tempo_candidates"])
    print(f"  tempo candidates: {alts}")
    if db_conf < 0.9 and alt_downbeat is not None and a.downbeat is None:
        print(f"  downbeat uncertain: runner-up bar starts at {alt_downbeat} s (check: click --grid downbeat; "
              f"set: --downbeat <s>)")
    if not steady and a.grid_mode == "auto":
        print("  tempo not steady: tracked beats kept (--grid-mode steady forces a straight grid)")
    if off_grid:
        what = "grid replaces tracked beats" if use_fit else "tracked beats leave the straight grid"
        print(f"  {what} at " + ", ".join(f"{s}-{e}" for s, e in off_grid) + " s: listen there with click")
    for s in sections:
        rise = "" if s["rise_db"] is None else f"  rise {s['rise_db']:+.1f} dB"
        print(f"  section {s['start']:8.3f}-{s['end']:8.3f}  {s['bars']:5.1f} bars  {s['level_db']:6.1f} dBFS{rise}")
    top = sorted(hits, key=lambda h: -h["strength"])[:5]
    if top:
        print("  strongest hits: " + ", ".join(f"{h['t']} ({h['strength']}, {h['on']})" for h in sorted(top, key=lambda h: h["t"])))
    print(f"wrote {out}")


# ---------------------------------------------------------------- snap

def load_beats(path):
    d = E.read_json(path)
    for k in ("beats", "downbeats", "hits"):
        if k not in d:
            sys.exit(f"{path} is not a beats.json (no '{k}')")
    return d


def grid_points(b, name, min_strength):
    """(times, labels) of one grid."""
    beats = b["beats"]
    m, ph = b.get("meter", 4), b.get("bar_phase", 0)

    def bb(i):
        k = i - ph
        return f"{k // m + 1}.{k % m + 1}"
    if name == "beat":
        return beats, [f"beat {bb(i)}" for i in range(len(beats))]
    if name in ("downbeat", "bar"):
        return b["downbeats"], [f"downbeat bar {(beats.index(t) - ph) // m + 1}" if t in beats else "downbeat"
                                for t in b["downbeats"]]
    if name == "half":
        pts, lab = [], []
        for i, t in enumerate(beats):
            pts.append(t)
            lab.append(f"beat {bb(i)}")
            if i + 1 < len(beats):
                pts.append(r3((t + beats[i + 1]) / 2))
                lab.append(f"beat {bb(i)}+")
        return pts, lab
    if name == "hit":
        hs = [h for h in b["hits"] if h["strength"] >= min_strength]
        return [h["t"] for h in hs], [f"hit {h['strength']:.2f} ({h.get('on', '?')})" for h in hs]
    sys.exit(f"unknown grid '{name}' (beat, downbeat, half, hit)")


def walk(node, parts, trail):
    """Yield (list, trail) for every list the dotted path reaches; '*' fans out over a list or dict."""
    if not parts:
        if not isinstance(node, list):
            sys.exit(f"--path {'.'.join(trail) or '(root)'} is a {type(node).__name__}, not a list")
        yield node, trail
        return
    p, rest = parts[0], parts[1:]
    if p == "*":
        items = enumerate(node) if isinstance(node, list) else node.items()
        for k, v in items:
            yield from walk(v, rest, trail + [str(k)])
        return
    try:
        nxt = node[int(p)] if isinstance(node, list) else node[p]
    except (KeyError, IndexError, ValueError, TypeError):
        sys.exit(f"--path: nothing at '{'.'.join(trail + [p])}'")
    yield from walk(nxt, rest, trail + [p])


def collect(spec, path, keys):
    """[(time, label, setter)] for every numeric event under path."""
    events = []
    for lst, trail in walk(spec, [p for p in path.split(".") if p] if path else [], []):
        base = ".".join(trail)
        for i, el in enumerate(lst):
            if isinstance(el, (int, float)) and not isinstance(el, bool):
                events.append((float(el), f"{base}[{i}]", (lst, i)))
            elif isinstance(el, dict):
                for k in keys:
                    v = el.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        events.append((float(v), f"{base}[{i}].{k}", (el, k)))
    return events


def snap(a):
    bpath = Path(a.beats)
    b = load_beats(bpath)
    grids = [(g, *grid_points(b, g, a.min_strength)) for g in a.grid.split(",") if g]
    fps = E.norm_fps(a.fps) if a.fps else None
    spec = None
    if a.json:
        spath = Path(a.json)
        spec = copy.deepcopy(E.read_json(spath))
        events = collect(spec, a.path or "", [k for k in a.key.split(",") if k])
        if not events:
            sys.exit(f"no numeric '{a.key}' values under --path {a.path!r} in {spath}")
        out = Path(a.out) if a.out else spath.with_name(spath.name.removesuffix(".json") + ".snapped.json")
        if out.resolve() in (spath.resolve(), bpath.resolve()):
            sys.exit("--out would overwrite an input; name a new file")
    else:
        try:
            events = [(float(t), f"t{i + 1}", None) for i, t in enumerate(a.times.split(",")) if t.strip()]
        except ValueError:
            sys.exit("--times takes seconds separated by commas: 1.2,3.4")
        out = Path(a.out) if a.out else None
        if out and out.resolve() == bpath.resolve():
            sys.exit("--out would overwrite the beats file")

    rows, kept, used = [], [], {}
    for t, label, setter in events:
        hit = None
        near = None
        for gname, pts, labs in grids:
            if not pts:
                continue
            tl = np.asarray(pts) + a.offset
            k = int(np.argmin(np.abs(tl - t)))
            d = float(tl[k] - t)
            if near is None or abs(d) < abs(near[1]):
                near = (gname, d)
            if abs(d) <= a.max_shift:
                hit = (gname, float(tl[k]), labs[k])
                break
        if hit is None:
            rows.append({"event": label, "old": r3(t), "new": r3(t), "shift_ms": 0, "grid": None, "snapped": False})
            kept.append((label, t, near))
            continue
        new = hit[1]
        if fps:
            new = round(new * fps) / fps
        rows.append({"event": label, "old": r3(t), "new": r3(new), "shift_ms": round((new - t) * 1000),
                     "grid": hit[2], "snapped": True, **({"frame": round(new * fps)} if fps else {})})
        used.setdefault(r3(new), []).append(label)
        if setter is not None:
            setter[0][setter[1]] = r3(new)

    w = max([len(r["event"]) for r in rows] + [5])
    print(f"{'event':<{w}}  {'old':>8}  {'new':>8}  {'shift':>7}  grid")
    for r in rows:
        fr = f"  f{r['frame']}" if "frame" in r else ""
        g = r["grid"] or "kept"
        print(f"{r['event']:<{w}}  {r['old']:8.3f}  {r['new']:8.3f}  {r['shift_ms']:+5d} ms  {g}{fr}")
    if kept:
        print(f"\n{len(kept)} event(s) beyond --max-shift {a.max_shift} s, left in place:")
        for label, t, near in kept:
            hint = f"nearest {near[0]} {near[1] * 1000:+.0f} ms" if near else "empty grid"
            print(f"  {label} at {t:.3f} ({hint})")
    for t, labels in used.items():
        if len(labels) > 1:
            print(f"warning: {', '.join(labels)} all landed on {t:.3f}")
    n = sum(r["snapped"] for r in rows)
    print(f"\n{n}/{len(rows)} snapped (grid {a.grid}, max shift {a.max_shift} s"
          f"{f', {a.fps} fps' if fps else ''}{f', offset {a.offset} s' if a.offset else ''})")
    if spec is not None:
        E.write_json(out, spec)
        print(f"wrote {out}")
    elif out:
        E.write_json(out, {"beats": str(bpath.resolve()), "grid": a.grid, "max_shift": a.max_shift, "fps": a.fps,
                           "offset": a.offset, "events": rows})
        print(f"wrote {out}")


# ---------------------------------------------------------------- click

def tone(freq, dur, amp, sr=CLICK_SR):
    t = np.arange(int(dur * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t) * np.exp(-t / (dur / 4))).astype(np.float32)


def tick(dur, amp, seed, sr=CLICK_SR):
    rng = np.random.default_rng(seed)
    n = int(dur * sr)
    x = rng.standard_normal(n).astype(np.float32)
    x = np.diff(x, prepend=0)       # brighter
    return (amp * x / (np.abs(x).max() + 1e-9) * np.exp(-np.arange(n) / (n / 5))).astype(np.float32)


def click(a):
    src, bpath, out = Path(a.input), Path(a.beats), Path(a.output)
    if out.resolve() in (src.resolve(), bpath.resolve()):
        sys.exit("output would overwrite an input; name a new file")
    b = load_beats(bpath)
    info = probe(src)
    start = b.get("analyzer", {}).get("params", {}).get("start", 0.0)
    end = b.get("end", info["duration"])
    origin = b.get("start", 0.0)
    y = decode(src, CLICK_SR, 2, start, end if end < info["duration"] - 1e-3 else None)
    mix = y * 10 ** (a.music_db / 20)
    n = mix.shape[1]
    beats = b["beats"]
    downs = set(b["downbeats"])
    sounds = []
    for g in a.grid.split(","):
        if g == "beat":
            sounds += [(t, tone(1600, 0.04, 0.6) if t in downs else tone(1000, 0.03, 0.35)) for t in beats]
        elif g in ("downbeat", "bar"):
            sounds += [(t, tone(1600, 0.04, 0.6)) for t in b["downbeats"]]
        elif g == "half":
            sounds += [((x + y_) / 2, tone(700, 0.02, 0.2)) for x, y_ in zip(beats, beats[1:])]
        elif g == "hit":
            sounds += [(h["t"], tick(0.015, 0.2 + 0.5 * h["strength"], i)) for i, h in enumerate(b["hits"])
                       if h["strength"] >= a.min_strength]
        else:
            sys.exit(f"unknown grid '{g}' (beat, downbeat, half, hit)")
    for t, s in sounds:
        i = int(round((t - origin) * CLICK_SR))
        if 0 <= i < n:
            j = min(n, i + len(s))
            mix[:, i:j] += s[:j - i]
    peak = float(np.abs(mix).max())
    if peak > 0.98:
        mix *= 0.98 / peak
    out.parent.mkdir(parents=True, exist_ok=True)
    data = np.ascontiguousarray(mix.T).astype("<f4").tobytes()
    r = subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "f32le", "-ar", str(CLICK_SR), "-ac", "2", "-i", "-",
                        "-c:a", "pcm_s16le", str(out)], input=data, capture_output=True)
    if r.returncode:
        sys.exit(f"ffmpeg failed writing {out}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    print(f"wrote {out} ({n / CLICK_SR:.1f} s from {origin:.3f} s, {len(sounds)} clicks, grid {a.grid}, "
          f"music {a.music_db:+.0f} dB)")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="tempo, beats, downbeats, hits, sections -> beats.json")
    p.add_argument("input")
    p.add_argument("--out", help="default: ./<stem>.beats.json")
    p.add_argument("--start", type=float, help="analyse from this file second")
    p.add_argument("--end", type=float, help="analyse up to this file second")
    p.add_argument("--meter", type=int, default=4, help="beats per bar (4)")
    p.add_argument("--bpm", type=float, help="known tempo: track at exactly this bpm (fixes half/double-tempo picks)")
    p.add_argument("--downbeat", type=float, help="a known downbeat (file seconds): sets the bar phase")
    p.add_argument("--grid-mode", choices=["auto", "tracked", "steady"], default="auto",
                   help="auto = straight grid when the tempo is steady, else tracked beats")
    p.add_argument("--hit-min", type=float, default=0.5, help="keep hits from this strength (0-1)")
    p.add_argument("--no-refine", action="store_true", help="keep beats on onset frames (no sub-frame peak)")
    p.add_argument("--force", action="store_true", help="recompute even when the output is up to date")

    p = sub.add_parser("snap", help="move event times onto the beat grid -> new JSON")
    p.add_argument("beats")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--times", help="seconds, comma separated")
    src.add_argument("--json", help="JSON spec holding the events")
    p.add_argument("--path", default="", help="dotted path to the event list in --json (ints index lists, * fans out)")
    p.add_argument("--key", default="t", help="time field(s) of each event, comma separated (t)")
    p.add_argument("--grid", default="beat", help="beat|downbeat|half|hit, or a priority list: downbeat,beat")
    p.add_argument("--max-shift", type=float, default=0.15, help="farthest move in seconds (0.15)")
    p.add_argument("--fps", type=float, help="round snapped times to this frame rate")
    p.add_argument("--offset", type=float, default=0.0,
                   help="timeline time of music second 0 (= timeline position - in point); events are timeline times")
    p.add_argument("--min-strength", type=float, default=0.0, help="hit grid: skip weaker hits")
    p.add_argument("--out", help="default for --json: <spec>.snapped.json beside it")

    p = sub.add_parser("click", help="music + clicks on the grid -> WAV")
    p.add_argument("input", help="the music file analysed")
    p.add_argument("beats")
    p.add_argument("output", help="new WAV")
    p.add_argument("--grid", default="beat", help="beat (downbeats accented), downbeat, half, hit; comma list mixes")
    p.add_argument("--music-db", type=float, default=-6.0, help="music gain under the clicks (-6)")
    p.add_argument("--min-strength", type=float, default=0.0, help="hit grid: skip weaker hits")

    a = ap.parse_args()
    {"analyze": analyze, "snap": snap, "click": click}[a.cmd](a)


if __name__ == "__main__":
    main()
