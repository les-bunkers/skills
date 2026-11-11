"""Upload-ready copies of the chosen look for every approved clip, named by posting date, post text beside each video;
optionally each clip also goes into the videos plugin's published layout.

usage: python export_finals.py [-c clips.json] [LOOK] [ID=LOOK …] [--publish-dir DIR] [--all] [--dry]
  LOOK       look for every clip (default: config looks[0]); ID=LOOK overrides one clip
  --all      every scheduled clip (default: only clips with "approved": true in clips.json)
  --publish-dir DIR   also write DIR/<slug>/ (default: config publish.dir; e.g. <project>/artifacts/videos)
  --dry      print what would be written, touch nothing
in:  <out>/_review/schedule.json (review_page.py), renders/<ID>_<look>.mp4, <ID>_<look>_cover.jpg (covers.py),
     clip "post" (caption text) and "hashtags"
out: <out>/_deliver/<date>-<lane>-<n>-<id>.mp4 (hard link of the render) + same-name .txt (post text) + -cover.jpg +
     -share.mp4 (small copy for texting / phone review) + README.md (the plan as a list).
     Publish: DIR/<slug>/<slug>-9x16.mp4, <slug>-cover.jpg, <slug>-share.mp4, release.md (frontmatter title, slug,
     campaign, content, source, formats, platforms, post_date, status, posted, loudness, qc; body ## Caption /
     ## Hashtags / ## Notes; an existing `posted:` value is kept) + its row in DIR/index.md (same slug replaced).
     slug = clip "slug", else publish.slug_prefix (default "<campaign>-" or "clip-") + id.
File names: lowercase letters, digits and hyphens only (messaging apps and uploaders mangle the rest).
"""
import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import FFMPEG, Batch, jload  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
INDEX_HEAD = ("Published videos. One folder per video (release.md + upload-ready files); how it was made = `source:` in "
              "release.md.\n\n| Video | Campaign | Formats | Status | Record |\n|---|---|---|---|---|\n")
FIELDS = ["title", "slug", "campaign", "content", "source", "formats", "platforms", "post_date", "status", "posted",
          "loudness", "qc"]


def slugify(s):
    s = s.lower().replace("_", "-")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", s)).strip("-")


def link(src, dst):
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def copy_if_newer(src, dst):
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)


def share_copy(src, dst):
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "slow", "-crf", "23",
                        "-maxrate", "2600k", "-bufsize", "5200k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", str(dst)], check=True)


