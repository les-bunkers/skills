"""Resolve side of the audio cleanup (cleanup.py). Runs INSIDE DaVinci Resolve Studio through the Resolve MCP tool
`run_script_unsafe` (it injects `resolve` and `project`; 60 s cap per call: one timeline per call for `place` on
long timelines). Stdlib only. Set globals, then exec this file:

    ACTION = "plan"                 # or "place"
    JOB = r"<job folder>"           # plan.json / manifest.json live there (forward slashes work on Windows)
    TIMELINES = ["My timeline"]     # exact names or substrings; plan: required; place: default = all in plan.json
    TRACK = 1                       # plan: audio track that carries the dialogue (default 1)
    ENABLE = "none"                 # place: "none" = existing toggles untouched, new tracks off (default);
                                    #   "master" = master on, every other audio track off
    SPLIT_TRACKS = False            # place: also add one RE-USE track per speaker for split sources
    exec(open(r"<plugin>/scripts/resolve_cleanup.py", encoding="utf-8").read())

plan  -> <JOB>/plan.json: per timeline, every item of TRACK: source file, record position, source in/out (clip
         frames, exclusive out) at the clip's fps. Read-only in Resolve. Multicam / compound / generated items
         have no file path: listed under "skipped" (use a timeline built from the plain files, or decompose the
         multicam first). Re-planning a timeline replaces only that timeline in plan.json.
place -> adds "RE-USE" and "MASTER −14 LUFS" (name from manifest.json) stereo audio tracks mirroring the TRACK
         items of the plan, from the manifest's mov wrappers, media in bin "Audio cleanup — <job folder name>".
         Idempotent: deletes earlier tracks with those names first. Restores the open timeline and page. Returns
         per timeline position_mismatches (must be 0) and max_source_time_error_s (must be 0.0).
"""
import datetime
import json
import os

ACTION = globals().get("ACTION", "plan")
JOB = globals()["JOB"]
TIMELINES = globals().get("TIMELINES")
TRACK = int(globals().get("TRACK", 1))
ENABLE = globals().get("ENABLE", "none")
SPLIT_TRACKS = bool(globals().get("SPLIT_TRACKS", False))
RE_NAME, SPK_PREFIX = "RE-USE", "RE-USE — "
EXACT = {23.976: 24000 / 1001, 29.97: 30000 / 1001, 47.952: 48000 / 1001, 59.94: 60000 / 1001, 119.88: 120000 / 1001}


def fps_of(v):
    f = float(v)
    return EXACT.get(round(f, 3), f)


def all_timelines():
    return [project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)]  # noqa: F821


def pick(names):
    tls = all_timelines()
    out = []
    for want in names:
        exact = [t for t in tls if t.GetName() == want]
        hit = exact or [t for t in tls if want in t.GetName()]
        assert hit, f"no timeline matches {want!r}"
        out += [t for t in hit if t not in out]
    return out


def plan():
    assert TIMELINES, "plan needs TIMELINES"
    rows = []
    for tl in pick(TIMELINES):
        st, tfps = tl.GetStartFrame(), fps_of(tl.GetSetting("timelineFrameRate"))
        items, skipped = [], []
        for i, it in enumerate(tl.GetItemListInTrack("audio", TRACK) or []):
            m = it.GetMediaPoolItem()
            path = m.GetClipProperty("File Path") if m else ""
            if not path:
                skipped.append({"index": i, "name": it.GetName(), "type": m.GetClipProperty("Type") if m else None,
                                "reason": "no file path (multicam / compound / generated)"})
                continue
            cfps = fps_of(m.GetClipProperty("FPS") or tfps)
            dur = it.GetDuration()
            src_in = int(it.GetSourceStartFrame())
            src_out = src_in + int(round(dur * cfps / tfps))
            items.append({"path": path, "rec_f": it.GetStart() - st, "dur_f": dur, "src_in_f": src_in,
                          "src_out_f": src_out, "clip_fps": cfps, "src_in_s": src_in / cfps, "src_out_s": src_out / cfps,
                          "api_source_start_s": it.GetSourceStartTime(), "api_source_end_s": it.GetSourceEndTime()})
        rows.append({"name": tl.GetName(), "fps": tfps, "start_frame": st, "track": TRACK, "items": items,
                     "skipped": skipped})
    doc = {"project": project.GetName(), "made": datetime.datetime.now().isoformat(timespec="seconds"),  # noqa: F821
           "timelines": rows}
    os.makedirs(JOB, exist_ok=True)
    p = os.path.join(JOB, "plan.json")
    if os.path.exists(p):  # merge: re-planning a timeline replaces only that timeline
        with open(p, encoding="utf-8") as f:
            old = json.load(f)
        keep = [t for t in old["timelines"] if t["name"] not in {r["name"] for r in rows}]
        doc["timelines"] = keep + rows
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    return {r["name"]: {"items": len(r["items"]), "skipped": len(r["skipped"]), "fps": r["fps"],
                        "sources": len({x["path"] for x in r["items"]}),
                        "used_s": round(sum(x["src_out_s"] - x["src_in_s"] for x in r["items"]), 1)} for r in rows}


