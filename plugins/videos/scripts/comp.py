# /// script
# requires-python = ">=3.10"
# ///
"""Layout comp: a talking-head video laid out in HyperFrames from ONE all-angles master (the "mega-file": every camera
of the approved cut side by side in one big frame, same cut on every feed). 16:9 first; the 9:16 comes from the same
spans (same cut, same layout per span) in the vertical geometry.

  uv run comp.py plan   --spec comp.json [--fmt 16x9|9x16]   framing per span -> <out>/<fmt>/plan.json + a table
  uv run comp.py enc    --spec comp.json [--fmt ...]         one ffmpeg crop of the master per panel (cached, shared
                                                             between formats), overlay clips
  uv run comp.py audio  --spec comp.json [--fmt ...]         the mastered sound into the project
  uv run comp.py html   --spec comp.json [--fmt ...]         index.html; refuses to overwrite Studio or hand edits
                                                             (--force once you kept them)
  uv run comp.py all    --spec comp.json [--fmt ...]         plan + enc + audio + html
  uv run comp.py verify --spec comp.json [--fmt ...]         every track: frame count = span, frame 0 = master at f0
  uv run comp.py gaps   <render.mp4> [--fps 24000/1001]      frames where a panel shows its empty background

Spec, kinds, geometry and the rules behind them: references/comp.md. Stdlib only (ffmpeg on PATH).
"""
import argparse
import hashlib
import html as H_
import json
import os
import re
import shutil
import statistics as st
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

# Span starts land EPS frames before their frame: a start rounded UP past the frame time left that frame on an empty
# panel at ~20 cuts of a 2:46 video. Track frame k then plays at comp frame f0 + k.
EPS = 0.25
PANEL_BG = "#111"

# 16:9 canvas. Panels sit in a 1600 px wide column (160-1760) above the caption band 872-1080.
G16 = dict(
    W=1920, H=1080, rad=26, lens=320, full=(0, 0, 1920, 1080),
    win_slot=(220, 40, 1480, 832),            # screen window slot; the window = the crop's aspect, centred in it
    bub=(1730, 830, 300),                     # face bubble: centre x, y, diameter
    side2=[(160, 40, 792, 832), (968, 40, 792, 832)],
    three16=[(160, 40, 1180, 832), (1356, 40, 404, 408), (1356, 464, 404, 408)],
    quad=[(160, 40, 792, 408), (968, 40, 792, 408), (160, 464, 792, 408), (968, 464, 792, 408)],
    duo_a=(40, 40, 1420, 833), duo_b_x=1476, duo_b_w=404,   # big panel A (screen / reel) + narrow camera panel B
    frac={"side2": 0.30, "big": 0.28, "small": 0.36, "quad": 0.36, "bub": 0.55, "hero_b": 0.34},
    band=(872, 1080), cap=(62, 1160, 26),     # caption band; size px, line box max width, max chars per chunk
)
# 9:16 canvas: one column x 44-1036, margin 44, radius 20. Platform UI covers the top and bottom: content and eyes stay
# in y 220-1436. Each layout keeps its own clear caption band.
G9 = dict(
    W=1080, H=1920, rad=20, lens=210, safe=(220, 1436),
    card=(44, 44, 992, 1832), win=(44, 44, 992, 558), low=(44, 800, 992, 1076),
    stack2=[(44, 44, 992, 676), (44, 912, 992, 964)],
    stack3w=[(44, 44, 992, 558), (44, 762, 992, 558), (44, 1338, 992, 538)],
    quad=[(44, 44, 487, 820), (549, 44, 487, 820), (44, 1056, 487, 820), (549, 1056, 487, 820)],
    band={"full": (1266, 1430), "screen": (602, 800), "duo": (602, 800), "hero": (602, 800), "side2": (736, 896),
          "three16": (602, 762), "quad": (864, 1056)},
    frac={"low": 0.40, "stack2": (0.45, 0.36), "stack3w": 0.34, "quad": 0.30},
    card_punch_max=1.25,                      # the vertical card is already a close-up: a bigger punch cuts the hair
    cap=(64, 760, 24),
)
KINDS = ("full", "side2", "three16", "quad", "screen", "duo", "hero")


# ---------------------------------------------------------------- spec
def path(p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(BASE, p))


def per_fmt(v):
    """a spec value given once or per format ({"16x9": ..., "9x16": ...})."""
    return v.get(FMT) if isinstance(v, dict) and FMT in v else v


def load(spec, fmt):
    global S, BASE, FMT, V9, G, FPS, FPS_S, OUT, ROOT, MASTER, CELL, CAMS, FACES, MIN_F, END_F, SHADOW, SCREEN, BUB_CAM
    BASE = os.path.dirname(os.path.abspath(spec))
    S = json.load(open(spec, encoding="utf-8"))
    FMT, V9 = fmt, fmt == "9x16"
    G = G9 if V9 else G16
    FPS_S = str(S.get("fps", "24000/1001"))
    n, _, d = FPS_S.partition("/")
    FPS = float(n) / float(d or 1)
    OUT = path(S.get("out", "comp"))
    ROOT = OUT.replace("\\", "/") + "/" + FMT
    MASTER = path(S["master"])
    CELL = {k: tuple(v) for k, v in S["cells"].items()}
    CAMS = {k: dict(S.get("cams", {}).get(k, {})) for k in CELL}
    for c in CAMS.values():
        c.setdefault("role", "front")
        assert c["role"] in ("front", "side", "top"), c
    FACES = json.load(open(path(S["faces"]), encoding="utf-8")) if S.get("faces") else {}
    MIN_F = round(float(S.get("min_shot_s", 1.5)) * FPS)
    END_F = int(S["end_f"])
    SHADOW = S.get("style", {}).get("shadow", "0 18px 50px rgba(0,0,0,.45)")
    SCREEN = S.get("screen") or {}
    BUB_CAM = S.get("bubble_cam") or next(k for k, c in CAMS.items() if c["role"] == "front" and not c.get("faces_from"))


def role(cam):
    return CAMS[cam]["role"]


def ff(cam):
    """the camera whose face measurements frame this one (a program feed that mirrors the front camera)."""
    return CAMS[cam].get("faces_from", cam)


# ---------------------------------------------------------------- framing
def stats(cam, f0=None, f1=None):
    """median face numbers of a camera over [f0, f1) (frames), else the whole video."""
    c = CAMS[cam]
    rows = [r for r in FACES.get(cam, []) if r[6] > c.get("min_face_h", 0)]
    if f0 is not None:
        sub = [r for r in rows if f0 / FPS <= r[0] < f1 / FPS]
        if len(sub) >= 2:
            rows = sub
        elif c.get("faces_from"):
            return None
    if not rows:
        return None
    m = lambda i: st.median(r[i] for r in rows)  # noqa: E731
    return dict(nx=m(1), ny=m(2), ex=m(3), ey=m(4), fw=m(5), fh=m(6), yaw=m(7), n=len(rows))


