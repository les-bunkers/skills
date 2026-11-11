"""Batch driver for the vertical clip pipeline. Skips any step whose output is already newer than its inputs.

usage: python run_batch.py [-c clips.json] STAGE [IDS…]
  prep    plan (shots + faces) -> transcript review (OpenAI cached) -> master (crop + LUT + -14 LUFS)
  final   transcript --apply -> build looks -> hyperframes check -> render -> raea check (when installed)
  all     prep then final, clip by clip (re-plans and re-masters clips whose edges.json changed their ranges)
  sheets  <clip>/renders/sheet.jpg: one row of frames per rendered look
IDS default = every clip in the config. One failure does not stop the batch. Log: <out>/_logs/<stage>-<time>.log.
env: LOOKS="signal editorial" (default: clip "looks", else config "looks"), FORCE=1 re-runs plan/master/apply/render,
     STEPS="plan review master" (prep subset), PAID=1 lets the review make its one paid OpenAI call per uncached clip,
     MIN_FREE_GB=3 (a render waits up to 10 min for that much free RAM, else is skipped),
     UV, HYPERFRAMES, RAEA, FFMPEG, FFPROBE = command overrides.
Plan re-runs only when the clip's kept ranges changed (manual plan.json edits survive). The review never re-runs once
stt_review.json exists (it would renumber hunks and orphan stt_fixes.json): delete both to redo it.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import FFMPEG, SCRIPTS, Batch, fps_arg, jload, raea_bin  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
PY = sys.executable
UV = os.environ.get("UV", "uv")
HF = os.environ.get("HYPERFRAMES", "hyperframes")
FORCE = os.environ.get("FORCE") == "1"
PAID = os.environ.get("PAID") == "1"
STEPS = os.environ.get("STEPS", "plan review master").split()
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "3"))
LOG = None


def say(msg):
    print(msg)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def fresh(out, *inputs):
    """out exists and is newer than every existing input."""
    out = Path(out)
    if FORCE or not out.exists():
        return False
    return all(out.stat().st_mtime >= Path(i).stat().st_mtime for i in inputs if i and Path(i).exists())


def run(cid, what, cmd, cwd=None, out=None, ok_codes=(0,)):
    """cmd = list, or a shell string (HyperFrames: the npm shim on Windows is a .cmd, it needs the shell)."""
    t = time.time()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       shell=isinstance(cmd, str))
    ok = r.returncode in ok_codes
    if out:
        Path(out).write_text(r.stdout, encoding="utf-8")
    tail = (r.stdout if not out else "").strip().splitlines()[-2:] + r.stderr.strip().splitlines()[-3:]
    say(f"[{time.strftime('%H:%M:%S')}] {cid} {what}: {'ok' if ok else 'FAIL ' + str(r.returncode)} "
        f"({time.time() - t:.0f}s)" + ("" if ok and not tail else "\n    " + "\n    ".join(tail)))
    return r.returncode if not ok else 0


def script(name, *args):
    return [PY, str(SCRIPTS / name), "-c", str(B.path), *args]


def uv_script(name, *args):
    return [UV, "run", "--quiet", "--script", str(SCRIPTS / name), "-c", str(B.path), *args]


def free_ram_gb():
    """Available physical memory in GB, or None when the platform doesn't say."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                           [(n, ctypes.c_ulonglong) for n in ("total", "avail", "tpf", "apf", "tv", "av", "aev")]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.avail / 2 ** 30
        if Path("/proc/meminfo").exists():
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2 ** 20
        if sys.platform == "darwin":
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            page = 16384 if "16384" in vm.splitlines()[0] else 4096
            pages = sum(int(line.split()[-1].rstrip(".")) for line in vm.splitlines()
                        if line.startswith(("Pages free", "Pages inactive", "Pages speculative")))
            return pages * page / 2 ** 30
    except Exception:  # noqa: BLE001
        return None
    return None


def ram_ok(cid):
    """Two render chains plus a VM once left ~2 GB free and spawned processes started failing: wait for headroom."""
    for _ in range(20):
        gb = free_ram_gb()
        if gb is None or gb >= MIN_FREE_GB:
            return True
        say(f"    {cid}: {gb:.1f} GB free < {MIN_FREE_GB} GB, waiting before render")
        time.sleep(30)
    say(f"    {cid}: render skipped (low RAM); rerun final later")
    return False


