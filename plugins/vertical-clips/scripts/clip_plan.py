# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python-headless>=4.8", "numpy>=1.26"]
# ///
"""Shots + faces -> 9:16 layout plan for one or more clips.

usage: uv run clip_plan.py [-c clips.json] ID [ID…]
in:  clips.json (episode video, fps, main_side; clip kept ranges), <clip>/edges.json if any
out: <clip>/plan.json (rewritten only when it changed, so fresh masters and renders stay fresh), <clip>/plan_sheet.jpg
     (one tile per shot: source frame with face boxes and crop | result), <clip>/<ID>.cuts.json (shared Cut list)

1. Shots: relative frame-difference cut detector inside each kept range; range seams are cuts too.
2. Faces: YuNet (OpenCV) on 3 frames per shot (25/50/75 %), score >= 0.6, width >= 2.5 % of frame, not touching the
   left/right 1.5 % edge (over-the-shoulder foreground), centre above the bottom 45 % (hands/mic); clustered by x.
3. Layout per shot:
   - 1 person, or 2 that fit one 9:16 window -> "single": full-height 9:16 window; one person = sticky crop (fixed
     while the face stays inside it, one eased pan only when it would leave, see track());
   - 2+ people too far apart -> "stack": top panel 1080 x seam on the main speaker (episode "main_side" = their seat
     in the wide shot), bottom panel 1080 x (1920 - seam) on the other; face ~24 % of panel height, eyes at 40 %
     (top) / 36 % (bottom, clear of the platform caption area). Captions sit centred on the seam (build_vertical.py);
   - 0 people -> "single" centred, flagged "review";
   - single whose face ends below layout.low_face -> lift(): tighter bottom-anchored crop (<= MAX_LIFT x).
Override any shot by editing plan.json ("layout", "crop", "panels") before clip_master.py; the plan is only rebuilt
when the clip's kept ranges change.
YuNet model: downloaded once into $VERTICAL_CLIPS_CACHE (or config "cache_dir", default ~/.cache/vertical-clips).
"""
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import FFMPEG, FFPROBE, OUT_H, OUT_W, YUNET_SHA256, YUNET_URL, Batch  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
CUT_RATIO, CUT_MIN = 6.0, 4.0
MIN_SHOT_S = 0.4
FACE_IN_PANEL = 0.24  # stacked panel: face height / panel height (head + shoulders + mic)
TRACK_RATE = 3.0      # face samples per second inside a single shot
STICKY_MARGIN = 0.10  # face box must stay this far (x crop width) inside the crop before the crop moves
MOVE_S = 0.6          # one eased pan when it must move
MIN_HOLD_S = 1.0      # a shorter hold between two pans merges into a neighbour when the face still fits
MAX_LIFT = 1.2        # lift(): most a low face may be zoomed in


def yunet(batch):
    """Path of the YuNet 2023mar face model, downloaded and hash-checked on first use."""
    p = batch.cache_dir / "face_detection_yunet_2023mar.onnx"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading YuNet -> {p}")
        data = urllib.request.urlopen(YUNET_URL, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != YUNET_SHA256:
            raise SystemExit(f"YuNet download hash mismatch ({YUNET_URL}); put the file at {p} by hand")
        p.write_bytes(data)
    return str(p)


def probe_size(video):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def scene_cuts(video, t0, t1, fps):
    """Source times of hard cuts strictly inside (t0, t1). Relative detector: mean abs difference of 64x36 gray
    frames; a cut = a frame whose difference is >= CUT_RATIO x the median of its +-1 s neighbourhood and >= CUT_MIN.
    (ffmpeg's scene score peaked at 0.27 on real cuts in a dark set: a fixed 0.3 threshold missed every one.)"""
    p = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", str(video), "-an",
                        "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True)
    fr = np.frombuffer(p.stdout, np.uint8).reshape(-1, 36, 64).astype(np.float32)
    if len(fr) < 3:
        return []
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))  # d[i] = change from frame i to i+1
    k = int(round(fps))
    cuts = []
    for i in range(len(d)):
        nb = np.concatenate([d[max(0, i - k):i], d[i + 1:i + 1 + k]])
        if d[i] >= CUT_MIN and len(nb) and d[i] >= CUT_RATIO * max(np.median(nb), 0.5):
            if not cuts or i - cuts[-1] > 3:
                cuts.append(i)
    return [t0 + (i + 1) / fps for i in cuts]