def anchor(cam, s):
    """(anchor point in cell px, target fraction in the window). Front: nose on the upper third, centred. Side: the head
    on the third behind it, looking room in front. Top (hands, desk): a fixed point of the cell."""
    r = role(cam)
    cw, ch = CELL[cam][2:]
    if r == "top":
        da = (s or {}).get("da") or CAMS[cam].get("anchor") or (0.5, 0.5)
        return (da[0] * cw, da[1] * ch), (0.5, 0.5)
    assert s, f"no face measurements for camera {cam}: run comp_faces.py first"
    if r == "front":
        return (s["nx"], s["ny"]), (0.5, 1 / 3)
    back = 0.25 * s["fw"]
    if s["yaw"] < 0:   # nose left of the eyes = faces frame-left -> head on the right third, room on the left
        return (s["ex"] + back, s["ny"]), (2 / 3, 1 / 3)
    return (s["ex"] - back, s["ny"]), (1 / 3, 1 / 3)


def tgt9(cam, rect, s, side_x=(0.38, 0.62), frac=None):
    """9:16 anchor target in a panel: nose y from the panel's part inside the safe zone (eyes on its upper third), side
    cameras on the third behind the head (the narrow single card: 0.40 / 0.60, the head would hit the edge on a true
    third); frac = face height / panel height -> headroom so the hair clears the panel top."""
    y0, y1 = max(rect[1], G9["safe"][0]), min(rect[1] + rect[3], G9["safe"][1])
    ty = 1 / 3 if y1 - y0 < 200 else (y0 + (y1 - y0) / 3 + 40 - rect[1]) / rect[3]
    if frac:
        ty = max(ty, 0.05 + 0.8 * frac)
    r = role(cam)
    if r != "side" or not s:
        return (0.5, ty) if r != "top" else (0.5, 0.5)
    return (side_x[1], ty) if s["yaw"] < 0 else (side_x[0], ty)


def place(cam, ww, wh, a, tgt):
    cw, ch = CELL[cam][2:]
    x = min(max(a[0] - tgt[0] * ww, 0), cw - ww)
    y = min(max(a[1] - tgt[1] * wh, 0), ch - wh)
    return [x, y, ww, wh]


def frame(cam, aspect, mode, s, punch=1.0, frac=None, tgt=None):
    """cell window [x, y, w, h] (cell px) for a panel of this aspect. mode full = the whole canvas, panel = a framed
    panel (face = frac of its height), card9 = the 9:16 single card (full cell height / punch)."""
    cw, ch = CELL[cam][2:]
    a, tgt0 = anchor(cam, s)
    top = role(cam) == "top"
    if mode == "card9":
        wh = ch / punch
        ww = wh * aspect
        if ww > cw:
            ww, wh = cw, cw / aspect
        return place(cam, ww, wh, a, tgt)
    tgt = tgt or tgt0
    if mode == "full":
        if top:
            ww = cw / 1.25
            win = place(cam, ww, ww / aspect, a, tgt)
        else:
            best, z = None, 1.0
            while z <= 1.7 + 1e-9:     # smallest zoom that puts the anchor on its third
                ww = cw / z
                wh = ww / aspect
                if wh > ch:
                    wh, ww = ch, ch * aspect
                win = place(cam, ww, wh, a, tgt)
                err = max(abs((a[0] - win[0]) / ww - tgt[0]), 0.5 * abs((a[1] - win[1]) / wh - tgt[1]))
                if best is None or err < best[0] - 1e-6:
                    best = (err, win)
                if err <= 0.02:
                    break
                z = round(z + 0.01, 2)
            win = best[1]
        if punch != 1.0:
            win = place(cam, win[2] / punch, win[3] / punch, a, tgt)
        return win
    wh = ch if top else min(s["fh"] / frac, ch)
    ww = wh * aspect
    if ww > cw:
        ww, wh = cw, cw / aspect
    win = place(cam, ww, wh, a, tgt)
    if not top:   # the cell edge stops the head short of its third -> tighten (face up to 60 % of the panel)
        while abs((a[0] - win[0]) / win[2] - tgt[0]) > 0.03 and win[3] * 0.97 > s["fh"] / 0.6:
            win = place(cam, win[2] * 0.97, win[3] * 0.97, a, tgt)
    return win


def contain(cam, win, aspect, f0, f1, pad=1.25, ty=1 / 3):
    """shift, then widen if needed, a panel window so every face sample of the span stays inside (head = eye centre
    +- pad * face width / 2). Returns (window, note)."""
    c = ff(cam)
    rows = [r for r in FACES.get(c, []) if f0 / FPS <= r[0] < f1 / FPS]
    if not rows:
        return win, ""
    hx0 = min(r[3] - pad * r[5] / 2 for r in rows)
    hx1 = max(r[3] + pad * r[5] / 2 for r in rows)
    x, y, w, h = win
    if hx0 >= x and hx1 <= x + w:
        return win, ""
    cw, ch = CELL[c][2:]
    ww = min(max(w, hx1 - hx0), ch * aspect, cw)
    wh = ww / aspect
    ny = st.median(r[2] for r in rows)
    nx = min(max((hx0 + hx1) / 2 - ww / 2, 0), cw - ww)
    ny0 = min(max(ny - wh * ty, 0), ch - wh)
    return [nx, ny0, ww, wh], ("contain" if ww == w else f"contain z-{w / ww:.2f}")


def fit(slot, aspect):
    """largest rect of this aspect centred in slot."""
    x, y, w, h = slot
    if w / h > aspect:
        nw = h * aspect
        return [round(x + (w - nw) / 2), y, round(nw), h]
    nh = w / aspect
    return [x, round(y + (h - nh) / 2), w, round(nh)]


def ev(x):
    return int(round(x / 2)) * 2


def blurs_for(src, f0, f1):
    """screen blur rects (screen-cell px + frame ranges) that meet this crop during the span, in track px + track frames."""
    out = []
    for b in SCREEN.get("blur", []):
        bx, by, bw, bh = b["rect"]
        x0, y0 = max(bx, src[0]), max(by, src[1])
        x1, y1 = min(bx + bw, src[0] + src[2]), min(by + bh, src[1] + src[3])
        if x1 <= x0 or y1 <= y0:
            continue
        for a, e in b.get("frames") or [[0, END_F]]:
            if a < f1 and e > f0:
                out.append([round(x0 - src[0]), round(y0 - src[1]), round(x1 - x0), round(y1 - y0), max(a, f0) - f0, min(e, f1) - f0])
    return out or None


