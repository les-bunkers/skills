"""Render approved Resolve timelines to master files. Runs inside Resolve through the Resolve MCP `run_script_unsafe`;
from a shell it only prints the snippet (rendering is the human's call: run it once the timeline is approved).

Per timeline: Edit page, check the audio tracks (only the track whose name starts with ARGS audio_track enabled, when
given), load the MASTER preset for its size (resolve_presets.py builds it once per Resolve from a stock preset:
3840x2160 16:9 -> "videos master 4K HEVC 80M" = H.265 NVIDIA Main10 VBR 80 Mbps; every other size, 9x16 and shorts
included -> "videos master H264 20M" = H.264 NVIDIA VBR 20 Mbps; AAC 320k), then set only TargetDir / CustomName,
plus the frame size on a vertical (9x16) timeline. A job whose size differs from its timeline's is deleted, never
rendered. Why a preset and not settings: the API ignores VideoQuality (the stock presets render ~11 Mbps at 4K), and
after audio-only renders a hand-built job rendered audio only while the job list claimed video. No NVIDIA encoder ->
the stock "YouTube - 2160p" / "YouTube - 1080p" preset, and the job's report says "preset_fallback". Name the timelines by format ("Studio tour 9x16", "Studio tour 16x9",
shorts "Studio tour 9x16 S01 …"): prefix "Studio tour 9x16 S" renders every short of the video.
Output name = slug of the timeline name (lowercase letters, digits, hyphens) + ARGS suffix.
ARGS: {"timelines": [names] | "prefix": "...", "target_dir": "...", "audio_track": "MASTER" (optional),
       "suffix": "-master", "start": true, "scripts_dir": "<this folder>", "preset_wide" / "preset": a preset name
       to use instead of the masters}
result: jobs queued (format, size, fps, codecs per job), started or not. Poll GetRenderJobStatus = Complete before
reading a file (ffprobe sees a duration while Resolve still writes). Then finals_check.py on every file.
usage: python render_finals.py --timeline "Studio tour" --target-dir <masters dir> [--audio-track MASTER] --snippet
"""
import re
import sys
import time
import unicodedata
from pathlib import Path


def slug(name):
    s = "".join(ch for ch in unicodedata.normalize("NFD", name) if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def run_in_resolve(resolve, args):
    import importlib
    sys.path.insert(0, args["scripts_dir"])
    import resolve_presets as RP
    importlib.reload(RP)  # Resolve keeps modules between calls
    p = resolve.GetProjectManager().GetCurrentProject()
    made = {}
    out = args["target_dir"]
    Path(out).mkdir(parents=True, exist_ok=True)
    want = set(args.get("timelines") or [])
    prefix = args.get("prefix")
    resolve.OpenPage("edit")  # the Deliver page swallows track toggles
    jobs, report = [], []
    for i in range(1, p.GetTimelineCount() + 1):
        t = p.GetTimelineByIndex(i)
        name = t.GetName()
        if not ((want and name in want) or (prefix and name.startswith(prefix))):
            continue
        p.SetCurrentTimeline(t)
        if args.get("audio_track"):
            on = [t.GetIsTrackEnabled("audio", n) for n in range(1, t.GetTrackCount("audio") + 1)]
            names = [t.GetTrackName("audio", n) or "" for n in range(1, t.GetTrackCount("audio") + 1)]
            ok = [n for n, (e, nm) in enumerate(zip(on, names)) if e] == [n for n, nm in enumerate(names)
                                                                          if nm.startswith(args["audio_track"])]
            if not ok or not any(nm.startswith(args["audio_track"]) for nm in names):
                report.append({"timeline": name, "skipped": f"audio tracks enabled {on} names {names}"})
                continue
        w = int(float(t.GetSetting("timelineResolutionWidth") or 0))
        h = int(float(t.GetSetting("timelineResolutionHeight") or 0))
        kind = RP.kind_for(w, h)
        preset, fallback = args.get("preset_wide" if kind == "uhd" else "preset"), None
        if not preset:
            if kind not in made:
                made[kind] = RP.ensure(resolve, p, kind)
            preset = made[kind].get("preset")
            if not preset:
                fallback = made[kind].get("error")
                preset = "YouTube - 2160p" if kind == "uhd" else "YouTube - 1080p"
        if not p.LoadRenderPreset(preset):
            report.append({"timeline": name, "skipped": f"render preset {preset!r} did not load"})
            continue
        fname = slug(name) + args.get("suffix", "")
        s = {"SelectAllFrames": True, "TargetDir": out, "CustomName": fname, "ExportSubtitle": False}
        if h > w:  # vertical (9x16) timeline: the stock YouTube presets are landscape, so the size follows the timeline
            s.update(FormatWidth=w, FormatHeight=h)
        ok = p.SetRenderSettings(s)
        jid = p.AddRenderJob()
        j = next((x for x in p.GetRenderJobList() if x["JobId"] == jid), {})
        size = (j.get("FormatWidth"), j.get("FormatHeight"))
        if w and h and all(size) and tuple(map(int, size)) != (w, h):  # never a 9x16 cut in a landscape frame
            p.DeleteRenderJob(jid)
            report.append({"timeline": name, "skipped": f"job size {size[0]}x{size[1]} != timeline {w}x{h}: job deleted",
                           "settings_ok": ok})
            continue
        jobs.append(jid)
        report.append({"timeline": name, "file": f"{out}/{fname}.mp4", "settings_ok": ok, "preset": preset,
                       **({"preset_fallback": fallback} if fallback else {}),
                       "job": {k: j.get(k) for k in ("FormatWidth", "FormatHeight", "FrameRate", "VideoCodec", "AudioCodec",
                                                     "AudioSampleRate", "IsExportVideo", "IsExportAudio")}})
    started = False
    if jobs and args.get("start", True):
        for _ in range(5):  # StartRendering sometimes returns False on the first call
            if p.StartRendering(jobs, False):
                started = True
                break
            time.sleep(1)
    return {"started": started, "jobs": report, "job_ids": jobs, "presets_made": made}


def _cli():
    import argparse
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeline", action="append", default=[])
    ap.add_argument("--prefix")
    ap.add_argument("--target-dir", required=True)
    ap.add_argument("--audio-track", help="name prefix of the only audio track that must be enabled (e.g. MASTER)")
    ap.add_argument("--suffix", default="")
    ap.add_argument("--snippet", action="store_true", help="print the run_script_unsafe code (default)")
    a = ap.parse_args()
    args = {"timelines": a.timeline, "prefix": a.prefix, "target_dir": str(Path(a.target_dir).resolve()),
            "audio_track": a.audio_track, "suffix": a.suffix, "start": True,
            "scripts_dir": str(Path(__file__).resolve().parent)}
    me = str(Path(__file__).resolve())
    print(f"g = dict(globals()); g['ARGS'] = {args!r}\n"  # a Python literal: JSON's null / true break the exec
          f"exec(open({me!r}, encoding='utf-8').read(), g); result = g['result']")


if "ARGS" in globals() and "resolve" in globals():
    result = run_in_resolve(resolve, ARGS)  # noqa: F821 (injected by the Resolve MCP)
elif __name__ == "__main__":
    _cli()
