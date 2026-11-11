"""Cut list -> DaVinci Resolve timeline (multicam aware) + review markers. Runs inside Resolve through Blackmagic's
Resolve MCP `run_script_unsafe` (full Python, 60 s per call), or from a shell as a dry run that prints the plan.

Dry run (no Resolve): python resolve_build.py --project cut/edit.json --cuts cut/tight.cuts.json [--mode build] --dry-run
Exec snippet for run_script_unsafe (prints it): python resolve_build.py --project ... --cuts ... --mode build --snippet
Inside Resolve, the snippet sets ARGS and __file__ and execs this file; `result` = what happened (counts, failures).

Modes (one per MCP call; each stays well under 60 s for a 20-min cut):
  import          every source's program file + angle files (+ audio_files) into <root_bin>/Rushes/<source id>
  multicam        one multicam clip per source with angles (program = angle 1, synced on audio); clips renamed
                  "1 <id> program", "2 <id> <angle>" ... because Resolve orders angles by clip NAME
  build           timeline from the cut list. Rough list: V1 multicam (or program file), linked sound; V2 face angle
                  on face segments. Tight list (has v_in_f): V1 picture and A1 sound appended apart (J-cuts: sound
                  before picture), sub-frame audio edges + 1-frame "Cross Fade +3 dB" on in-the-voice cuts, V2 face,
                  V3 camera switches. Subtitles (the cut list's srt) first, then the cut list's markers.
  review_markers  one marker per tighten cut that needs ears (colours below) + Lavender at every J join
  inspect         V1/A1 items of the named timeline vs this cut list's plan: same = no hand edits, safe to rebuild
  read_cuts       the named timeline's A1 items as they are now -> Cut list on source frames (ARGS out = path):
                  captions, graphics and formats follow the edit the human approved, never the planned cut list
Never deletes: a timeline with the same name -> error, unless ARGS["replace"] = True (inspect it first). Current
timeline and page are restored after a build.

ARGS keys: mode, project (edit.json), cuts (*.cuts.json), name (timeline name, default the cut list's), replace,
out (read_cuts).
edit.json "resolve": {"root_bin": "Shoot 2026-03-02"} = top media-pool folder (default "Edit"). Review colours:
Fuchsia phrase trim on a soft dip · Sand timed filler on a soft dip · Sky filler cut inside the voice (listen first)
· Pink phrase trim (check meaning) · Mint timed filler · Cyan breath glued to a word · Lavender J-cut.
Verified API behaviour this relies on (Resolve Studio 21.2): engine.md § Resolve.
"""
import json
import math
import os
import sys
from pathlib import Path


def _here():
    f = globals().get("__file__")
    return Path(f).resolve().parent if f else Path.cwd()


sys.path.insert(0, str(_here()))
import editlib as E  # noqa: E402

REVIEW = [  # (kind test, colour, name): listen-first variants, then timed fillers, then phrases, then glued breaths
    (lambda k: k == "phrase (tight)", "Fuchsia", "phrase trim on a soft dip — listen"),
    (lambda k: k == "filler (timed, tight)", "Sand", "filler cut on a soft dip — listen"),
    (lambda k: k == "filler (timed, in the voice)", "Sky", "filler cut inside the voice, 1-frame cross fade — listen"),
    (lambda k: k == "phrase", "Pink", "phrase trim — check the meaning"),
    (lambda k: k == "filler (timed)", "Mint", "timed filler cut — listen"),
    (lambda k: "glued" in k, "Cyan", "breath glued to a word (ears) — listen"),
]


# ---------------------------------------------------------------- plan (pure Python: dry run and build share it)

def mc_offset(P, sid):
    """Where program frame 0 sits inside the source's multicam clip: a full-extents multicam starts with the
    earliest-starting angle; angle a starts round(offset_a * fps) after the program -> offset = -min(0, min starts)."""
    mc = P.src(sid).get("multicam") or {}
    if mc.get("out_offset_f") is not None:
        return int(mc["out_offset_f"])
    starts = [0] + [round((a["offset_s"] or 0.0) * P.fps) for a in P.angles(sid).values()]
    return -min(0, min(starts))


