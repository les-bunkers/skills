"""One-table QC of every clip x look: render frames vs master, size, audio stream, hyperframes check, raea flags,
caption-band warnings, stale renders (older than the master). Prints problems only.

usage: python qc_report.py [-c clips.json] [IDS…]     (env LOOKS limits looks)
out:   <out>/_review/qc.json (one row per clip x look)
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import FFPROBE, OUT_H, OUT_W, Batch, jload  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def probe(mp4):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,nb_frames,width,height",
                        "-of", "json", str(mp4)], capture_output=True, text=True)
    st = json.loads(r.stdout or "{}").get("streams", [])
    v = next((s for s in st if s["codec_type"] == "video"), {})
    return int(v.get("nb_frames", 0) or 0), (v.get("width"), v.get("height")), any(s["codec_type"] == "audio" for s in st)


def rows_for(b, cid):
    d = b.out / cid
    m = jload(d / "master.json", {}) or {}
    out = []
    for s in b.looks_for(cid):
        mp4 = d / "renders" / f"{cid}_{s}.mp4"
        row = {"id": cid, "look": s, "exists": mp4.exists(), "problems": []}
        if not mp4.exists():
            row["problems"].append("not rendered")
        else:
            n, wh, audio = probe(mp4)
            chk, raea, build = (jload(d / f"check-{s}.json", {}) or {}, jload(d / "renders" / f"raea-{s}.json", {}) or {},
                                jload(d / f"build-{s}.json", {}) or {})
            master = d / "assets" / "master.mp4"
            stale = master.exists() and mp4.stat().st_mtime < master.stat().st_mtime
            row.update(frames=n, master_frames=m.get("frames"), size=list(wh), audio=audio, check_ok=chk.get("ok"),
                       raea=[f"{i['p']} {i['rule']} @{i['at']:.1f}s" for i in raea.get("issues", [])],
                       raea_pass=raea.get("passed", []), bands=len(build.get("band_warnings", [])), stale=stale,
                       loudness=[m.get("I"), m.get("TP")])
            if m.get("frames") and abs(n - m["frames"]) > 1:
                row["problems"].append(f"frames {n} vs master {m['frames']}")
            if tuple(wh) != (OUT_W, OUT_H):
                row["problems"].append(f"size {wh}")
            if not audio:
                row["problems"].append("no audio stream")
            if chk.get("ok") is False:
                row["problems"].append("hyperframes check not ok")
            if stale:
                row["problems"].append("render older than master")
            if m.get("I") is not None and abs(m["I"] + 14) > 0.5:
                row["problems"].append(f"master loudness {m['I']} LUFS")
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    b = Batch(a.config)
    ids = a.ids or [c for c in b.clip_ids() if (b.out / c / "plan.json").exists()]
    rows = [r for cid in ids for r in rows_for(b, cid)]
    (b.out / "_review").mkdir(parents=True, exist_ok=True)
    (b.out / "_review" / "qc.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    problems = [r for r in rows if r["problems"]]
    print(f"{len(rows)} renders, {sum(r['exists'] for r in rows)} present, {len(problems)} with problems")
    for p in problems:
        print(f"  PROBLEM {p['id']} {p['look']}: {'; '.join(p['problems'])}")
    flags = {}
    for r in rows:
        for f in r.get("raea", []):
            flags.setdefault(f.split(" @")[0], []).append(f"{r['id']}/{r['look']}")
    for k, v in sorted(flags.items()):
        print(f"  raea {k}: {len(v)} renders: {', '.join(sorted(set(v)))}")
    bands = [f"{r['id']}/{r['look']}" for r in rows if r.get("bands")]
    if bands:
        print(f"  caption band warnings (look at those moments): {', '.join(bands)}")


if __name__ == "__main__":
    main()