def frame_at(video, t, W, H):
    p = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, check=True)
    return np.frombuffer(p.stdout, np.uint8)[: W * H * 3].reshape(H, W, 3).copy()


def frames_between(video, t0, t1, rate, W, H, scale=2):
    """Half-size BGR frames at `rate` fps from t0 to t1 (one decode); frame k ~ t0 + k / rate."""
    w, h = W // scale, H // scale
    p = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", str(video), "-an",
                        "-vf", f"fps={rate},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                       capture_output=True, check=True)
    fr = np.frombuffer(p.stdout, np.uint8)
    n = len(fr) // (w * h * 3)
    return [fr[i * w * h * 3:(i + 1) * w * h * 3].reshape(h, w, 3) for i in range(n)], scale


def track(video, fa, fb, fps, W, H, det, person, cw):
    """Sticky crop for a one-person single shot. A face tracker that followed every lean looked nervous; a crop fixed on
    the median position cut off a speaker who leaned ~600 px in 2 s. So: face box every 1/TRACK_RATE s (largest face,
    gaps held, median of 3); holds = the longest runs whose boxes all fit inside the crop minus STICKY_MARGIN per side;
    one fixed x per hold (centred on its run), one MOVE_S smoothstep pan between holds, timed to finish 0.2 s after the
    face crossed. Returns (x, None) = fixed crop for the whole shot, (mean x, [[t_rel, crop_x], …]) = holds + pans,
    or None when no face is seen."""
    imgs, sc = frames_between(video, fa / fps, fb / fps, TRACK_RATE, W, H)
    raw = []  # one-person shot: the largest face is the subject; None where the head is down / turned away
    for img in imgs:
        fs = [{key: v * sc for key, v in f.items() if key != "score"} for f in faces(det, img)]
        fs = [f for f in fs if f["h"] >= 0.6 * person["h"]]
        f = max(fs, key=lambda f: f["h"]) if fs else None
        raw.append((f["x"], f["x"] + f["w"]) if f else None)
    if not any(raw):
        return None
    held, b = [], next(r for r in raw if r)  # leading gap = first detection
    for r in raw:
        b = r or b
        held.append(b)
    box = [tuple(float(np.median([h[j] for h in held[max(0, i - 1):i + 2]])) for j in (0, 1)) for i in range(len(held))]
    room = cw * (1 - 2 * STICKY_MARGIN)
    holds = []  # [k0, k1, lo, hi]: samples k0..k1 fit one crop
    for k, (lo, hi) in enumerate(box):
        if holds and max(holds[-1][3], hi) - min(holds[-1][2], lo) <= room:
            holds[-1] = [holds[-1][0], k, min(holds[-1][2], lo), max(holds[-1][3], hi)]
        else:
            holds.append([k, k, lo, hi])
    i = 1  # a quick lean and back: a short middle hold joins a neighbour if the face fits it with half the margin
    while i < len(holds) - 1:
        h = holds[i]
        j = next((j for j in (i - 1, i + 1) if (h[1] - h[0] + 1) / TRACK_RATE < MIN_HOLD_S
                  and max(holds[j][3], h[3]) - min(holds[j][2], h[2]) <= cw * (1 - STICKY_MARGIN)), None)
        if j is None:
            i += 1
            continue
        o = holds[j]
        holds[j] = [min(o[0], h[0]), max(o[1], h[1]), min(o[2], h[2]), max(o[3], h[3])]
        del holds[i]
    xs = [clamp((h[2] + h[3]) / 2 - cw / 2, 0, W - cw) for h in holds]
    if len(holds) == 1 or max(xs) - min(xs) < 4:
        return round(xs[0]), None
    keys = [[0.0, xs[0]]]
    for h, x0, x1 in zip(holds[1:], xs, xs[1:]):
        ts = max(keys[-1][0], h[0] / TRACK_RATE + 0.2 - MOVE_S)
        for q in (0, 0.2, 0.4, 0.6, 0.8, 1):
            t = round(ts + q * MOVE_S, 3)
            if t > keys[-1][0] + 0.001:
                keys.append([t, x0 + (x1 - x0) * q * q * (3 - 2 * q)])
    keys = [[round(t, 3), round(x)] for t, x in keys]
    dur = (fb - fa) / fps
    return round(float(np.mean([x for t, x in keys if t <= dur] or [keys[0][1]]))), keys