def mc_name(P, sid):
    mc = P.src(sid).get("multicam")
    if mc is False:
        return None
    if isinstance(mc, dict) and mc.get("name"):
        return mc["name"]
    return f"MC {sid}" if P.angles(sid) else None


def plan(P, cut, use_mc):
    """Every item the build appends, in record frames from the timeline start. use_mc(sid) -> bool."""
    fps = E.norm_fps(cut["fps"])
    segs = cut["segments"]
    tight = any("v_in_f" in s for s in segs)
    out = {"tight": tight, "pic": [], "snd": [], "linked": [], "face": [], "cams": [], "xf": 0, "fade_in": 0,
           "markers": list(cut.get("markers", [])), "srt": cut.get("srt"), "fps": fps,
           "total_f": E.total_f(segs)}
    for s in segs:
        sid = s["src"]
        mc = use_mc(sid)
        off = mc_offset(P, sid) if mc else 0
        clip = ("mc", mc_name(P, sid)) if mc else ("file", P.file(sid))
        face_file, d = P.face(sid)
        if not tight:
            out["linked"].append({"clip": clip, "startFrame": s["in_f"] + off, "endFrame": s["out_f"] + off,
                                  "recordFrame": s["rec_f"], "trackIndex": 1})
            if s.get("face") and face_file:
                fi, fo, rec = s["in_f"] - d, s["out_f"] - d, s["rec_f"]
                if fi < 0:
                    rec, fi = rec - fi, 0
                out["face"].append({"clip": ("file", face_file), "startFrame": fi, "endFrame": fo, "recordFrame": rec,
                                    "trackIndex": 2, "mediaType": 1})
            continue
        if s.get("angle"):  # camera switch: that angle's own clip, synced, on V3
            ang = s["angle"]
            da = ang["delay_f"]
            fi, fo, rec = s["v_in_f"] - da, s["v_out_f"] - da, s["v_rec_f"]
            fo = min(fo, fi + ang.get("max_f", fo - fi))
            if fi < 0:
                rec, fi = rec - fi, 0
            out["cams"].append({"clip": ("file", P.p(ang["file"])), "startFrame": fi, "endFrame": fo, "recordFrame": rec,
                                "trackIndex": 3, "mediaType": 1, "angle": ang["name"]})
        out["pic"].append({"clip": clip, "startFrame": s["v_in_f"] + off, "endFrame": s["v_out_f"] + off,
                           "recordFrame": s["v_rec_f"], "trackIndex": 1, "mediaType": 1})
        # sound: a_in_f / a_out_f / a_rec_f may be fractional (in-the-voice cuts). Appended on whole source frames
        # rounded inward (AppendToTimeline rounds startFrame/endFrame; recordFrame may be fractional), then trimmed
        # out to the exact edges
        ai, ao, ar = s.get("a_in_f", s["in_f"]), s.get("a_out_f", s["out_f"]), s.get("a_rec_f", s["rec_f"])
        si, so = math.ceil(ai - 1e-6), math.floor(ao + 1e-6)
        out["snd"].append({"clip": clip, "startFrame": si + off, "endFrame": so + off, "recordFrame": ar + (si - ai),
                           "trackIndex": 1, "mediaType": 2, "exact": (ai, ao, ar), "xf": bool(s.get("xf")),
                           "fade_in": bool(s.get("fade_in"))})
        out["xf"] += bool(s.get("xf"))
        out["fade_in"] += bool(s.get("fade_in"))
        if s.get("face") and face_file:
            fi, fo, rec = s["v_in_f"] - d, s["v_out_f"] - d, s["v_rec_f"]
            if fi < 0:
                rec, fi = rec - fi, 0
            out["face"].append({"clip": ("file", face_file), "startFrame": fi, "endFrame": fo, "recordFrame": rec,
                                "trackIndex": 2, "mediaType": 1})
    return out


