# /// script
# requires-python = ">=3.10"
# dependencies = ["opencv-python>=4.8", "numpy"]
# ///
"""Measure the speaker's face in every camera cell of an all-angles master, for comp.py's framing.

  uv run comp_faces.py --spec comp.json [--fps 2] [--to <s>] [--model <yunet.onnx>]

Writes the spec's `faces` file: {cam: [[t, nose_x, nose_y, eye_mid_x, eye_mid_y, face_w, face_h, yaw, score], ...]}
in cell px at full resolution; yaw < 0 = nose left of the eyes (the speaker faces frame-left). Cameras with role `top`
or `"faces": false` are skipped. Decodes at most 3840 px wide (an 8K master at half size), `--fps` samples per second.
Face detector: YuNet (OpenCV Zoo, MIT), downloaded once to ~/.cache/videos/.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"


def model_path(arg):
    if arg:
        return arg
    p = os.path.join(os.path.expanduser("~"), ".cache", "videos", os.path.basename(YUNET_URL))
    if not os.path.exists(p) or os.path.getsize(p) < 100_000:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        req = urllib.request.Request(YUNET_URL, headers={"User-Agent": "videos-plugin"})
        with urllib.request.urlopen(req, timeout=60) as r, open(p + ".part", "wb") as f:
            f.write(r.read())
        os.replace(p + ".part", p)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--to", type=float, help="stop after this many seconds (a quick test)")
    ap.add_argument("--model", help="YuNet .onnx (default: downloaded once)")
    a = ap.parse_args()
    base = os.path.dirname(os.path.abspath(a.spec))
    S = json.load(open(a.spec, encoding="utf-8"))
    rel = lambda p: p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))  # noqa: E731
    master, out_path = rel(S["master"]), rel(S.get("faces", "faces.json"))
    cams = S.get("cams", {})
    cells = {k: v for k, v in S["cells"].items()
             if cams.get(k, {}).get("role", "front") != "top" and cams.get(k, {}).get("faces", True)}
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", master], capture_output=True, text=True, check=True)
    MW, MH = (int(x) for x in r.stdout.strip().split(","))
    k = max(1, -(-MW // 3840))          # decode scale: 2 for an 8K master
    HW, HH = MW // k, MH // k
    yunet = model_path(a.model)
    cmd = ["ffmpeg", "-v", "error", "-i", master, *(["-t", str(a.to)] if a.to else []), "-vf",
           f"fps={a.fps},scale={HW}:{HH}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    dets, out, n = {}, {c: [] for c in S["cells"]}, 0
    while True:
        buf = p.stdout.read(HW * HH * 3)
        if len(buf) < HW * HH * 3:
            break
        fr = np.frombuffer(buf, np.uint8).reshape(HH, HW, 3)
        t = n / a.fps
        for cam, (x, y, w, h) in cells.items():
            crop = np.ascontiguousarray(fr[y // k:(y + h) // k, x // k:(x + w) // k])
            ch, cw = crop.shape[:2]
            if (cw, ch) not in dets:
                dets[(cw, ch)] = cv2.FaceDetectorYN.create(yunet, "", (cw, ch), 0.6, 0.3, 50)
            _, faces = dets[(cw, ch)].detect(crop)
            if faces is None or not len(faces):
                continue
            f = max(faces, key=lambda q: q[2] * q[3])   # the biggest face (a program feed may hold a small preview)
            bw, bh = f[2:4]
            rex, rey, lex, ley, nx, ny = f[4:10]
            em_x, em_y = (rex + lex) / 2, (rey + ley) / 2
            yaw = (nx - em_x) / max(1.0, abs(lex - rex))
            out[cam].append([round(t, 2), *(round(float(v) * k, 1) for v in (nx, ny, em_x, em_y, bw, bh)),
                             round(float(yaw), 3), round(float(f[14]), 3)])
        n += 1
        if n % 60 == 0:
            print(f"{t:.0f}s", {c: len(v) for c, v in out.items()}, flush=True)
    p.wait()
    json.dump(out, open(out_path, "w"), separators=(",", ":"))
    for cam, rows in out.items():
        if rows:
            A = np.array(rows)
            print(cam, len(rows), "nose median", np.median(A[:, 1:3], 0).round(), "face_h median", np.median(A[:, 6]).round(),
                  "yaw median", np.median(A[:, 7]).round(2))
    print(n, "samples ->", out_path)


if __name__ == "__main__":
    main()