def faces(det, img):
    H, W = img.shape[:2]
    det.setInputSize((W, H))
    _, f = det.detect(img)
    out = []
    for r in (f if f is not None else []):
        x, y, w, h, score = float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[14])
        # a face cut by the left/right frame edge = over-the-shoulder foreground, not a second subject (a half face at
        # the edge turned a single of the host into a false stack); a "face" centred in the bottom 45 % = hands or mic
        # (three false stacks in one batch); heads sit higher
        if (score >= 0.6 and w >= 0.025 * W and x > 0.015 * W and x + w < 0.985 * W
                and y + h / 2 < 0.55 * H):
            out.append({"x": x, "y": y, "w": w, "h": h, "score": round(score, 3)})
    return out


def people(dets, W):
    """Cluster detections from several frames by face centre x; median box per person, sorted left -> right."""
    clusters = []
    for d in sorted(dets, key=lambda d: d["x"] + d["w"] / 2):
        cx = d["x"] + d["w"] / 2
        if clusters and abs(cx - clusters[-1]["cx"]) < 0.08 * W:
            clusters[-1]["d"].append(d)
            clusters[-1]["cx"] = np.median([e["x"] + e["w"] / 2 for e in clusters[-1]["d"]])
        else:
            clusters.append({"cx": cx, "d": [d]})
    out = []
    for c in clusters:
        med = {k: float(np.median([e[k] for e in c["d"]])) for k in ("x", "y", "w", "h")}
        med["hits"] = len(c["d"])
        out.append(med)
    return out


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def single_crop(ppl, W, H):
    cw, ch = H * OUT_W / OUT_H, H
    if ppl:
        x0 = min(p["x"] for p in ppl)
        x1 = max(p["x"] + p["w"] for p in ppl)
        cx = (x0 + x1) / 2
    else:
        cx = W / 2
    x = clamp(cx - cw / 2, 0, W - cw)
    return {"x": round(x), "y": 0, "w": round(cw), "h": round(ch)}


def lift(s, W, H, low_face):
    """Single shot whose face ends below low_face (output px) -> tighter, bottom-anchored crop so the face rises above
    the caption band. Smallest zoom that does it, capped at MAX_LIFT; centre x kept, track keys shifted to the
    narrower window. Hit ~25 of 279 shots in a podcast batch (host listening shots, a guest sitting low)."""
    c = s["crop"]
    ppl = [p for p in s["people"] if c["x"] <= p["x"] + p["w"] / 2 <= c["x"] + c["w"]]
    if not ppl:
        return
    fb, top = max(p["y"] + p["h"] for p in ppl), min(p["y"] for p in ppl)
    if (fb - c["y"]) * OUT_H / c["h"] <= low_face:
        return
    h = max((H - fb) / (1 - low_face / OUT_H), H / MAX_LIFT)
    if H / h < 1.02:  # a 1-2 % zoom changes nothing visible
        return
    y = H - h
    if top - y < 0.05 * h:  # head would leave the frame: leave it
        return
    w = h * OUT_W / OUT_H
    dx = (c["w"] - w) / 2
    x = clamp(c["x"] + dx, 0, W - w)
    if c.get("track"):
        c["track"] = [[t, round(clamp(k + dx, 0, W - w))] for t, k in c["track"]]
    c.update(x=round(x), y=round(y), w=round(w), h=round(h), lift=round(H / h, 3))


