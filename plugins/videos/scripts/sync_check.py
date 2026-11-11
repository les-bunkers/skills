# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Lag between two renders (or any two files) of the same material, measured on several 2 s windows.

Use it after a build or render to prove two tracks line up (e.g. a render of A1 only vs A2 only, or a master vs the
program sound): every window should give the same lag, 0 when they are in sync. A correlation near 1 with lag 0 on
identical files proves nothing about a track toggle: rebuild the expected sound instead (finals_check.py content).
in:  two media files (any format ffmpeg reads; mono mix at --sr)
out: per window: time, lag in samples and ms (+ = the second file is late), peak correlation; then the set of lags
usage: uv run sync_check.py a.wav b.wav [--sr 48000] [--windows 8]
"""
import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()


def load(p, sr):
    raw = subprocess.run([E.need_ffmpeg(), "-v", "error", "-i", str(p), "-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--sr", type=int, default=48000)
    ap.add_argument("--windows", type=int, default=8)
    a = ap.parse_args()
    sr = a.sr
    x0, y0 = load(a.a, sr), load(a.b, sr)
    n = min(len(x0), len(y0))
    print(f"len a={len(x0)} b={len(y0)} sr={sr}")
    W, S = 2 * sr, sr // 20  # 2 s windows, search +-50 ms
    lags = []
    step = max(1, (n - W - 2 * S) // a.windows)
    for start in range(S, n - W - S, step):
        x = x0[start:start + W]
        y = y0[start - S:start + W + S]
        if np.sqrt(np.mean(x ** 2)) < 1e-3:
            continue
        m = 1 << int(np.ceil(np.log2(len(x) + len(y))))
        c = np.fft.irfft(np.fft.rfft(y, m) * np.conj(np.fft.rfft(x, m)), m)[:2 * S + 1]
        k = int(np.argmax(c))
        seg = y[k:k + W]
        corr = float(np.dot(x, seg) / (np.linalg.norm(x) * np.linalg.norm(seg) + 1e-12))
        lags.append(k - S)
        print(f"t={start / sr:7.2f}s lag={k - S:+d} samples ({(k - S) / sr * 1000:+.2f} ms) corr={corr:.4f}")
    print("lags:", sorted(set(lags)))


if __name__ == "__main__":
    main()
