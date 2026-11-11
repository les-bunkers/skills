"""Cover image per clip x look: the hook, styled like the look, over the middle of the clip's longest single shot (the
first frame often shows the other person listening, a weak thumbnail). Upload it as the Reel/Short cover.

usage: python covers.py [-c clips.json] [IDS…]     (env LOOKS limits looks; clip "cover_at": seconds overrides the frame)
out:   <clip>/renders/<ID>_<look>_cover.jpg (1080x1920)
How: a one-frame HyperFrames project per look in <clip>/_cover/<look>/ (frame.jpg + hook band), captured with
`hyperframes snapshot --at 0`, converted to JPEG with ffmpeg. Needs the clip's master (and the hook in clips.json).
"""
import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_vertical as BV  # noqa: E402
from vc_config import FFMPEG, OUT_H, OUT_W, Batch, jload  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
HF = os.environ.get("HYPERFRAMES", "hyperframes")


def cover_frame(plan):
    """Middle of the longest single shot that has a face; returns (clip seconds, shot span)."""
    fps, t, best = plan["fps"], 0.0, None
    for s in plan["shots"]:
        n = (s["out_f"] - s["in_f"]) / fps
        if s["layout"] == "single" and s["people"] and (best is None or n > best[1]):
            best = (t, n)
        t += n
    t0, n = best or (0.0, 1.0)
    return round(t0 + n / 2, 2), (t0, t0 + n)


def cover(b, cid, looks):
    d = b.clip_dir(cid)
    plan, clip = jload(d / "plan.json"), b.clip(cid)
    hook = clip.get("hook", "")
    if not hook:
        print(f"{cid}: no hook in clips.json, no cover")
        return
    t, span = cover_frame(plan)
    if "cover_at" in clip:  # the longest single is the other person, or a better expression elsewhere
        t, span = clip["cover_at"], (clip["cover_at"] - 0.1, clip["cover_at"] + 0.1)
    (d / "renders").mkdir(exist_ok=True)
    (top, bot), _ = BV.pick_band([(b.seam - 110, b.seam + 110)], BV.shot_faces(plan), *span)
    reg, pre = BV.fonts(b), BV.presets(b)
    for key in looks:
        st = pre[key]
        work = d / "_cover" / key
        (work / "assets").mkdir(parents=True, exist_ok=True)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(t), "-i", str(d / "assets" / "master.mp4"),
                        "-frames:v", "1", "-q:v", "2", str(work / "assets" / "frame.jpg")], check=True)
        font = st.get("font", "Bricolage Grotesque")
        BV.link(Path(reg[font]["file"]), work / "assets" / "fonts" / Path(reg[font]["file"]).name)
        hsize, hbrk = BV.hook_layout(hook, st)
        doc = f"""<!doctype html>
<html lang="{html.escape(b.episode_of(cid)['language'])}"><head><meta charset="utf-8">
<meta name="viewport" content="width={OUT_W}, height={OUT_H}">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
<title>{html.escape(cid)} cover {key}</title><style>
{BV.font_faces([font], reg)}
#root {{ position: relative; width: {OUT_W}px; height: {OUT_H}px; overflow: hidden; background: #000;
  font-family: '{font}', sans-serif; font-optical-sizing: auto; }}
#frame {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
.band-slot {{ position: absolute; left: 0; right: 0; top: {top}px; height: {bot - top}px; display: flex; align-items: center; justify-content: center; }}
#hook-line {{ text-align: center; padding: 0 70px; text-wrap: balance; {BV.text_shadow(st)} }}
{BV.hook_css(st, hsize)}{BV.slab_css(st)}
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="1" data-width="{OUT_W}" data-height="{OUT_H}">
  <img id="frame" src="assets/frame.jpg" alt="">
  <div class="band-slot"><div id="hook-line" class="{"slab" if st["band"] else ""}"><span>{BV.hook_html(hook, hbrk)}</span></div></div>
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
window.__timelines["main"] = tl;
</script>
</body></html>
"""
        (work / "index.html").write_text(doc, encoding="utf-8")
        (work / "hyperframes.json").write_text(json.dumps(BV.HF_CONFIG, indent=2), encoding="utf-8")
        snap = d / "_cover" / f"snap-{key}"
        shutil.rmtree(snap, ignore_errors=True)
        r = subprocess.run(f'{HF} snapshot . --at 0 --no-end --keep --output "{snap}"', cwd=work, shell=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        png = next(iter(sorted(snap.glob("*.png"))), None) if snap.exists() else None
        if not png:
            print(f"  {cid} {key}: cover FAILED\n    " + "\n    ".join((r.stdout + r.stderr).strip().splitlines()[-4:]))
            continue
        out = d / "renders" / f"{cid}_{key}_cover.jpg"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(png), "-vf", f"scale={OUT_W}:{OUT_H}", "-q:v", "2",
                        str(out)], check=True)
        print(f"  {cid} {key}: cover at {t:.1f}s -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    batch = Batch(a.config)
    for cid in a.ids or [c for c in batch.clip_ids() if (batch.out / c / "plan.json").exists()]:
        cover(batch, cid, batch.looks_for(cid))