def loudness(mp4):
    """Integrated loudness and true peak of the shipped file (ebur128; loudnorm's input_i is biased by silence)."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(mp4), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    tail = r[r.rfind("Summary:"):]
    i = re.search(r"\bI[:] +(-?[0-9.]+) LUFS", tail)
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+) dBFS", tail, re.S)
    return f"{float(i.group(1)):.1f} LUFS / {float(tp.group(1)):.1f} dBTP" if i and tp else ""


def qc_line(d, cid, look):
    """pass <date> when the HyperFrames check is ok and raea raised no P0; else what failed."""
    chk = jload(d / f"check-{look}.json", {}) or {}
    raea = jload(d / "renders" / f"raea-{look}.json", None)
    bad = [] if chk.get("ok") else ["hyperframes check"]
    if raea:
        bad += [f"raea {i['rule']}" for i in raea.get("issues", []) if i.get("p") == "P0"]
    return f"pass {dt.date.today().isoformat()}" if not bad else "check: " + ", ".join(bad)


def read_front(path):
    if not path.exists():
        return {}
    m = re.match(r"---\r?\n(.*?)\r?\n---", path.read_text(encoding="utf-8"), re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line and not line.startswith((" ", "#")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def publish(b, pub, cid, look, day, lane, n, src, cover, share, dry):
    """One clip -> <pub>/<slug>/ (video, cover, share copy, release.md) + its row in <pub>/index.md."""
    c, cfg = b.clip(cid), b.cfg.get("publish", {})
    campaign = cfg.get("campaign", "")
    slug = slugify(c.get("slug") or cfg.get("slug_prefix", f"{campaign}-" if campaign else "clip-") + cid)
    dst = pub / slug
    if dry:
        print(f"  [dry] publish {dst} <- {src.name} (look {look}, {day} {lane}-{n})")
        return
    dst.mkdir(parents=True, exist_ok=True)
    copy_if_newer(src, dst / f"{slug}-9x16.mp4")
    if cover.exists():
        copy_if_newer(cover, dst / f"{slug}-cover.jpg")
    if share.exists():
        copy_if_newer(share, dst / f"{slug}-share.mp4")
    rel = dst / "release.md"
    posted = read_front(rel).get("posted", "")  # filled in by the human after posting
    tags = c.get("hashtags", "")
    tags = " ".join(tags) if isinstance(tags, list) else tags
    checks = sum(1 for f in (jload(b.out / cid / "stt_fixes.json", []) or []) if str(f.get("why", "")).startswith("CHECK"))
    head = {"title": c.get("hook", cid).replace("*", ""), "slug": slug, "campaign": campaign,
            "content": c.get("content", ""), "source": cfg.get("source", ""), "formats": "9x16",
            "platforms": cfg.get("platforms", "instagram youtube tiktok"), "post_date": day,
            "status": "posted" if posted else "ready", "posted": posted,
            "loudness": loudness(dst / f"{slug}-9x16.mp4"), "qc": qc_line(b.out / cid, cid, look)}
    notes = [f"- Clip {cid}, look {look}, slot {lane}-{n} on {day}.",
             f"- Caption words marked CHECK in the transcript fixes: {checks}." if checks else ""]
    body = ("\n## Caption\n\n" + (c.get("post") or "(post text missing)") + "\n\n## Hashtags\n\n" + (tags or "") +
            "\n\n## Notes\n\n" + "\n".join(x for x in notes if x) + "\n")
    rel.write_text("---\n" + "".join(f"{k}: {head[k]}\n" for k in FIELDS) + "---\n" + body, encoding="utf-8")
    index = pub / "index.md"
    if not index.exists():
        index.write_text(INDEX_HEAD, encoding="utf-8")
    rows = [r for r in index.read_text(encoding="utf-8").rstrip("\n").split("\n") if f"[{slug}]" not in r]
    rows.append(f"| [{slug}]({slug}/release.md) | {campaign} | 9x16 | {head['status']} {day} | `{head['source']}` |")
    index.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"  publish {dst}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("looks", nargs="*", help="LOOK and/or ID=LOOK overrides")
    ap.add_argument("--publish-dir")
    ap.add_argument("--all", action="store_true", help="every scheduled clip, approved or not")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    b = Batch(a.config)
    plain = [x for x in a.looks if "=" not in x]
    look = plain[0] if plain else b.looks[0]
    over = dict(x.split("=", 1) for x in a.looks if "=" in x)
    pub = Path(a.publish_dir).resolve() if a.publish_dir else b.resolve(b.cfg.get("publish", {}).get("dir"))
    days = jload(b.out / "_review" / "schedule.json")
    if days is None:
        raise SystemExit("no schedule: run review_page.py first")
    out = b.out / "_deliver"
    if not a.dry:
        out.mkdir(exist_ok=True)
    lines, n_done = [f"# Clip deliveries, look {look}", ""], 0
    for d in days:
        lines.append(f"## {d['label']}")
        for lane, ids in d["lanes"].items():
            for k, cid in enumerate(ids, 1):
                if not a.all and not b.clip(cid).get("approved"):
                    lines.append(f"- {lane} {k}: {cid} not approved yet")
                    continue
                lk = over.get(cid, look)
                src = b.out / cid / "renders" / f"{cid}_{lk}.mp4"
                if not src.exists():
                    lines.append(f"- MISSING {cid} look {lk}")
                    print(f"MISSING {src}")
                    continue
                stem = slugify(f"{d['date']}-{lane}-{k}-{cid}")
                cover = b.out / cid / "renders" / f"{cid}_{lk}_cover.jpg"
                share = out / f"{stem}-share.mp4"
                hook = b.clip(cid).get("hook", cid).replace("*", "")
                if a.dry:
                    print(f"  [dry] {out / (stem + '.mp4')} <- {src.name}")
                else:
                    link(src, out / f"{stem}.mp4")
                    (out / f"{stem}.txt").write_text(b.clip(cid).get("post", ""), encoding="utf-8")
                    if cover.exists():
                        link(cover, out / f"{stem}-cover.jpg")
                    share_copy(src, share)
                if pub:
                    publish(b, pub, cid, lk, d["date"], lane, k, src, cover, share, a.dry)
                n_done += 1
                lines.append(f"- {lane} {k}: **{cid}** look {lk}, « {hook} » → `{stem}.mp4` + `.txt`")
        lines.append("")
    if not a.dry:
        (out / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"{n_done} clips {'would be ' if a.dry else ''}exported -> {out}" + (f" + {pub}" if pub else ""))


if __name__ == "__main__":
    main()