def panel_crop(p, W, H, out_h, eyes=0.40):
    """1080 x out_h window around one face: face height ~FACE_IN_PANEL of panel height, eyes `eyes` down."""
    ph = clamp(p["h"] / FACE_IN_PANEL, 0.30 * H, H)
    pw = ph * OUT_W / out_h
    if pw > W:
        pw, ph = W, W * out_h / OUT_W
    cx, eye_y = p["x"] + p["w"] / 2, p["y"] + 0.4 * p["h"]
    x = clamp(cx - pw / 2, 0, W - pw)
    y = clamp(eye_y - eyes * ph, 0, H - ph)
    return {"x": round(x), "y": round(y), "w": round(pw), "h": round(ph)}


def merge_jitter(shots, cw, tol=0.1):
    """False cuts inside one camera shot (flash, fast move) split it into near-identical crops; the output then jumps
    a few px for a few frames (seen as 4- and 6-frame shots). Adjacent shots of one range with the same layout and
    windows within tol x crop width merge; the longer shot's framing wins."""
    def close(a, b):
        if a["seg"] != b["seg"] or a["layout"] != b["layout"]:
            return False
        if a["layout"] == "single":
            return abs(a["crop"]["x"] - b["crop"]["x"]) <= tol * cw
        return all(abs(p["x"] - q["x"]) <= tol * cw and abs(p["y"] - q["y"]) <= tol * cw
                   for p, q in zip(a["panels"], b["panels"]))
    out = []
    for s in shots:
        if out and close(out[-1], s):
            prev = out[-1]
            keep = prev if prev["out_f"] - prev["in_f"] >= s["out_f"] - s["in_f"] else s
            out[-1] = dict(keep, in_f=prev["in_f"], out_f=s["out_f"])
        else:
            out.append(s)
    return out


def plan(b, clip_id):
    ep = b.episode_of(clip_id)
    video, fps, seam = ep["video"], ep["fps"], b.seam
    W, H = probe_size(video)
    det = cv2.FaceDetectorYN.create(yunet(b), "", (W, H), 0.6, 0.3, 50)
    cw = H * OUT_W / OUT_H
    segs = b.clip_segments(clip_id)
    shots = []
    for si, s in enumerate(segs):
        t0, t1 = s["in_f"] / fps, s["out_f"] / fps
        bounds = [t0] + [c for c in scene_cuts(video, t0, t1, fps) if t0 + MIN_SHOT_S < c < t1 - MIN_SHOT_S] + [t1]
        for a, z in zip(bounds[:-1], bounds[1:]):
            fa, fb = round(a * fps), round(z * fps)  # frame-exact shot range
            if fb - fa < 2:
                continue
            dets = []
            for q in (0.25, 0.5, 0.75):
                dets += faces(det, frame_at(video, (fa + (fb - fa) * q) / fps, W, H))
            ppl = people(dets, W)
            ppl = [p for p in ppl if p["hits"] >= 2] or ppl  # seen in >= 2 of 3 frames
            shot = {"seg": si, "in_f": fa, "out_f": fb, "people": [{k: round(v, 1) for k, v in p.items()} for p in ppl]}
            span = (max(p["x"] + p["w"] for p in ppl) - min(p["x"] for p in ppl)) if ppl else 0
            if len(ppl) >= 2 and span > 0.8 * cw:
                two = sorted(sorted(ppl, key=lambda p: -p["h"])[:2], key=lambda p: p["x"])
                if ep["main_side"] == "right":
                    two.reverse()  # main speaker first = top panel
                shot.update(layout="stack", seam=seam,
                            panels=[panel_crop(two[0], W, H, seam, 0.40), panel_crop(two[1], W, H, OUT_H - seam, 0.36)],
                            panel_people=[{k: round(p[k], 1) for k in ("x", "y", "w", "h")} for p in two])
            else:
                shot.update(layout="single", crop=single_crop(ppl, W, H))
                if not ppl:
                    shot["review"] = "no face found"
            shots.append(shot)
    shots = merge_jitter(shots, cw)
    for s in shots:
        if s["layout"] == "single" and len(s["people"]) == 1:
            r = track(video, s["in_f"], s["out_f"], fps, W, H, det, s["people"][0], cw)
            if r:
                s["crop"]["x"], keys = r
                if keys:
                    s["crop"]["track"] = keys
        if s["layout"] == "single":
            lift(s, W, H, b.low_face)
    out = {"clip": clip_id, "video": str(video), "fps": fps, "src_w": W, "src_h": H, "out": [OUT_W, OUT_H],
           "segments": segs, "shots": shots}
    d = b.clip_dir(clip_id)
    b.write_cuts(clip_id, segs)
    new = json.dumps(out, indent=1, ensure_ascii=False)
    if (d / "plan.json").exists() and (d / "plan.json").read_text(encoding="utf-8") == new:
        print(f"{clip_id}: plan unchanged ({len(shots)} shots)")  # keep mtime -> master/renders stay fresh
        return
    (d / "plan.json").write_text(new, encoding="utf-8")
    sheet(out, d / "plan_sheet.jpg")
    for s in shots:
        print(f"  {s['in_f'] / fps:8.2f}-{s['out_f'] / fps:8.2f}s  {s['layout']:6s} people {len(s['people'])}"
              + (f"  REVIEW: {s['review']}" if s.get("review") else ""))
    print(f"{clip_id}: {len(shots)} shots -> {d / 'plan.json'}")