def review_plan(cut):
    fps = E.norm_fps(cut["fps"])
    marks = []
    for x in cut.get("cuts", []):
        hit = next((r for r in REVIEW if any(r[0](k) for k in x["kinds"])), None)
        if not hit:
            continue
        note = f"{x['src']} {x['src_s']:.2f} s — …{x['before']} ✂ {' '.join(x['dropped'])} ✂ {x['after']}… ({', '.join(x['kinds'])})"
        marks.append((round(x["rec_s"] * fps), hit[1], hit[2], note))
    segs = cut["segments"]
    for i, s in enumerate(segs[:-1]):
        if s.get("J"):
            marks.append((segs[i + 1]["rec_f"], "Lavender", f"J {s['J']} fr — sound comes in before the picture",
                          f"{s['src']}: next part's picture {s['J']} frames after its sound"))
    return marks


def summary(pl, marks=None):
    n = {"tight": pl["tight"], "V1": len(pl["pic"] or pl["linked"]), "A1": len(pl["snd"] or pl["linked"]),
         "V2_face": len(pl["face"]), "V3_cams": len(pl["cams"]), "xfade_joins": pl["xf"], "fade_in": pl["fade_in"],
         "markers": len(pl["markers"]), "length_f": pl["total_f"]}
    if marks is not None:
        cnt = {}
        for m in marks:
            cnt[m[1]] = cnt.get(m[1], 0) + 1
        n["review_markers"] = cnt
    return n


# ---------------------------------------------------------------- Resolve side