def sub(mp, parent, name):
    for f in parent.GetSubFolderList() or []:
        if f.GetName() == name:
            return f
    return mp.AddSubFolder(parent, name)


def ours(n, master_name):
    return n in (RE_NAME, master_name) or n.startswith(SPK_PREFIX) or (n.startswith("MASTER ") and n.endswith(" LUFS"))


def place():
    with open(os.path.join(JOB, "plan.json"), encoding="utf-8") as f:
        plan_doc = json.load(f)
    with open(os.path.join(JOB, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    master_name = man.get("master_track", "MASTER −14 LUFS")
    by_path = {os.path.normcase(s["path"]): s for s in man["sources"]}
    names = [t["name"] for t in plan_doc["timelines"]]
    tls = {t.GetName(): t for t in (pick(TIMELINES) if TIMELINES else pick(names))}
    prev_tl, prev_page = project.GetCurrentTimeline(), resolve.GetCurrentPage()  # noqa: F821
    resolve.OpenPage("edit")  # noqa: F821  (Deliver page swallows track toggles)
    mp = project.GetMediaPool()  # noqa: F821
    folder = sub(mp, mp.GetRootFolder(), "Audio cleanup — " + os.path.basename(JOB.rstrip("\\/")))
    have = {os.path.normcase(c.GetClipProperty("File Path")): c for c in (folder.GetClipList() or [])}

    def media(path):
        k = os.path.normcase(path)
        if k not in have:
            mp.SetCurrentFolder(folder)
            have[k] = (mp.ImportMedia([path]) or [None])[0]  # plain path strings: the dict form imports nothing
        return have[k]

    report = {}
    for t in plan_doc["timelines"]:
        if t["name"] not in tls:
            continue
        tl = tls[t["name"]]
        project.SetCurrentTimeline(tl)  # noqa: F821
        st = tl.GetStartFrame()
        for i in range(tl.GetTrackCount("audio"), 0, -1):
            if ours(tl.GetTrackName("audio", i) or "", master_name):
                tl.DeleteTrack("audio", i)
        want = [(RE_NAME, "reuse"), (master_name, "master")]
        if SPLIT_TRACKS:
            spk = sorted({sp for x in t["items"] for sp in by_path[os.path.normcase(x["path"])].get("speakers", [])})
            want += [(SPK_PREFIX + s, "speaker:" + s) for s in spk]
        new = {}
        for label, kind in want:
            tl.AddTrack("audio", "stereo")
            idx = tl.GetTrackCount("audio")
            clips = []
            for x in t["items"]:
                outs = by_path[os.path.normcase(x["path"])]["outputs"]
                path = (outs.get("reuse") if kind == "reuse" else outs.get("master") if kind == "master"
                        else outs.get("speakers", {}).get(kind.split(":", 1)[1])) if outs else None
                if not path:
                    continue
                clips.append({"mediaPoolItem": media(path["resolve_media"]), "startFrame": x["src_in_f"],
                              "endFrame": x["src_out_f"], "recordFrame": st + x["rec_f"], "trackIndex": idx,
                              "mediaType": 2})
            if clips:
                mp.AppendToTimeline(clips)
            tl.SetTrackName("audio", idx, label)
            new[label] = idx
        ref = tl.GetItemListInTrack("audio", t["track"]) or []
        ref_at = {(r.GetStart(), r.GetEnd()): r for r in ref}
        mism, time_err = {}, {}
        for label, idx in new.items():
            got = tl.GetItemListInTrack("audio", idx) or []
            mism[label] = len([g for g in got if (g.GetStart(), g.GetEnd()) not in ref_at])
            # same source second as the original item = the wrapper's frame grid was read at the right fps
            errs = [abs(g.GetSourceStartTime() - ref_at[(g.GetStart(), g.GetEnd())].GetSourceStartTime())
                    for g in got if (g.GetStart(), g.GetEnd()) in ref_at]
            time_err[label] = round(max(errs), 6) if errs else None
        if ENABLE == "master":
            for i in range(1, tl.GetTrackCount("audio") + 1):
                tl.SetTrackEnable("audio", i, i == new[master_name])
        else:
            for idx in new.values():
                tl.SetTrackEnable("audio", idx, False)
        report[t["name"]] = {"tracks": new, "items": {lb: len(tl.GetItemListInTrack("audio", i) or [])
                                                      for lb, i in new.items()},
                             "reference_items": len(ref), "position_mismatches": mism,
                             "max_source_time_error_s": time_err}
    if prev_tl:
        project.SetCurrentTimeline(prev_tl)  # noqa: F821
    if prev_page:
        resolve.OpenPage(prev_page)  # noqa: F821
    report["restored"] = {"timeline": prev_tl.GetName() if prev_tl else None, "page": prev_page}
    return report


result = plan() if ACTION == "plan" else place()