def screen_crop(sp):
    if sp.get("crop"):
        return tuple(sp["crop"])
    return tuple(SCREEN["crops"][sp.get("src", "app")])


# ---------------------------------------------------------------- plan
def spans():
    sp = sorted(S["spans"], key=lambda x: x["f"])
    for x in sp:
        assert x["k"] in KINDS, f"span at frame {x['f']}: kind {x['k']} not in {KINDS}"
    return sp


def runs_of(out, kind):
    runs = []
    for s in out:
        if s["kind"] == kind:
            if runs and runs[-1][1] == s["f0"]:
                runs[-1][1] = s["f1"]
            else:
                runs.append([s["f0"], s["f1"]])
    return runs


def plan():
    SP = spans()
    fr = [x["f"] for x in SP] + [END_F]
    hero_runs, cur = [], None
    for i, x in enumerate(SP):
        if x["k"] == "hero":
            if cur and cur[1] == x["f"]:
                cur[1] = fr[i + 1]
            else:
                cur = [x["f"], fr[i + 1]]
                hero_runs.append(cur)
    sticky, out, bad = {}, [], []
    scr = SCREEN.get("cam")
    for i, sp in enumerate(SP):
        f0, f1 = fr[i], fr[i + 1]
        note = sp.get("note", "")
        if f1 - f0 < MIN_F:
            bad.append(f"span {i} at {f0} ({note}): {f1 - f0} frames < {MIN_F}")
        k = sp["k"]
        panels = []
        HR = next((r for r in hero_runs if r[0] <= f0 < r[1]), None)

        def cam_panel(cam, rect, mode, frac=None, punch=1.0, push=None):
            assert cam in CELL, f"span {i}: camera {cam} not in cells"
            aspect = rect[2] / rect[3]
            top = role(cam) == "top"
            tg = None
            if k == "hero" and not top:   # sticky over the reel run only: narrow panel, the speaker moves more there
                sg = stats(ff(cam), *HR)
            else:
                sg = stats(ff(cam))
            sl = stats(cam, f0, f1) if not top else None
            if top and sp.get("da"):
                sg = dict(sg or {}, da=sp["da"])
            side_x = (0.40, 0.60) if mode == "card9" else (0.38, 0.62)
            if V9:
                tg = tgt9(cam, rect, sg, side_x, frac)
            key = (ff(cam), round(aspect, 3), mode, frac, punch, tuple(sp["da"]) if sp.get("da") else None, tg)
            if key not in sticky:
                sticky[key] = frame(cam, aspect, mode, sg, punch, frac, tg)
            win, note_ = sticky[key], ""
            if sl:   # moved? a new framing only when the span's anchor is far from the sticky one
                a_g, _ = anchor(cam, sg)
                a_l, _ = anchor(cam, sl)
                if abs(a_l[0] - a_g[0]) > 0.10 * win[2] or abs(a_l[1] - a_g[1]) > 0.08 * win[3]:
                    win = frame(cam, aspect, mode, sl, punch, frac, tgt9(cam, rect, sl, side_x, frac) if V9 else None)
                    note_ = "reframed"
            if (k == "hero" or V9) and not top:   # narrow panels: the head stays inside for the whole span
                win, c_note = contain(cam, win, aspect, f0, f1, ty=tg[1] if tg else 1 / 3)
                note_ = c_note or note_
            a_s, _ = anchor(cam, sl or sg)
            panels.append(dict(cam=cam, rect=list(rect), win=[round(v, 1) for v in win], z=round(CELL[cam][2] / win[2], 2),
                               origin=[round((a_s[0] - win[0]) / win[2], 3), round((a_s[1] - win[1]) / win[3], 3)],
                               push=push, note=note_, round=mode != "full"))

        def app_panel(slot):
            src = screen_crop(sp)
            rect = fit(slot, src[2] / src[3])
            panels.append(dict(cam=scr, rect=rect, win=list(src), app=True, round=True, views=sp.get("views", []),
                               lens=sp.get("lens", []), blur=blurs_for(src, f0, f1)))
            return rect

        if V9:
            if k == "full":
                cam_panel(sp["cam"], G9["card"], "card9", punch=min(sp.get("punch", 1.0), G9["card_punch_max"]), push=sp.get("push"))
            elif k in ("side2", "three16", "quad"):
                rects, fk = {"side2": (G9["stack2"], "stack2"), "three16": (G9["stack3w"], "stack3w"), "quad": (G9["quad"], "quad")}[k]
                for j, cam in enumerate(sp["cams"]):
                    fr_ = G9["frac"][fk]
                    cam_panel(cam, rects[j], "panel", frac=fr_[j] if isinstance(fr_, tuple) else fr_)
            else:   # screen / duo / hero: 16:9 window on top, a camera panel under it
                if k in ("screen", "duo"):
                    app_panel(G9["win"])
                cam_panel(sp.get("cam", BUB_CAM), G9["low"], "panel", frac=G9["frac"]["low"])
            out.append(dict(i=i, f0=f0, f1=f1, kind=k, note=note, panels=panels, band=list(G9["band"][k])))
            continue
        if k == "full":
            cam_panel(sp["cam"], G16["full"], "full", punch=sp.get("punch", 1.0), push=sp.get("push"))
        elif k in ("side2", "three16", "quad"):
            rects = G16[k]
            for j, cam in enumerate(sp["cams"]):
                fk = {"side2": "side2", "quad": "quad"}.get(k) or ("big" if j == 0 else "small")
                cam_panel(cam, rects[j], "panel", frac=G16["frac"][fk])
        elif k == "hero":
            hr = fit(G16["duo_a"], 16 / 9)
            cam_panel(sp["cam"], [G16["duo_b_x"], hr[1], G16["duo_b_w"], hr[3]], "panel", frac=G16["frac"]["hero_b"])
        elif k in ("screen", "duo"):
            rect = app_panel(G16["win_slot"] if k == "screen" else G16["duo_a"])
            if k == "screen":
                s = stats(BUB_CAM, f0, f1) or stats(BUB_CAM)
                side = s["fh"] / G16["frac"]["bub"]
                win = place(BUB_CAM, side, side, (s["nx"], s["ny"]), (0.5, 0.42))
                bx, by, bd = G16["bub"]
                panels.append(dict(cam=BUB_CAM, rect=[bx - bd // 2, by - bd // 2, bd, bd], win=[round(v, 1) for v in win], bubble=True))
            else:
                cam = sp.get("cam", BUB_CAM)
                cam_panel(cam, [G16["duo_b_x"], rect[1], G16["duo_b_w"], rect[3]], "panel",
                          frac=None if role(cam) == "top" else G16["frac"]["hero_b"])
        out.append(dict(i=i, f0=f0, f1=f1, kind=k, note=note, panels=panels))
    os.makedirs(ROOT, exist_ok=True)
    hero_rect = list(G9["win"]) if V9 else fit(G16["duo_a"], 16 / 9)
    P = dict(fmt=FMT, fps=FPS_S, end_f=END_F, w=G["W"], h=G["H"], spans=out,
             hero=dict(runs=hero_runs, rect=hero_rect) if hero_runs else None)
    json.dump(P, open(ROOT + "/plan.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for s in out:
        print(f"{s['f0']:5d} {s['f0'] / FPS:7.2f}-{s['f1'] / FPS:7.2f} {s['kind']:7s} " +
              " ".join(f"{p['cam']}[z{p.get('z', '-')}]" + (f"!{p['note']}" if p.get("note") else "") for p in s["panels"])
              + f"  {s['note']}")
    print("SHORT:", bad or "none")
    print("plan ->", ROOT + "/plan.json")


# ---------------------------------------------------------------- encode
def materialize(sig, out, build):
    """out = a cached file for this signature (hard link, else copy); build(tmp) makes it once. Both formats share the
    cache, so an identical track (screen crops, the sound) is encoded once."""
    if os.path.exists(out) and os.path.exists(out + ".ok") and open(out + ".ok").read() == sig:
        return "cached"
    ext = os.path.splitext(out)[1]
    cache = os.path.join(OUT, ".cache", hashlib.sha1(sig.encode()).hexdigest()[:20] + ext)
    state = "shared"
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache[: -len(ext)] + ".part" + ext
        err = build(tmp)
        if err:
            return "FAIL " + err
        os.replace(tmp, cache)
        state = "ok"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    try:
        os.link(cache, out)
    except OSError:
        shutil.copy2(cache, out)
    open(out + ".ok", "w").write(sig)
    return state


def ffmpeg(args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    return r.stderr[-400:] if r.returncode else None


X264 = ["-an", "-c:v", "libx264", "-preset", "fast", "-crf", "15", "-g", "24", "-pix_fmt", "yuv420p"]


def enc_job(s, j, p):
    cam = p["cam"]
    cx, cy = CELL[cam][:2]
    x, y, w, h = p["win"]
    X, Y, Wc, Hc = ev(cx + x), ev(cy + y), ev(w), ev(h)
    if p.get("app"):
        ow, oh = Wc, Hc                 # native size: zooms up to ~2.6x stay 1:1 (a QR stays sharp)
    elif p.get("bubble"):
        ow, oh = 600, 600
    else:
        k = p["push"][1] if p.get("push") else 1.0
        ow, oh = ev(p["rect"][2] * k), ev(p["rect"][3] * k)
    out = f"{ROOT}/assets/s{s['i']:02d}_{j}.mp4"
    n = s["f1"] - s["f0"]
    blur = p.get("blur")
    sig = f"track|{MASTER}|{os.path.getmtime(MASTER):.0f}|{X},{Y},{Wc},{Hc},{ow},{oh},{s['f0']},{n},{FPS_S},{blur}"
    vf = f"crop={Wc}:{Hc}:{X}:{Y}" + ("" if (ow, oh) == (Wc, Hc) else f",scale={ow}:{oh}:flags=lanczos") + ",setsar=1"
    for bi, (bx, by, bw, bh, n0, n1) in enumerate(blur or []):   # app tracks are native size: rects are track px
        vf += (f",split[a{bi}][b{bi}];[b{bi}]crop={bw}:{bh}:{bx}:{by},boxblur=luma_radius=12:luma_power=3:chroma_radius=6:"
               f"chroma_power=3[bb{bi}];[a{bi}][bb{bi}]overlay={bx}:{by}:enable='between(n,{n0},{n1 - 1})'")
    return out, materialize(sig, out, lambda tmp: ffmpeg(
        ["-ss", f"{(s['f0'] + 0.25) / FPS:.6f}", "-i", MASTER, "-frames:v", str(n), "-vf", vf, *X264, "-r", FPS_S, tmp]))


def overlays(P):
    """spec overlays laid out for this format: id, kind, f0, f1, rect (+ kind fields). No f0/f1 on a reel = every hero run."""
    out = []
    for ov in S.get("overlays", []):
        slot = ov.get("slot", "hero")
        if slot == "hero":
            assert P.get("hero"), f"overlay {ov['id']}: slot hero but the plan has no hero span"
            rect, runs = P["hero"]["rect"], P["hero"]["runs"]
        else:
            rect, runs = per_fmt(ov["rect"]), []
        ranges = [(ov["f0"], ov["f1"])] if "f0" in ov else [tuple(r) for r in runs]
        assert ranges, f"overlay {ov['id']}: no f0/f1 and no hero run"
        for k, (f0, f1) in enumerate(ranges):
            oid = ov["id"] if len(ranges) == 1 else f"{ov['id']}-r{k}"
            out.append(dict(ov, id=oid, f0=f0, f1=f1, rect=list(rect)))
    return out


def enc_overlay(ov):
    if ov["kind"] not in ("video", "master"):
        return None
    out = f"{ROOT}/assets/ov_{ov['id']}.mp4"
    n = ov["f1"] - ov["f0"]
    w, h = ev(ov["rect"][2]), ev(ov["rect"][3])
    if ov["kind"] == "video":   # a downloaded reel or a generated clip, looped to the range
        src = path(ov["src"])
        sig = f"video|{src}|{os.path.getmtime(src):.0f}|{n},{w},{h},{FPS_S}"
        return out, materialize(sig, out, lambda tmp: ffmpeg(
            ["-stream_loop", "-1", "-i", src, "-vf", f"fps={FPS_S},scale={w}:{h}:flags=lanczos,setsar=1", "-frames:v", str(n),
             *X264, tmp]))
    cam = ov.get("cam") or SCREEN.get("cam")   # master: a crop of one cell, frozen from `hold` on (a page loading, a bar)
    x, y, cw, chh = ov["crop"]
    hold = ov.get("hold", ov["f1"])
    n_live = hold - ov["f0"]
    sig = f"master|{MASTER}|{os.path.getmtime(MASTER):.0f}|{cam},{ov['crop']},{ov['f0']},{n},{hold},{w},{h},{FPS_S}"
    vf = f"crop={cw}:{chh}:{CELL[cam][0] + x}:{CELL[cam][1] + y},scale={w}:{h}:flags=lanczos,setsar=1"
    if n > n_live:
        vf += f",tpad=stop_mode=clone:stop={n - n_live}"
    return out, materialize(sig, out, lambda tmp: ffmpeg(
        ["-ss", f"{(ov['f0'] + 0.25) / FPS:.6f}", "-i", MASTER, "-frames:v", str(n_live), "-vf", vf, *X264, "-r", FPS_S, tmp]))


def enc():
    P = json.load(open(ROOT + "/plan.json", encoding="utf-8"))
    jobs = [(s, j, p) for s in P["spans"] for j, p in enumerate(s["panels"])]
    fails = 0
    with ThreadPoolExecutor(int(S.get("enc_workers", 4))) as ex:
        res = list(ex.map(lambda a: enc_job(*a), jobs)) + [r for r in map(enc_overlay, overlays(P)) if r]
    for out, stt in res:
        if stt not in ("cached", "shared"):
            print(os.path.basename(out), stt, flush=True)
        fails += stt.startswith("FAIL")
    print(len(jobs), "tracks,", len(res) - len(jobs), "overlay clips,", sum(s == "shared" for _, s in res), "shared,", fails, "failed")
    if fails:
        sys.exit(1)


# ---------------------------------------------------------------- audio
def audio_out():
    a = S["audio"]
    return f"{ROOT}/assets/audio/{os.path.splitext(os.path.basename(a['src']))[0]}.m4a"


def audio():
    """the mastered sound in the comp itself, so Studio plays it and the render needs no mux. `af` = an optional ffmpeg
    filter (gain + limiter when the master is not mastered yet)."""
    a = S["audio"]
    src, af, out = path(a["src"]), a.get("af"), audio_out()
    sig = f"audio|{src}|{os.path.getmtime(src):.0f}|{af}"
    stt = materialize(sig, out, lambda tmp: ffmpeg(["-i", src, "-vn", *(["-af", af] if af else []), "-c:a", "aac", "-b:a", "320k", tmp]))
    print("audio", out, stt)
    if stt.startswith("FAIL"):
        sys.exit(1)


# ---------------------------------------------------------------- captions
def cap_words():
    """words on the cut's timeline: a Words file ({"words": [...]}, record seconds) or a plain list; fillers out, a lone
    ? ! glued to the word before (French spacing when lang fr)."""
    W_ = json.load(open(path(S["words"]), encoding="utf-8"))
    W_ = W_["words"] if isinstance(W_, dict) else W_
    drop = re.compile(S.get("captions", {}).get("drop", r"(?i)(euh+|heu+|um+|uh+|erm)[,.]?"))
    fr = S.get("lang", "").startswith("fr")
    ws = []
    for w in W_:
        if w.get("type", "word") != "word":
            continue
        x = {"text": w["text"], "start": w["start"], "end": w["end"]}
        if not re.search(r"\w", x["text"]) and ws:
            ws[-1]["text"] += (" " if fr else "") + x["text"]
            continue
        if drop.fullmatch(x["text"]):
            continue
        ws.append(x)
    return ws


def cap_groups(ws, max_chars):
    """chunks never cross a sentence; break after . ! ? always, after , ; : once the chunk has 2+ words, at pauses
    > 0.35 s, and before max_chars; a lone word joins its neighbour."""
    out, cur = [], []
    for w in ws:
        if cur:
            prev = cur[-1]
            n = len(" ".join(x["text"] for x in cur))
            if (n + 1 + len(w["text"]) > max_chars or re.search(r"[.!?…]$", prev["text"])
                    or (re.search(r"[,;:]$", prev["text"]) and (len(cur) >= 2 or len(prev["text"]) > 4))
                    or w["start"] - prev["end"] > 0.35):
                out.append(cur)
                cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    txt = lambda c: " ".join(x["text"] for x in c)  # noqa: E731
    i = 0
    while i < len(out):
        g = out[i]
        if len(g) == 1 and len(out) > 1:
            if i > 0 and not re.search(r"[.!?…,;:]$", out[i - 1][-1]["text"]) and len(txt(out[i - 1] + g)) <= max_chars + 5:
                out[i - 1] += g
                del out[i]
                continue
            if i + 1 < len(out) and not re.search(r"[.!?…]$", g[0]["text"]) and len(txt(g + out[i + 1])) <= max_chars + 5:
                out[i + 1] = g + out[i + 1]
                del out[i]
                continue
        i += 1
    return out


def captions(P, D):
    """(html parts, js lines, css, count): one div per chunk, shown / hidden hard, the spoken word in the accent colour.
    9:16: each chunk sits in its layout's clear band, never straddles a band change and ends on it."""
    C = S.get("captions", {})
    size, max_w, max_chars = C.get("size", G["cap"][0]), C.get("max_width", G["cap"][1]), C.get("max_chars", G["cap"][2])
    accent = C.get("accent", "#4CAF50")
    font_css, family = "", C.get("family", "sans-serif")
    if C.get("font"):
        f = path(C["font"])
        os.makedirs(ROOT + "/assets/fonts", exist_ok=True)
        shutil.copy2(f, ROOT + "/assets/fonts/" + os.path.basename(f))
        fmt_ = {".woff2": " format('woff2')", ".woff": " format('woff')", ".ttf": " format('truetype')", ".otf": " format('opentype')"}.get(
            os.path.splitext(f)[1].lower(), "")
        font_css = (f"@font-face {{ font-family: '{family}'; src: url('assets/fonts/{os.path.basename(f)}'){fmt_}; "
                    f"font-weight: {C.get('weight', 700)}; font-display: block; }}")
    words = cap_words()
    bands = None
    if V9:
        bands = [((s["f0"] - EPS) / FPS, tuple(s["band"])) for s in P["spans"]]
        cuts = [t for (t, b), (_, b0) in zip(bands[1:], bands[:-1]) if b != b0]
        groups = []
        for a, b in zip([0.0] + cuts, cuts + [1e9]):
            seg = [w for w in words if a <= w["start"] < b]
            groups += [(g, a, b) for g in (cap_groups(seg, max_chars) if seg else [])]
    else:
        groups = [(g, 0.0, 1e9) for g in cap_groups(words, max_chars)]

    def band_at(t):
        return [b for t0, b in bands if t0 <= t + 1e-6][-1] if bands else G16["band"]
    parts, js, dump = [], [], []
    for gi, (gr, seg_a, seg_b) in enumerate(groups):
        gs = round(max(0.0, gr[0]["start"] - 0.05), 3)
        nxt = groups[gi + 1][0][0]["start"] - 0.05 if gi + 1 < len(groups) else D
        gs = round(max(gs, seg_a), 3)
        ge = round(min(max(gr[-1]["end"] + 0.25, gs + 0.4), nxt, D, seg_b), 3)
        if ge - gs < 0.2:
            continue
        gid = f"cap{gi:03d}"
        sp_ = []
        for wi, w in enumerate(gr):
            wid = f"{gid}w{wi}"
            t = re.sub(r"[,.;:]+$", "", w["text"])            # keep ? and !, drop trailing , . ; :
            sp_.append(f'<span class="w" id="{wid}">{H_.escape(t, quote=False)}</span>')
            on = round(max(gs, w["start"]), 3)
            off = round(max(on + 0.06, gr[wi + 1]["start"] if wi + 1 < len(gr) else ge), 3)
            js.append(f'tl.set("#{wid}", {{color: "{accent}"}}, {on});')
            if off < ge:
                js.append(f'tl.set("#{wid}", {{color: "#FFFFFF"}}, {off});')
        b0, b1 = band_at(gr[0]["start"])
        parts.append(f'<div id="{gid}" class="cap" style="top:{b0}px;height:{b1 - b0}px"><div class="cap-line">{" ".join(sp_)}</div></div>')
        js.append(f'tl.set("#{gid}", {{autoAlpha: 1}}, {gs});')
        js.append(f'tl.set("#{gid}", {{autoAlpha: 0}}, {ge});')
        dump.append([gs, ge, " ".join(w["text"] for w in gr)])
    json.dump(dump, open(ROOT + "/captions.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    shadow = C.get("shadow", "0 0 2px rgba(0,0,0,1), 0 2px 4px rgba(0,0,0,.9), 0 0 16px rgba(0,0,0,.72)")
    css = f"""{font_css}
.cap {{ position: absolute; left: 0; right: 0; display: flex; align-items: center; justify-content: center; opacity: 0; visibility: hidden; }}
.cap-line {{ max-width: {max_w}px; padding: 0 {0 if V9 else 60}px; text-align: center; text-wrap: balance; font-family: '{family}', sans-serif;
  font-weight: {C.get('weight', 700)}; font-synthesis: none; font-size: {size}px; line-height: 1.08; letter-spacing: 0.01em; color: #FFFFFF;
  text-shadow: {shadow}; }}
.cap-line .w {{ display: inline-block; }}
"""
    return parts, js, css, len(dump)


# ---------------------------------------------------------------- html
def view_xy(z, cx, cy, ww, wh):
    """content transform (x, y, scale) that centres box point (cx, cy) (fractions) in a ww x wh box, clamped."""
    x = min(0, max(ww - z * ww, ww / 2 - z * cx * ww))
    y = min(0, max(wh - z * wh, wh / 2 - z * cy * wh))
    return round(x, 1), round(y, 1), z


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def html(force=False):
    P = json.load(open(ROOT + "/plan.json", encoding="utf-8"))
    idx, mark = ROOT + "/index.html", ROOT + "/.comp-index.sha256"
    if os.path.exists(idx) and not force:
        if not os.path.exists(mark) or open(mark).read().strip() != sha(idx):
            sys.exit(f"{idx} changed since comp.py wrote it (Studio or hand edits). Read them (`hyperframes history --json`), "
                     "carry them into the spec, then rerun with --force.")
    W, H, RAD, LENS = G["W"], G["H"], G["rad"], G["lens"]
    D = round(P["end_f"] / FPS, 4)
    parts, js = [], []
    vid = [0]
    sc = CELL[SCREEN["cam"]] if SCREEN.get("cam") else None

    def video(src, f0, n, track, cls="clip"):   # track = Studio lane (display only)
        vid[0] += 1
        return (f'<video id="vid{vid[0]}" class="{cls}" src="assets/{src}" data-start="{(f0 - EPS) / FPS:.4f}" '
                f'data-duration="{n / FPS:.4f}" data-track-index="{track}" muted playsinline></video>')

    for s in P["spans"]:
        t0, t1 = (s["f0"] - EPS) / FPS, (s["f1"] - EPS) / FPS
        n = s["f1"] - s["f0"]
        sid = f"sp{s['i']:02d}"
        inner = []
        for j, p in enumerate(s["panels"]):
            src = f"s{s['i']:02d}_{j}.mp4"
            x, y, w, h = p["rect"]
            pid = f"{sid}p{j}"
            if p.get("app"):
                rx, ry, rw, rh = p["win"]

                def to_box(fx, fy):
                    return ((fx * sc[2] - rx) / rw, (fy * sc[3] - ry) / rh)

                vstate = [(t0, 0.0, 0.0, 1.0)]
                for (tv, fx, fy, z) in p["views"]:
                    vstate.append((tv, 0.0, 0.0, 1.0) if fx is None else (tv, *view_xy(z, *to_box(fx, fy), w, h)))
                cid = f"{pid}c"
                js.append(f'tl.set("#{cid}", {{x: 0, y: 0, scale: 1}}, 0);')
                for (tv, vx, vy, vz) in vstate[1:]:
                    js.append(f'tl.to("#{cid}", {{x: {vx}, y: {vy}, scale: {vz}, duration: 0.8, ease: "power2.inOut"}}, {tv:.3f});')
                lens_html = ""
                for li, (g0, fx, fy, mag) in enumerate(p["lens"]):
                    ax, ay = to_box(fx, fy)
                    vv = [v for v in vstate if v[0] <= g0][-1]
                    px, py = vv[1] + vv[3] * ax * w, vv[2] + vv[3] * ay * h
                    lx = min(max(px, LENS / 2 + 12), w - LENS / 2 - 12)
                    ly = min(max(py, LENS / 2 + 12), h - LENS / 2 - 12)
                    sl = vv[3] * mag
                    lid = f"{pid}l{li}"
                    lens_html += (f'<div id="{lid}" class="lens" style="width:{LENS}px;height:{LENS}px;left:{lx - LENS / 2:.1f}px;'
                                  f'top:{ly - LENS / 2:.1f}px;transform-origin:{50 + (px - lx) / LENS * 100:.0f}% {50 + (py - ly) / LENS * 100:.0f}%">'
                                  f'<div class="lens-in" id="{lid}i" style="width:{w}px;height:{h}px">{video(src, s["f0"], n, 10 + li)}</div></div>')
                    js.append(f'tl.set("#{lid}i", {{x: {LENS / 2 - sl * ax * w:.1f}, y: {LENS / 2 - sl * ay * h:.1f}, scale: {sl:.4f}}}, 0);')
                    js.append(f'tl.set("#{lid}", {{scale: 0, autoAlpha: 0}}, 0);')
                    js.append(f'tl.set("#{pid}ptr", {{x: {min(px + 150, w - 40):.1f}, y: {min(py + 110, h - 50):.1f}, scale: 1, autoAlpha: 1}}, {g0:.3f});')
                    js.append(f'tl.to("#{pid}ptr", {{x: {px:.1f}, y: {py:.1f}, duration: 0.55, ease: "power2.inOut"}}, {g0:.3f});')
                    js.append(f'tl.to("#{pid}ptr", {{scale: 0, duration: 0.2, ease: "power2.in"}}, {g0 + 0.55:.3f});')
                    js.append(f'tl.set("#{pid}ptr", {{autoAlpha: 0, scale: 1}}, {g0 + 0.76:.3f});')
                    js.append(f'tl.set("#{lid}", {{autoAlpha: 1}}, {g0 + 0.55:.3f});')
                    js.append(f'tl.to("#{lid}", {{scale: 1, duration: 0.4, ease: "power2.out"}}, {g0 + 0.55:.3f});')
                    t_lo = max(g0 + 1.6, t1 - 0.36)
                    js.append(f'tl.to("#{lid}", {{scale: 0, duration: 0.3, ease: "power2.in"}}, {t_lo:.3f});')
                    js.append(f'tl.set("#{lid}", {{autoAlpha: 0}}, {t_lo + 0.31:.3f});')
                ptr = ""
                if p["lens"]:
                    ptr = (f'<svg id="{pid}ptr" class="ptr" viewBox="0 0 34 44"><path d="M3 2 L3 34 L11 26 L17 40 L23 37 L17 24 L29 24 Z" '
                           f'fill="#FFFFFF" stroke="#0E1419" stroke-width="2.5" stroke-linejoin="round"/></svg>')
                    js.append(f'tl.set("#{pid}ptr", {{autoAlpha: 0}}, 0);')
                inner.append(f'<div class="pnl" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;border-radius:{RAD}px;box-shadow:{SHADOW}">'
                             f'<div class="ct" id="{cid}" style="width:{w}px;height:{h}px">{video(src, s["f0"], n, 1 + j)}</div>{lens_html}{ptr}</div>')
            elif p.get("bubble"):
                inner.append(f'<div class="bub" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"><div class="pw">{video(src, s["f0"], n, 1 + j)}</div></div>')
            else:
                r = f"border-radius:{RAD}px;box-shadow:{SHADOW};" if p["round"] else ""
                ox, oy = p["origin"]
                inner.append(f'<div class="pnl" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;{r}">'
                             f'<div class="pw" id="{pid}w" style="transform-origin:{ox * 100:.1f}% {oy * 100:.1f}%">{video(src, s["f0"], n, 1 + j)}</div></div>')
                if p.get("push"):   # [start s into the span, scale, duration s]: an eased push-in inside the panel
                    tr, to, du = p["push"]
                    js.append(f'tl.set("#{pid}w", {{scale: 1}}, 0);')
                    js.append(f'tl.to("#{pid}w", {{scale: {to}, duration: {du}, ease: "power2.inOut"}}, {t0 + tr:.3f});')
        parts.append(f'<div id="{sid}" class="sp">{"".join(inner)}</div>')
        js.append(f'tl.set("#{sid}", {{autoAlpha: 1}}, {t0:.4f});')
        if s["f1"] < P["end_f"]:
            js.append(f'tl.set("#{sid}", {{autoAlpha: 0}}, {t1:.4f});')
    # overlays on a slot (the hero reel, a crop of the screen, a generated UI), under the camera panels in DOM order
    ov_css = ""
    for k, ov in enumerate(overlays(P)):
        x, y, w, h = ov["rect"]
        o0, o1 = (ov["f0"] - EPS) / FPS, (ov["f1"] - EPS) / FPS
        oid = f"ov-{ov['id']}"
        if ov["kind"] in ("video", "master"):
            body = '<div class="pw">' + video("ov_" + ov["id"] + ".mp4", ov["f0"], ov["f1"] - ov["f0"], 9 - k) + "</div>"
            bg = ""
        else:   # html: a fragment written for this slot rect (plan.json hero.rect), its css, its tl.* lines
            body = open(path(per_fmt(ov["html"])), encoding="utf-8").read()
            bg = f"background:{ov.get('bg', PANEL_BG)};"
            if ov.get("css"):
                ov_css += open(path(per_fmt(ov["css"])), encoding="utf-8").read() + "\n"
            if ov.get("js"):
                js += [ln for ln in open(path(per_fmt(ov["js"])), encoding="utf-8").read().splitlines() if ln.strip()]
        parts.insert(k, f'<div id="{oid}" class="pnl" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;border-radius:{RAD}px;'
                        f'box-shadow:{SHADOW};{bg}opacity:0;visibility:hidden">{body}</div>')
        js.append(f'tl.set("#{oid}", {{autoAlpha: 1}}, {o0:.4f});')
        js.append(f'tl.set("#{oid}", {{autoAlpha: 0}}, {o1:.4f});')
    cap_parts, cap_js, cap_css, n_cap = captions(P, D) if S.get("words") else ([], [], "", 0)
    if S.get("audio"):
        parts.append(f'<audio id="master-audio" class="clip" src="assets/audio/{os.path.basename(audio_out())}" data-start="0" '
                     f'data-duration="{D}" data-track-index="0" data-volume="1"></audio>')
    parts += cap_parts
    js += cap_js
    wl = S.get("style", {}).get("wall", {})
    base, mid, edge = wl.get("base", "#0E1419"), wl.get("mid", "#1D2B36"), wl.get("edge", "#15212A")
    b1, b2 = wl.get("band", "76,175,80"), wl.get("band2", "120,160,190")
    css = f"""
#root {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; background: {base}; }}
#wall {{ position: absolute; inset: 0; overflow: hidden; background: linear-gradient(150deg, #0B1116 0%, {mid} 48%, {edge} 72%, #0B1116 100%); }}
#wall-drift {{ position: absolute; left: -15%; top: 0; width: 130%; height: 100%; }}
.band {{ position: absolute; top: -60%; height: 220%; filter: blur(38px); transform: rotate(-28deg); }}
#band1 {{ left: -10%; width: 26%; background: linear-gradient(90deg, rgba({b1},0) 0%, rgba({b1},.20) 50%, rgba({b1},0) 100%); }}
#band2 {{ left: 38%; width: 18%; background: linear-gradient(90deg, rgba({b2},0) 0%, rgba({b2},.16) 50%, rgba({b2},0) 100%); }}
#band3 {{ left: 70%; width: 30%; background: linear-gradient(90deg, rgba({b1},0) 0%, rgba({b1},.12) 50%, rgba({b1},0) 100%); }}
#glow {{ position: absolute; width: 70%; height: 70%; left: 15%; top: 10%; border-radius: 50%; background: radial-gradient(closest-side, rgba({b1},.10), rgba({b1},0)); filter: blur(30px); }}
.sp {{ position: absolute; inset: 0; opacity: 0; visibility: hidden; }}
.pnl {{ position: absolute; overflow: hidden; background: {PANEL_BG}; }}
.pw {{ position: absolute; inset: 0; }}
.pw video, .ct video, .lens-in video {{ position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: fill; }}
.ct {{ position: absolute; left: 0; top: 0; transform-origin: 0 0; }}
.lens {{ position: absolute; border-radius: 50%; overflow: hidden; background: {PANEL_BG}; box-shadow: 0 0 0 4px rgba(255,255,255,.92), 0 12px 30px rgba(0,0,0,.55); opacity: 0; visibility: hidden; }}
.lens-in {{ position: absolute; left: 0; top: 0; transform-origin: 0 0; }}
.ptr {{ position: absolute; left: 0; top: 0; width: 34px; height: 44px; transform-origin: 0 0; opacity: 0; visibility: hidden; }}
.bub {{ position: absolute; border-radius: 50%; overflow: hidden; background: #1A2731; box-shadow: 0 0 0 5px rgba(255,255,255,.10), 0 14px 36px rgba(0,0,0,.5); }}
"""
    css += cap_css + ov_css
    doc = f"""<!doctype html>
<html lang="{S.get('lang', 'en')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={W}, height={H}">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
<title>{H_.escape(S.get('title', 'comp'))} {FMT}</title>
<style>{css}</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{D}" data-width="{W}" data-height="{H}">
  <div id="wall"><div id="wall-drift"><div class="band" id="band1"></div><div class="band" id="band2"></div><div class="band" id="band3"></div></div><div id="glow"></div></div>
  {chr(10).join(parts)}
</div>
<script>
window.__timelines = window.__timelines || {{}};
const tl = gsap.timeline({{ paused: true }});
tl.to("#wall-drift", {{x: {round(W * 0.12)}, duration: {D}, ease: "none"}}, 0);
tl.to("#glow", {{x: -346, y: 86, duration: {D}, ease: "none"}}, 0);
{chr(10).join(js)}
window.__timelines["main"] = tl;
</script>
</body>
</html>
"""
    open(idx, "w", encoding="utf-8", newline="\n").write(doc)
    open(mark, "w").write(sha(idx))
    if not os.path.exists(ROOT + "/hyperframes.json"):
        json.dump({"$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
                   "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
                   "media": {"autoProxy": True}, "authoringSkill": "hyperframes"}, open(ROOT + "/hyperframes.json", "w"), indent=2)
    print("index.html", len(doc), "bytes,", vid[0], "videos,", n_cap, "captions, duration", D, "->", idx)


# ---------------------------------------------------------------- checks
def gray(args, vf, w=64, h=36):
    r = subprocess.run(["ffmpeg", "-v", "error", *args, "-frames:v", "1", "-vf", f"{vf},scale={w}:{h}:flags=area,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    return r.stdout


def mad(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, min(len(a), len(b)))


def verify():
    """every camera track: frame count = span length, and frame 0 matches the master at f0 better than at f0 +- 1
    (an off-by-one start shows one frame of the previous shot at every cut)."""
    P = json.load(open(ROOT + "/plan.json", encoding="utf-8"))
    bad = 0
    for s in P["spans"]:
        for j, p in enumerate(s["panels"]):
            if p.get("bubble"):
                continue
            cx, cy = CELL[p["cam"]][:2]
            x, y, w, h = p["win"]
            crop = f"crop={ev(w)}:{ev(h)}:{ev(cx + x)}:{ev(cy + y)}"
            f = f"{ROOT}/assets/s{s['i']:02d}_{j}.mp4"
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                                "stream=nb_read_packets", "-of", "csv=p=0", f], capture_output=True, text=True)
            n = int(r.stdout.strip() or 0)
            a = gray(["-i", f], "null")
            d = {k: mad(a, gray(["-ss", f"{(s['f0'] + k + 0.25) / FPS:.6f}", "-i", MASTER], crop)) for k in (-1, 0, 1)}
            ok = n == s["f1"] - s["f0"] and d[0] <= min(d[-1], d[1]) + 0.01
            bad += not ok
            if not ok:
                print("BAD", os.path.basename(f), "frames", n, "/", s["f1"] - s["f0"], "diff -1/0/+1", [round(d[k], 2) for k in (-1, 0, 1)])
            break   # first panel of each span is enough: every track is cut the same way
    print("verify:", bad, "bad spans of", len(P["spans"]))
    if bad:
        sys.exit(1)


def gaps(render, fps_s):
    """frames where a big share of the picture is the flat empty panel background (#111): a panel with no frame.
    Pixels within 13-21 on every channel count (#111 = 17, plus encoder drift); an overlay whose `bg` is #111 itself would
    count too, so give generated UIs another background (#181818 = 24 stays out)."""
    n_, _, d_ = fps_s.partition("/")
    fps = float(n_) / float(d_ or 1)
    w, h = 96, 54
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", render, "-vf", f"scale={w}:{h}:flags=area", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    share, k = [], 0
    while True:
        b = p.stdout.read(w * h * 3)
        if len(b) < w * h * 3:
            break
        flat = sum(1 for i in range(0, len(b), 3) if 13 <= min(b[i:i + 3]) and max(b[i:i + 3]) <= 21 and max(b[i:i + 3]) - min(b[i:i + 3]) <= 3)
        share.append(flat / (w * h))
        k += 1
    p.wait()
    runs = []
    for i, v in enumerate(share):
        if v > 0.25:
            if runs and runs[-1][1] == i - 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])
    for a, b in runs:
        print(f"empty panel f{a}-{b}  {a / fps:.2f}-{(b + 1) / fps:.2f} s  max {max(share[a:b + 1]):.2f}")
    print(k, "frames,", len(runs), "runs with an empty panel")
    if runs:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["plan", "enc", "audio", "html", "all", "verify", "gaps"])
    ap.add_argument("render", nargs="?", help="gaps: the rendered file")
    ap.add_argument("--spec", help="comp.json")
    ap.add_argument("--fmt", default="16x9", choices=["16x9", "9x16"])
    ap.add_argument("--force", action="store_true", help="html: overwrite an index.html edited since the last build")
    ap.add_argument("--fps", default="24000/1001", help="gaps: frame rate of the render")
    a = ap.parse_args()
    if a.step == "gaps":
        return gaps(a.render, a.fps)
    if not a.spec:
        ap.error("--spec is required")
    load(a.spec, a.fmt)
    if a.step in ("plan", "all"):
        plan()
    if a.step in ("enc", "all"):
        enc()
    if a.step in ("audio", "all") and S.get("audio"):
        audio()
    if a.step in ("html", "all"):
        html(a.force)
    if a.step == "verify":
        verify()


if __name__ == "__main__":
    main()
