# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Contact sheet per source: rows = program file then each angle, columns = 6 moments spread over the kept segments
(each angle shifted by its offset). Look at it before trusting the camera planner: green screen in view, an empty
seat, a camera pointed at a screen, a wrong offset (the rows don't show the same instant).

in:  --project edit.json, --cuts rough.cuts.json (or a tight one), optional --src (default: every source with angles)
out: --out-dir (default <project>/scans/) sheet_<src>.jpg, 480x270 tiles
usage: python angle_sheet.py --project cut/edit.json --cuts cut/rough.cuts.json [--src A] [--out-dir D]
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--cuts", required=True)
    ap.add_argument("--src", action="append")
    ap.add_argument("--out-dir")
    a = ap.parse_args()
    P = E.Project(a.project)
    c = E.load_cuts(a.cuts)
    fps = c["fps"]
    od = Path(a.out_dir) if a.out_dir else P.dir / "scans"
    od.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="angle_sheet_")
    for src in a.src or list(dict.fromkeys(s["src"] for s in c["segments"])):
        if not P.angles(src):
            print(f"{src}: no angles")
            continue
        segs = [s for s in c["segments"] if s["src"] == src and not s.get("alt")] or [s for s in c["segments"] if s["src"] == src]
        if not segs:
            print(f"{src}: not in this cut list")
            continue
        pts = [segs[int(k * (len(segs) - 1) / 5)] for k in range(6)]
        ts = [(s["in_f"] + s["out_f"]) / 2 / fps for s in pts]
        if len(segs) == 1:  # one long segment: spread the six moments inside it
            s = segs[0]
            ts = [(s["in_f"] + (s["out_f"] - s["in_f"]) * (k + 0.5) / 6) / fps for k in range(6)]
        rows = [("program", P.file(src), 0.0)] + [(n, x["file"], x["offset_s"] or 0.0) for n, x in P.angles(src).items()
                                                  if os.path.normcase(x["file"]) != os.path.normcase(P.file(src))]
        tiles = []
        for name, f, off in rows:
            for t in ts:
                out = os.path.join(tmp, f"{src}_{len(tiles):02d}.jpg")
                subprocess.run([E.need_ffmpeg(), "-v", "error", "-y", "-ss", f"{max(0, t - off):.3f}", "-i", f, "-frames:v", "1",
                                "-vf", "scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:-1:-1", out], check=True)
                tiles.append(out)
            print(f"  {src} row {len(tiles) // 6}: {name}  offset {off}")
        lst = os.path.join(tmp, f"{src}.txt")
        Path(lst).write_text("".join(f"file '{Path(t).as_posix()}'\n" for t in tiles), encoding="utf-8")
        dst = od / f"sheet_{src}.jpg"
        subprocess.run([E.need_ffmpeg(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-vf", f"tile=6x{len(rows)}",
                        "-frames:v", "1", str(dst)], check=True)
        print(f"{src}: times {[round(t, 1) for t in ts]} -> {dst}")


if __name__ == "__main__":
    main()