def prep(cid):
    d = B.clip_dir(cid)
    ep = B.episode_of(cid)
    plan_p = d / "plan.json"
    if "plan" in STEPS:
        old = (jload(plan_p) or {}).get("segments")
        if FORCE or old != B.clip_segments(cid):
            if run(cid, "plan", uv_script("clip_plan.py", cid)):
                return False
    if "review" in STEPS and not (d / "stt_review.json").exists():
        code = run(cid, "transcript review", script("stt_reconcile.py", cid, *(["--paid"] if PAID else [])),
                   ok_codes=(0,))
        if code == 3:
            say(f"    {cid}: no cached OpenAI transcript; ask before paying, then PAID=1 (or run the review with "
                "--two-sources)")
        if code:
            return False
    if "master" in STEPS and plan_p.exists() and not fresh(d / "assets" / "master.mp4", plan_p,
                                                           SCRIPTS / "clip_master.py", ep.get("lut"), ep["audio_src"]):
        if run(cid, "master", uv_script("clip_master.py", cid)):
            return False
    return True


def final(cid):
    d = B.clip_dir(cid)
    if not (d / "stt_fixes.json").exists() or not (d / "assets" / "master.mp4").exists():
        say(f"{cid}: skipped (stt_fixes.json or master.mp4 missing)")
        return
    if not fresh(d / "words.json", d / "stt_fixes.json", d / "stt_review.json", d / "edges.json"):
        if run(cid, "transcript apply", script("stt_reconcile.py", cid, "--apply")):
            return
    looks = B.looks_for(cid)
    if run(cid, f"build {' '.join(looks)}", script("build_vertical.py", cid, *looks)):
        return
    (d / "renders").mkdir(exist_ok=True)
    rate = fps_arg(B.episode_of(cid)["fps"])
    for s in looks:
        proj, mp4 = d / f"9x16-{s}", d / "renders" / f"{cid}_{s}.mp4"
        if fresh(mp4, proj / "index.html", d / "assets" / "master.mp4"):
            continue
        run(cid, f"check {s}", f"{HF} check --json", cwd=proj, out=d / f"check-{s}.json")
        chk = jload(d / f"check-{s}.json")
        if chk is None:
            say(f"    {cid} {s}: check json unreadable")
        elif not chk.get("ok", False):
            say(f"    {cid} {s}: hyperframes check not ok -> see check-{s}.json")
        if ram_ok(cid):
            # output path outside the project dir (a render into its own project fails)
            run(cid, f"render {s}", f'{HF} render . --fps {rate} --output "{mp4}" --quiet', cwd=proj)
    raea = raea_bin()
    for s in looks:
        mp4, qc = d / "renders" / f"{cid}_{s}.mp4", d / "renders" / f"raea-{s}.json"
        if raea and mp4.exists() and not fresh(qc, mp4):
            run(cid, f"raea {s}", [raea, "check", str(mp4), "--platform", "reels", "-o", str(qc)], ok_codes=(0, 1))


def sheets(cid):
    d = B.clip_dir(cid) / "renders"
    vids = [(s, d / f"{cid}_{s}.mp4") for s in B.looks_for(cid) if (d / f"{cid}_{s}.mp4").exists()]
    if not vids:
        return
    D = json.load(open(B.clip_dir(cid) / "master.json", encoding="utf-8"))["duration_s"]
    fps = float(B.episode_of(cid)["fps"])
    ts = [0.5] + [round(3.2 + (D - 4) * k / 4, 2) for k in range(5)]
    rows = []
    for _, v in vids:
        row = d / f"_row-{v.stem}.png"
        sel = "+".join(f"eq(n\\,{int(t * fps)})" for t in ts)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(v), "-vf", f"select='{sel}',scale=270:480,tile=6x1",
                        "-frames:v", "1", str(row)], check=False)
        rows.append(row)
    args = sum((["-i", str(r)] for r in rows), [])
    subprocess.run([FFMPEG, "-v", "error", "-y", *args, "-filter_complex", f"vstack={len(rows)}" if len(rows) > 1
                    else "null", str(d / "sheet.jpg")], check=False)
    for r in rows:
        r.unlink(missing_ok=True)
    say(f"{cid}: sheet {' '.join(s for s, _ in vids)} -> {d / 'sheet.jpg'}")


def everything(cid):
    if prep(cid):
        final(cid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("stage", choices=["prep", "final", "all", "sheets"])
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    B = Batch(a.config)
    ids = a.ids or B.clip_ids()
    (B.out / "_logs").mkdir(parents=True, exist_ok=True)
    LOG = B.out / "_logs" / f"{a.stage}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    fn = {"prep": prep, "final": final, "all": everything, "sheets": sheets}[a.stage]
    say(f"[{time.strftime('%H:%M:%S')}] {a.stage}: {len(ids)} clips")
    for cid in ids:
        try:
            fn(cid)
        except Exception as e:  # noqa: BLE001
            say(f"{cid}: ERROR {type(e).__name__}: {e}")
    say(f"[{time.strftime('%H:%M:%S')}] {a.stage}: done")
