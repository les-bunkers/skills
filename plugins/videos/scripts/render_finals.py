"""Render approved Resolve timelines to master files. Runs inside Resolve through the Resolve MCP `run_script_unsafe`;
from a shell it only prints the snippet (rendering is the human's call: run it once the timeline is approved).

Per timeline: Edit page, check the audio tracks (only the track whose name starts with ARGS audio_track enabled, when
given), load a STOCK preset by timeline width ("YouTube - 2160p" for >= 3840 wide, else "YouTube - 1080p"), then set
only TargetDir / CustomName. Why stock first: after audio-only renders a hand-built H.264 job rendered audio only
while the job list claimed video. The stock YouTube presets give H.264, ~11 Mbps at 4K, a keyframe every 24 frames.
Output name = slug of the timeline name (lowercase letters, digits, hyphens) + ARGS suffix.
ARGS: {"timelines": [names] | "prefix": "...", "target_dir": "...", "audio_track": "MASTER" (optional),
       "suffix": "-master", "start": true}
result: jobs queued (format, size, fps, codecs per job), started or not. Poll GetRenderJobStatus = Complete before
reading a file (ffprobe sees a duration while Resolve still writes). Then finals_check.py on every file.
usage: python render_finals.py --timeline "Studio tour — tight" --target-dir <masters dir> [--audio-track MASTER] --snippet
"""
import json
import re
import sys
import time
import unicodedata
from pathlib import Path


def slug(name):
    s = "".join(ch for ch in unicodedata.normalize("NFD", name) if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def run_in_resolve(resolve, args):
    p = resolve.GetProjectManager().GetCurrentProject()
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
        wide = int(t.GetSetting("timelineResolutionWidth") or 0) >= 3840
        p.LoadRenderPreset(args.get("preset_wide", "YouTube - 2160p") if wide else args.get("preset", "YouTube - 1080p"))
        fname = slug(name) + args.get("suffix", "")
        ok = p.SetRenderSettings({"SelectAllFrames": True, "TargetDir": out, "CustomName": fname, "ExportSubtitle": False})
        jid = p.AddRenderJob()
        j = next((x for x in p.GetRenderJobList() if x["JobId"] == jid), {})
        jobs.append(jid)
        report.append({"timeline": name, "file": f"{out}/{fname}.mp4", "settings_ok": ok,
                       "job": {k: j.get(k) for k in ("FormatWidth", "FormatHeight", "FrameRate", "VideoCodec", "AudioCodec",
                                                     "AudioSampleRate", "IsExportVideo", "IsExportAudio")}})
    started = False
    if jobs and args.get("start", True):
        for _ in range(5):  # StartRendering sometimes returns False on the first call
            if p.StartRendering(jobs, False):
                started = True
                break
            time.sleep(1)
    return {"started": started, "jobs": report, "job_ids": jobs}


def _cli():
    import argparse
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeline", action="append", default=[])
    ap.add_argument("--prefix")
    ap.add_argument("--target-dir", required=True)
    ap.add_argument("--audio-track", help="name prefix of the only audio track that must be enabled (e.g. MASTER)")
    ap.add_argument("--suffix", default="")
    ap.add_argument("--snippet", action="store_true", help="print the run_script_unsafe code (default)")
    a = ap.parse_args()
    args = {"timelines": a.timeline, "prefix": a.prefix, "target_dir": str(Path(a.target_dir).resolve()),
            "audio_track": a.audio_track, "suffix": a.suffix, "start": True}
    me = str(Path(__file__).resolve())
    print(f"g = dict(globals()); g['ARGS'] = {json.dumps(args, ensure_ascii=False)}\n"
          f"exec(open({me!r}, encoding='utf-8').read(), g); result = g['result']")


if "ARGS" in globals() and "resolve" in globals():
    result = run_in_resolve(resolve, ARGS)  # noqa: F821 (injected by the Resolve MCP)
elif __name__ == "__main__":
    _cli()
