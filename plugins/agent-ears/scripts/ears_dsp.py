"""Numpy helpers shared by the agent-ears scripts: decoding, windowed inference, spans, a level track, the word mask.

decode()        any ffmpeg-readable file -> mono float32 at sr (optionally high-passed), from t=0 of the file
windows()       overlapping [a, b) sample windows (long takes run in 20 s windows)
overlap_add()   per-window frame probabilities -> one curve, triangular weights (window edges count least)
spans()         runs where prob > thr and longer than min_frames -> [[t0, t1, peak], ...]
Level           16 kHz, high-passed 80 Hz, 30 ms Hann window every 10 ms: dB per hop, silence floor
                (5th percentile), silent = below floor + 10 dB. The same rule the tighten pass uses.
attached_mask() hops inside a word plus the non-silent run contiguous with it (a word's tail, a final « s »,
                a breath running into the next onset)
free_part()     longest stretch of a span outside that mask (>= 50 ms), or None

Imported by ears.py, run_peaframe.py, run_respiro.py, union.py, score.py. Not a command.
"""
import subprocess

import numpy as np

from ears_job import ffmpeg

HOP = 0.01      # Level hop (s)
MIN_FREE = 0.05  # a span needs >= 50 ms outside word-attached sound to survive the word filter


def decode(path, sr, highpass=None):
    af = ["-af", f"highpass=f={highpass}"] if highpass else []
    pcm = subprocess.run([ffmpeg(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), *af,
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.float32)


def windows(n, win, step):
    """Overlapping [a, b) windows covering n samples; the last one ends at n."""
    if n <= win:
        return [(0, n)]
    starts = list(range(0, n - win, step)) + [n - win]
    return [(s, s + win) for s in starts]


def overlap_add(n_frames, chunks):
    """chunks = [(frame_start, probs[..., f])] -> weighted average with a triangular window (edges count least)."""
    acc, wsum = None, np.zeros(n_frames)
    for f0, p in chunks:
        f = p.shape[-1]
        w = np.minimum(np.arange(1, f + 1), np.arange(f, 0, -1)).astype(float)
        if acc is None:
            acc = np.zeros(p.shape[:-1] + (n_frames,))
        f1 = min(n_frames, f0 + f)
        acc[..., f0:f1] += p[..., :f1 - f0] * w[:f1 - f0]
        wsum[f0:f1] += w[:f1 - f0]
    return acc / np.maximum(wsum, 1e-9)


def spans(prob, thr, hop_s, min_frames=1):
    """Runs where prob > thr, longer than min_frames frames -> [[t0, t1, peak], ...] in seconds."""
    prob = np.asarray(prob)
    on = prob > thr
    out, k, n = [], 0, len(prob)
    while k < n:
        if not on[k]:
            k += 1
            continue
        e = k
        while e < n and on[e]:
            e += 1
        if e - k > min_frames:
            out.append([round(k * hop_s, 3), round(e * hop_s, 3), round(float(prob[k:e].max()), 4)])
        k = e
    return out


class Level:
    """dB per 10 ms hop of the file (16 kHz, high-passed 80 Hz: chair and handling rumble otherwise read as voice)."""

    SR = 16000

    def __init__(self, path):
        x = decode(path, self.SR, highpass=80)
        hop, win = int(HOP * self.SR), int(0.03 * self.SR)
        n = max(0, (len(x) - win) // hop)
        db = np.empty(n, np.float32)
        hann = np.hanning(win).astype(np.float32)
        for a in range(0, n, 20000):  # blocks keep memory flat on long takes
            b = min(n, a + 20000)
            idx = np.arange(win)[None, :] + hop * np.arange(a, b)[:, None]
            fr = x[idx] * hann[None, :]
            db[a:b] = 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9)
        self.db = db
        self.floor = float(np.percentile(db, 5)) if n else 0.0
        self.sil = db < self.floor + 10

    def i(self, t):
        return max(0, min(len(self.db) - 1, int(round(t / HOP))))


def attached_mask(level, ws):
    """True on hops inside a word or contiguous (unbroken non-silent run) with one."""
    n = len(level.db)
    m = np.zeros(n, bool)
    for w in ws:
        a, b = level.i(w["start"]), level.i(w["end"])
        m[a:b + 1] = True
        k = b + 1
        while k < n and not level.sil[k]:
            m[k] = True
            k += 1
        k = a - 1
        while k >= 0 and not level.sil[k]:
            m[k] = True
            k -= 1
    return m


def free_part(mask, t0, t1):
    """Longest run of [t0, t1) outside the attached mask, or None when shorter than MIN_FREE."""
    a, b = int(round(t0 / HOP)), int(round(t1 / HOP))
    a, b = max(0, a), min(len(mask), b)
    best, k = None, a
    while k < b:
        if mask[k]:
            k += 1
            continue
        e = k
        while e < b and not mask[e]:
            e += 1
        if best is None or e - k > best[1] - best[0]:
            best = (k, e)
        k = e
    if best is None or (best[1] - best[0]) * HOP < MIN_FREE:
        return None
    return [round(best[0] * HOP, 3), round(best[1] * HOP, 3)]