def run_in_resolve(resolve, project, args):
    P = E.Project(args["project"])
    rcfg = P.cfg.get("resolve") or {}
    mp = project.GetMediaPool()
    root = mp.GetRootFolder()
    report = {}

    def sub(parent, name):
        for f in parent.GetSubFolderList() or []:
            if f.GetName() == name:
                return f
        return mp.AddSubFolder(parent, name)

    def index(folder, acc):
        for c in folder.GetClipList() or []:
            p = c.GetClipProperty("File Path")
            if p:
                acc[os.path.normcase(os.path.normpath(p))] = c
        for s in folder.GetSubFolderList() or []:
            index(s, acc)
        return acc

    def key(p):
        return os.path.normcase(os.path.normpath(p))

    top = sub(root, rcfg.get("root_bin", "Edit"))
    mode = args.get("mode", "build")

    def timelines():
        return [project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)]

    if mode == "import":
        have = index(top, {})
        rushes = sub(top, "Rushes")
        for sid in P.sources:
            files = [P.file(sid)] + [a["file"] for a in P.angles(sid).values()]
            files = list(dict.fromkeys(f for f in files if f))
            b = sub(rushes, sid)
            new = [f for f in files if key(f) not in have]
            if new:
                mp.SetCurrentFolder(b)
                for g in mp.ImportMedia(new) or []:
                    have[key(g.GetClipProperty("File Path"))] = g
            wavs = [P.p(w) for w in P.src(sid).get("audio_files") or []]
            neww = [w for w in wavs if key(w) not in have]
            if neww:
                mp.SetCurrentFolder(sub(b, "Audio"))
                for g in mp.ImportMedia(neww) or []:
                    have[key(g.GetClipProperty("File Path"))] = g
            missing = [f for f in files + wavs if key(f) not in have]
            report[sid] = {"files": len(files) + len(wavs), "new": len(new) + len(neww), "missing": missing}
        return report

    idx = index(top, {})
    mcbin = sub(top, "Multicam")
    mcs = {c.GetClipProperty("Clip Name"): c for c in mcbin.GetClipList() or []}

    if mode == "multicam":
        for sid in P.sources:
            name = mc_name(P, sid)
            if not name:
                report[sid] = {"mc": False, "why": "no angles"}
                continue
            if name not in mcs:
                order = [("program", P.file(sid))] + [(n, a["file"]) for n, a in P.angles(sid).items() if a["file"] != P.file(sid)]
                items = []
                for k, (n, f) in enumerate(order, 1):
                    it = idx.get(key(f))
                    if it is None:
                        report[sid] = {"error": "not imported (run mode import)", "file": f}
                        break
                    it.SetClipProperty("Clip Name", f"{k} {sid} {n}")  # angle order = clip name sort
                    items.append(it)
                else:
                    mp.SetCurrentFolder(mcbin)
                    got = mp.CreateMulticamClip(items, {"name": name, "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_AUDIO,
                                                        "angleNameMode": resolve.MULTICAM_ANGLE_NAME_CLIP,
                                                        "useFullClipExtents": True, "createBinForSourceClips": False,
                                                        "frameRate": float(P.cfg.get("fps", 23.976))}) or []
                    if got:
                        mcs[name] = got[0]
                if sid in report:
                    continue
            mc = mcs.get(name)
            lens = {}
            for n, f in [("program", P.file(sid))] + [(n, a["file"]) for n, a in P.angles(sid).items()]:
                it = idx.get(key(f))
                lens[n] = int(it.GetClipProperty("Frames")) if it else None
            starts = {"program": 0, **{n: round((a["offset_s"] or 0.0) * P.fps) for n, a in P.angles(sid).items()}}
            lo = min(0, min(starts.values()))
            exp = max(starts[n] + (lens[n] or 0) for n in lens) - lo
            report[sid] = {"mc": bool(mc), "frames": int(mc.GetClipProperty("Frames")) if mc else None, "expected": exp,
                           "out_offset_f": -lo, "check": "frames = expected, else confirm the offset on thumbnails"}
        return report

    if mode == "read_cuts":  # the timeline as it is now (a human moved cuts) -> Cut list on source frames
        name = args.get("name")
        tl = next((t for t in timelines() if t and t.GetName() == name), None)
        if tl is None:
            return {"error": f"no timeline {name!r}"}
        st = tl.GetStartFrame()
        by_name = {mc_name(P, sid): sid for sid in P.sources if mc_name(P, sid)}
        by_file = {key(P.file(sid)): sid for sid in P.sources}
        segs, unknown = [], []
        for it in tl.GetItemListInTrack("audio", 1) or []:
            mpi = it.GetMediaPoolItem()
            nm = mpi.GetClipProperty("Clip Name") if mpi else ""
            fp = mpi.GetClipProperty("File Path") if mpi else ""
            sid = by_name.get(nm) or (by_file.get(key(fp)) if fp else None)
            if sid is None:
                unknown.append(nm or fp)
                continue
            off = mc_offset(P, sid) if nm in by_name else 0
            sf, ef = it.GetSourceStartFrame() - off, it.GetSourceEndFrame() - off  # end exclusive
            segs.append({"src": sid, "in_f": sf, "out_f": ef, "rec_f": it.GetStart() - st, "in": round(sf / P.fps, 6),
                         "out": round(ef / P.fps, 6), "why": "read back from the timeline"})
        cut = {"fps": P.fps, "timeline": name, "sources": {s["src"]: P.file(s["src"]) for s in segs}, "segments": segs,
               "markers": [], "total_f": tl.GetEndFrame() - st}
        if args.get("out"):
            E.write_json(args["out"], cut)
        return {"timeline": name, "segments": len(segs), "unknown": unknown, "total_f": cut["total_f"], "out": args.get("out")}

    cut = E.read_json(args["cuts"])
    name = args.get("name") or cut.get("timeline") or Path(args["cuts"]).stem
    use_mc = lambda sid: bool(mc_name(P, sid)) and mc_name(P, sid) in mcs  # noqa: E731
    pl = plan(P, cut, use_mc)

    def clip_of(c):
        return mcs[c[1]] if c[0] == "mc" else idx.get(key(c[1]))

    if mode == "inspect":
        tl = next((t for t in timelines() if t and t.GetName() == name), None)
        if tl is None:
            return {"error": f"no timeline {name!r}"}
        st = tl.GetStartFrame()
        got = {"V1": [(i.GetStart() - st, i.GetEnd() - st) for i in tl.GetItemListInTrack("video", 1) or []],
               "A1": [(i.GetStart() - st, i.GetEnd() - st) for i in tl.GetItemListInTrack("audio", 1) or []]}
        if pl["tight"]:
            want_v = [(round(x["recordFrame"]), round(x["recordFrame"]) + x["endFrame"] - x["startFrame"]) for x in pl["pic"]]
            want_a = [(round(x["exact"][2]), round(x["exact"][2] + x["exact"][1] - x["exact"][0])) for x in pl["snd"]]
        else:
            want_v = want_a = [(x["recordFrame"], x["recordFrame"] + x["endFrame"] - x["startFrame"]) for x in pl["linked"]]
        dv = [(n, a, b) for n, (a, b) in enumerate(zip(got["V1"], want_v)) if tuple(a) != tuple(b)]
        da = [(n, a, b) for n, (a, b) in enumerate(zip(got["A1"], want_a)) if abs(a[0] - b[0]) > 1 or abs(a[1] - b[1]) > 1]
        same = not dv and not da and len(got["V1"]) == len(want_v) and len(got["A1"]) == len(want_a)
        return {"timeline": name, "same_as_cut_list": same, "V1": len(got["V1"]), "A1": len(got["A1"]),
                "V1_expected": len(want_v), "A1_expected": len(want_a), "V1_diffs": dv[:10], "A1_diffs": da[:10]}

    if mode == "review_markers":
        tl = next((t for t in timelines() if t and t.GetName() == name), None)
        if tl is None:
            return {"error": f"no timeline {name!r}"}

        def add(f, col, nm, note):  # AddMarker fails on an occupied frame -> try +1, -1, +2, -2
            return any(tl.AddMarker(f + d, col, nm, note, 1) for d in (0, 1, -1, 2, -2))
        cnt, fail = {}, []
        for f, col, nm, note in review_plan(cut):
            if add(f, col, nm, note):
                cnt[col] = cnt.get(col, 0) + 1
            else:
                fail.append(f)
        return {"timeline": name, "added": cnt, "failed": fail, "total_markers": len(tl.GetMarkers() or {})}

    if mode != "build":
        return {"error": f"unknown mode {mode!r}"}
    missing = sorted({str(x["clip"][1]) for x in pl["pic"] + pl["linked"] + pl["face"] + pl["cams"] if clip_of(x["clip"]) is None})
    if missing:
        return {"error": "media not in the media pool (run modes import / multicam)", "missing": missing}
    old = [t for t in timelines() if t and t.GetName() == name]
    if old and not args.get("replace"):
        return {"error": f"timeline {name!r} exists", "hint": "pass ARGS name = a new version, or replace = True after "
                "mode inspect shows no hand edits"}
    prev_tl, prev_page = project.GetCurrentTimeline(), resolve.GetCurrentPage()
    prev_name = prev_tl.GetName() if prev_tl is not None else None
    if old:
        mp.DeleteTimelines(old)
    tlbin = sub(top, "Timelines")
    subsbin = sub(tlbin, "Subtitles")
    srt = None
    if pl["srt"] and os.path.isfile(pl["srt"]):
        ex = idx.get(key(pl["srt"]))
        if ex is not None:  # drop and re-import: the SRT may have changed on disk (timelines keep their subtitle items)
            mp.DeleteClips([ex])
        mp.SetCurrentFolder(subsbin)
        got = mp.ImportMedia([pl["srt"]]) or []
        srt = got[0] if got else None
    mp.SetCurrentFolder(tlbin)
    tl = mp.CreateEmptyTimeline(name)
    project.SetCurrentTimeline(tl)
    st = tl.GetStartFrame()
    if srt:  # subtitles FIRST: an appended SRT ignores recordFrame and lands after the last clip
        mp.AppendToTimeline([{"mediaPoolItem": srt}])

    def items(lst, extra=()):
        return [{"mediaPoolItem": clip_of(x["clip"]), **{k: x[k] for k in ("startFrame", "endFrame", "trackIndex") + tuple(extra)},
                 "recordFrame": st + x["recordFrame"]} for x in lst]
    glide = {"joins": 0, "xfade": 0, "fade_in": 0, "fail": []}
    if not pl["tight"]:
        mp.AppendToTimeline(items(pl["linked"]))
        tl.AddTrack("video")
        if pl["face"]:
            mp.AppendToTimeline(items(pl["face"], ("mediaType",)))
    else:
        mp.AppendToTimeline(items(pl["pic"], ("mediaType",)))
        got_snd = mp.AppendToTimeline(items(pl["snd"], ("mediaType",))) or []
        # sub-frame audio edges + 1-frame cross fades need the preference "Align audio edits to frame boundaries" OFF
        if len(got_snd) == len(pl["snd"]):
            ex = [x["exact"] for x in pl["snd"]]
            ends = [st + ar + (ao - ai) for ai, ao, ar in ex]
            starts = [st + ar for ai, ao, ar in ex]
            for n, (it, x) in enumerate(zip(got_snd, pl["snd"])):
                if abs(it.GetStart(True) - starts[n]) > 1e-4:
                    ok = it.SetLeftOffset(it.GetLeftOffset(True) - (it.GetStart(True) - starts[n]), True, False)
                    if not ok or abs(it.GetStart(True) - starts[n]) > 1e-3:
                        glide["fail"].append(("left", n))
                tgt = starts[n + 1] if n + 1 < len(starts) and abs(starts[n + 1] - ends[n]) < 1e-3 else ends[n]
                if abs(it.GetEnd(True) - tgt) > 1e-4:
                    ok = it.SetRightOffset(it.GetRightOffset(True) - (tgt - it.GetEnd(True)), True, False)
                    if not ok or abs(it.GetEnd(True) - tgt) > 1e-3:
                        glide["fail"].append(("right", n))
                if x["fade_in"]:
                    glide["fade_in"] += bool(it.SetFades({"FadeIn": 1}))
            for n, (it, x) in enumerate(zip(got_snd, pl["snd"])):
                if not x["xf"]:
                    continue
                glide["joins"] += 1
                tr = it.AddTransition({"type": "Cross Fade +3 dB", "category": "audio", "position": "end",
                                       "alignment": "center", "duration": 1})
                if tr and abs(tr.GetEnd(True) - it.GetEnd(True)) < 1e-3 and abs(tr.GetStart(True) - (it.GetEnd(True) - 1)) < 1e-3:
                    glide["xfade"] += 1
                else:
                    glide["fail"].append(("xfade", n))
        else:
            glide["fail"].append(("append", len(got_snd), len(pl["snd"])))
        tl.AddTrack("video")
        if pl["face"]:
            mp.AppendToTimeline(items(pl["face"], ("mediaType",)))
        if pl["cams"]:
            tl.AddTrack("video")
            mp.AppendToTimeline(items(pl["cams"], ("mediaType",)))
            tl.SetTrackName("video", 3, "Cameras (delete a piece = back to program / face)")
    any_mc = any(x["clip"][0] == "mc" for x in pl["pic"] + pl["linked"])
    tl.SetTrackName("video", 1, "Multicam (active angle = program)" if any_mc else "Program")
    tl.SetTrackName("video", 2, "Face on hooks/outros (delete if you change the angle)")
    tl.SetTrackName("audio", 1, "Sound (J-cuts: sound before picture)" if pl["tight"] else "Sound")
    for m in pl["markers"]:
        tl.AddMarker(m["frame"], m["color"], m["name"], m.get("note", ""), 1)
    v1 = tl.GetItemListInTrack("video", 1) or []
    a1 = tl.GetItemListInTrack("audio", 1) or []
    report = {"timeline": name, "len": tl.GetEndFrame() - st, "expected": pl["total_f"], "V1": len(v1), "A1": len(a1),
              "V2": len(tl.GetItemListInTrack("video", 2) or []),
              "V3": len(tl.GetItemListInTrack("video", 3) or []) if tl.GetTrackCount("video") >= 3 else 0,
              "plan": summary(pl), "subs": len(tl.GetItemListInTrack("subtitle", 1) or []) if tl.GetTrackCount("subtitle") else 0,
              "markers": len(tl.GetMarkers() or {}), "glide": glide, "replaced": bool(old)}
    back = next((t for t in timelines() if t and prev_name and t.GetName() == prev_name), None)
    if back is not None:
        project.SetCurrentTimeline(back)
    if prev_page:
        resolve.OpenPage(prev_page)
    return report