def render_shot(img, s):
    if s["layout"] == "single":
        c = s["crop"]
        return cv2.resize(img[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]], (OUT_W, OUT_H), interpolation=cv2.INTER_LANCZOS4)
    seam = s.get("seam", OUT_H // 2)
    tiles = [cv2.resize(img[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]], (OUT_W, ph),
                        interpolation=cv2.INTER_LANCZOS4) for c, ph in zip(s["panels"], (seam, OUT_H - seam))]
    return np.vstack(tiles)


def sheet(p, path):
    W, H = p["src_w"], p["src_h"]
    tiles = []
    for s in p["shots"]:
        img = frame_at(p["video"], (s["in_f"] + s["out_f"]) / 2 / p["fps"], W, H)
        src = img.copy()
        for q in s["people"]:
            cv2.rectangle(src, (int(q["x"]), int(q["y"])), (int(q["x"] + q["w"]), int(q["y"] + q["h"])), (0, 255, 0), 3)
        for c in ([s["crop"]] if s["layout"] == "single" else s["panels"]):
            cv2.rectangle(src, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), (0, 200, 255), 3)
        out = render_shot(img, s)
        th = 480
        a = cv2.resize(src, (round(W * th / H), th))
        b = cv2.resize(out, (round(OUT_W * th / OUT_H), th))
        tile = np.hstack([a, np.zeros((th, 8, 3), np.uint8), b])
        cv2.putText(tile, f"{s['in_f'] / p['fps']:.1f}s {s['layout']}", (10, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        tiles.append(tile)
    wmax = max(t.shape[1] for t in tiles)
    tiles = [np.hstack([t, np.zeros((t.shape[0], wmax - t.shape[1], 3), np.uint8)]) for t in tiles]
    cols = 2
    rows = [np.hstack(tiles[i:i + cols] + [np.zeros_like(tiles[0])] * (cols - len(tiles[i:i + cols])))
            for i in range(0, len(tiles), cols)]
    cv2.imwrite(str(path), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 82])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("ids", nargs="+", help="clip ids")
    a = ap.parse_args()
    batch = Batch(a.config)
    for cid in a.ids:
        plan(batch, cid)