# ---------------------------------------------------------------- entry points

def _cli():
    import argparse
    E.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--cuts", help="cut list (needed for build / review_markers / inspect)")
    ap.add_argument("--mode", default="build", choices=["import", "multicam", "build", "review_markers", "inspect", "read_cuts"])
    ap.add_argument("--name", help="timeline name (default: the cut list's)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan: items per track, markers, joins")
    ap.add_argument("--snippet", action="store_true", help="print the run_script_unsafe code for this call")
    ap.add_argument("--replace", action="store_true", help="snippet: replace a same-name timeline (inspect first)")
    ap.add_argument("--json", help="dry run: write the full plan here")
    ap.add_argument("--out", help="snippet, read_cuts: where Resolve writes the read-back Cut list")
    a = ap.parse_args()
    if a.snippet:
        args = {"mode": a.mode, "project": str(Path(a.project).resolve()), "cuts": str(Path(a.cuts).resolve()) if a.cuts else None,
                "name": a.name, "replace": a.replace, "out": str(Path(a.out).resolve()) if a.out else None}
        me = str(Path(__file__).resolve())
        print(f"g = dict(globals()); g['ARGS'] = {json.dumps(args, ensure_ascii=False)}; g['__file__'] = {me!r}\n"
              f"exec(open({me!r}, encoding='utf-8').read(), g); result = g['result']")
        return
    P = E.Project(a.project)
    if a.mode in ("import", "multicam"):
        for sid in P.sources:
            files = [P.file(sid)] + [x["file"] for x in P.angles(sid).values()]
            print(f"{sid}: {len(files)} files, multicam {mc_name(P, sid)!r}, program at multicam frame {mc_offset(P, sid)}"
                  + "".join(f"\n   {'ok ' if os.path.isfile(f) else 'MISSING '}{f}" for f in files))
        return
    if not a.cuts:
        ap.error("--cuts is required for this mode")
    cut = E.read_json(a.cuts)
    pl = plan(P, cut, lambda sid: bool(mc_name(P, sid)))
    marks = review_plan(cut) if pl["tight"] else []
    name = a.name or cut.get("timeline")
    print(f"timeline {name!r} (dry run, nothing sent to Resolve)")
    print(json.dumps(summary(pl, marks), ensure_ascii=False))
    for lab, lst in (("V1 picture", pl["pic"] or pl["linked"]), ("A1 sound", pl["snd"]), ("V2 face", pl["face"]), ("V3 cameras", pl["cams"])):
        for x in lst[:3]:
            print(f"  {lab}: {x['clip'][1]!s:.60} src {x['startFrame']}-{x['endFrame']} at rec {x['recordFrame']}")
    for m in pl["markers"][:3]:
        print(f"  marker {m['frame']} {m['color']} {m['name']}")
    if a.json:
        E.write_json(a.json, {"plan": {k: v for k, v in pl.items()}, "review_markers": marks})


if "ARGS" in globals() and "resolve" in globals():
    result = run_in_resolve(resolve, project, ARGS)  # noqa: F821 (injected by the Resolve MCP)
elif __name__ == "__main__":
    _cli()
