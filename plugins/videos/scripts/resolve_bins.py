"""Where things go in a DaVinci Resolve project and what they are called: one layout for every stage, agent session
and human. Runs inside Resolve through Blackmagic's Resolve MCP (`run_script_unsafe`, `timeout` 60). Stdlib only.

    import sys, importlib; sys.path.insert(0, r"<plugin>/scripts")
    import resolve_bins as B; importlib.reload(B)              # reload: Resolve keeps modules between calls
    result = B.claim(resolve, project, "Teleprompter demo 9x16", "rough")   # an agent starts changing it
    result = B.release(resolve, project, "Teleprompter demo 9x16")          # done, the human signs off
    result = B.audit(project)                                               # what breaks the layout

Layout (paths from the media-pool root; the numbers keep the order in every sort mode):
  01 Timelines/WIP/Latest    working timelines nobody is changing right now, name = the video + its format
                             ("Teleprompter demo 9x16", "Teleprompter demo 16x9"); a short cut from the long vertical =
                             "<Video> 9x16 S01 <topic>"; a variant waiting for a choice = "<Video> 9x16 - <variant>".
                             tl_name() builds these.
  01 Timelines/WIP/Processing  the working timelines an agent is changing right now: hands off (claim()).
  01 Timelines/WIP/Archive   history: "<working name> vNN YYYY-MM-DD <state>" ("Teleprompter demo 9x16 v02 2026-03-04
                             rough"). NN counts up per working timeline and is never reused.
  01 Timelines/Waiting for review  working timelines an agent finished and the human must sign off (release()).
  01 Timelines/Published     approved cuts, same name pattern with state "approved" (older projects: "Final").
  01 Timelines/Scratch       tests and throwaways.
  02 Footage/<YYYY-MM-DD place or subject>/[<concept>/]<take or source>/    camera and audio files as shot
  02 Footage/<shoot>/Multicam                                               that shoot's multicam clips
  03 Assets/Images · Videos/<Video>[/<set>] · Music · SFX · Voice/<Video> · Subtitles/<Video>
                             made or bought material; Voice = cleaned, mastered or generated voice tracks.
  04 Exports/<Video>         the final rendered files of one video, checked and approved: its 9x16, its 16x9, its
                             shorts (file_export()). One item per deliverable; a re-render replaces the file in place.

Rules:
  - Camera and audio clips keep their FILE name as clip name. The camera goes in the clip's `Angle` metadata.
  - The bin of a working timeline is its status: Latest (idle), Processing (an agent is changing it), Waiting for
    review (an agent finished, the human signs off). Its name never changes with the status.
  - An agent starts changing a working timeline: claim() = snapshot() into the archive, then the timeline itself moves
    to Processing. Done: release() moves it to Waiting for review (or back to Latest when no sign-off is needed).
  - snapshot(): the COPY goes to the archive under a versioned name; the working timeline keeps its name and stays the
    same timeline, so every pointer to it (records, scripts, render jobs, the human's open tab) stays right.
  - Keywords on every item (Resolve shows one smart bin per keyword): the video name, the shoot, the kind (timeline /
    latest / archive / rough / tight / camera / audio track / multicam / graphic / voice / subtitle …); timelines also
    carry their format (9x16 / 16x9 / 4x3) and shorts "short".
  - The format in a timeline's name matches its resolution (audit checks it). A video is cut as the long 9x16 first;
    the 16x9 and the shorts are made from the approved 9x16 (references/resolve.md § Organization).
  - Nothing at the pool root, no top-level bin besides the four numbered ones, no "/" in a bin name.
  - Moves go through move(): `MoveClips` / `MoveFolders` COPY when Resolve's window believes Alt is down.
"""
import os
import re
import time

TIMELINES, FOOTAGE, ASSETS, EXPORTS = "01 Timelines", "02 Footage", "03 Assets", "04 Exports"
LATEST, PROCESSING, ARCHIVE = TIMELINES + "/WIP/Latest", TIMELINES + "/WIP/Processing", TIMELINES + "/WIP/Archive"
REVIEW, PUBLISHED, SCRATCH = TIMELINES + "/Waiting for review", TIMELINES + "/Published", TIMELINES + "/Scratch"
FINAL = PUBLISHED                    # the approved-cuts bin was called "Final" before; to=B.FINAL now files in Published
OLD_FINAL = TIMELINES + "/Final"     # still read (versions, audit) in projects that have it; audit asks to empty it
WORKING = {"latest": LATEST, "processing": PROCESSING, "review": REVIEW}  # status -> bin; working names only
ASSET_KINDS = ("Images", "Videos", "Music", "SFX", "Voice", "Subtitles")
VERSION = re.compile(r"^(?P<video>.+) v(?P<n>\d{2,3}) (?P<date>\d{4}-\d{2}-\d{2})(?: (?P<state>.+))?$")
# "latest" = the working version of a timeline (whichever status bin holds it), against "archive" / "published" copies
PLACE_WORDS = ("timeline", "latest", "archive", "published", "final", "scratch")
STATES = ("rough", "tight", "graded", "mastered", "approved")  # read back from a timeline's keywords; others: pass state
# Timeline size per format. 9x16 = the long vertical every video is cut as first, and its shorts; 16x9 is made from the
# approved 9x16 (a 16x9 project's own UHD or HD size is kept); 4x3 only when a platform asks for it.
FORMATS = {"9x16": (1080, 1920), "16x9": (3840, 2160), "4x3": (1440, 1080)}
FORMAT_WORDS = tuple(FORMATS)
# "<video> <fmt>[ S<nn>[ <label>]][ - <variant>]" (the version suffix is stripped first, VERSION)
NAME = re.compile(r"^(?P<video>.+?) (?P<fmt>9x16|16x9|4x3)(?: S(?P<short>\d{2,3})(?: (?P<label>.+?))?)?(?: - (?P<variant>.+))?$")


def clean(name):
    """A bin or timeline name Resolve takes: no path separators (a "/" in a bin name breaks every path lookup) and none
    of ? * " < > | (AddSubFolder refuses them, like Windows file names): "Ready? Go" -> "Ready - Go"."""
    return re.sub(r'\s*[\\/:?*"<>|]+\s*', " - ", str(name)).strip(" -")


def tl_name(video, fmt, short=None, label=None, variant=None):
    """Working timeline name: tl_name("Teleprompter demo", "9x16") -> "Teleprompter demo 9x16";
    tl_name("Teleprompter demo", "9x16", 1, "price objection") -> "Teleprompter demo 9x16 S01 price objection"."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)} (got {fmt!r})")
    s = f"{clean(video)} {fmt}"
    if short is not None:
        s += f" S{int(short):02d}" + (f" {clean(label)}" if label else "")
    return s + (f" - {clean(variant)}" if variant else "")


def parse(name):
    """What a timeline name says: video, fmt, short, label, variant, and version / date / state for an archived one.
    A name from before formats were named (no format token) gives fmt None and the whole base as the video."""
    out = {"name": name, "version": None, "date": None, "state": None}
    base = name or ""
    m = VERSION.match(base)
    if m:
        base = m.group("video")
        out.update(version=int(m.group("n")), date=m.group("date"), state=m.group("state"))
    n = NAME.match(base)
    if n:
        out.update(base=base, video=n.group("video"), fmt=n.group("fmt"),
                   short=int(n.group("short")) if n.group("short") else None, label=n.group("label"), variant=n.group("variant"))
    else:
        video, _, variant = base.partition(" - ")
        out.update(base=base, video=video, fmt=None, short=None, label=None, variant=variant or None)
    return out


def with_format(name, fmt):
    """`name` with its format token set to `fmt` (added when missing, replaced when another): a cut list read back from
    "Demo 9x16" builds "Demo 16x9"."""
    p = parse(name)
    if p["fmt"] == fmt:
        return name
    return tl_name(p["video"], fmt, p["short"], p["label"], p["variant"])


def fmt_of(tl, project=None):
    """The format a timeline's resolution makes (9x16 / 16x9 / 4x3), or None when it is none of them."""
    def num(src, key):
        try:
            return int(float(src.GetSetting(key) or 0))
        except Exception:
            return 0
    w, h = num(tl, "timelineResolutionWidth"), num(tl, "timelineResolutionHeight")
    if not (w and h) and project is not None:
        w, h = num(project, "timelineResolutionWidth"), num(project, "timelineResolutionHeight")
    if not (w and h):
        return None
    r = w / h
    for fmt, (fw, fh) in FORMATS.items():
        if abs(r - fw / fh) < 0.02:
            return fmt
    return None


def timeline_words(name):
    """Keywords a timeline carries besides its place and state: the video, its format, "short" for a short."""
    p = parse(name)
    return [p["video"], p["fmt"], "short" if p["short"] is not None else None]


def find_bin(mp, path):
    f = mp.GetRootFolder()
    for part in [p for p in path.split("/") if p]:
        f = next((s for s in (f.GetSubFolderList() or []) if s.GetName() == part), None)
        if f is None:
            return None
    return f


def path_bin(mp, path):
    """The bin at `path`, created when missing. AddSubFolder accepts duplicate names and makes the new bin current:
    look up by name first, and restore the current folder after."""
    f = mp.GetRootFolder()
    cur = mp.GetCurrentFolder()
    made = False
    for part in [p for p in path.split("/") if p]:
        nxt = next((s for s in (f.GetSubFolderList() or []) if s.GetName() == part), None)
        if nxt is None:
            nxt = mp.AddSubFolder(f, part)
            made = True
            if not nxt:
                raise RuntimeError(f"AddSubFolder failed: {path!r} at {part!r}")
        f = nxt
    if made and cur:
        mp.SetCurrentFolder(cur)
    return f


def walk(folder, path=""):
    """Every item under `folder`: (item, its folder, the folder's path)."""
    for c in folder.GetClipList() or []:
        yield c, folder, path
    for s in folder.GetSubFolderList() or []:
        yield from walk(s, (path + "/" if path else "") + s.GetName())


def held_modifiers(resolve):
    """Modifier keys Resolve's window believes are down. A stale Alt (left by an Alt+Tab away from Resolve) turns
    every MoveClips / MoveFolders into a duplicate, like Alt-dragging in the media pool, and still returns True.
    Clear it with one click or mouse move inside the Resolve window."""
    try:
        m = resolve.Fusion().GetModifiers(True) or {}
    except Exception:
        return []
    return sorted(k for k, v in m.items() if v)


def move(resolve, project, items, path):
    """MoveClips that proves it moved: refuses while a modifier key is held, then checks the same ids sit in the
    target. Returns {"moved": n} or {"error": …}."""
    items = [i for i in items if i]
    if not items:
        return {"moved": 0, "bin": path}
    held = held_modifiers(resolve)
    if held:
        return {"error": "a modifier key is held in Resolve's window: a move would copy", "held": held,
                "fix": "one click or mouse move inside the Resolve window, then retry"}
    mp = project.GetMediaPool()
    cur = mp.GetCurrentFolder()
    target = path_bin(mp, path)
    there = {c.GetUniqueId() for c in target.GetClipList() or []}
    todo = [i for i in items if i.GetUniqueId() not in there]
    ok = mp.MoveClips(todo, target) if todo else True
    there = {c.GetUniqueId() for c in target.GetClipList() or []}
    if cur:
        mp.SetCurrentFolder(cur)
    if not ok:
        return {"error": "MoveClips returned False", "bin": path}
    lost = [i for i in items if i.GetUniqueId() not in there]
    if lost:
        return {"error": "MoveClips copied instead of moving: the target holds copies with new ids, the originals "
                         "stayed. Delete the copies, clear the stuck modifier key, retry", "bin": path, "not_moved": len(lost)}
    return {"moved": len(todo), "bin": path}


def keywords(item):
    return [k.strip() for k in (item.GetMetadata("Keywords") or "").split(",") if k.strip()]


def tag(item, *words, drop=()):
    """Add keywords (kept: the ones already there, minus `drop`). Subtitle items refuse keywords: returns False."""
    have = [k for k in keywords(item) if k not in drop]
    new = have + [w for w in dict.fromkeys(str(w).strip() for w in words if w) if w not in have]
    if new == keywords(item):
        return True
    return bool(item.SetMetadata("Keywords", ", ".join(new)))  # one comma-separated string


def timeline(project, name):
    for i in range(1, project.GetTimelineCount() + 1):
        t = project.GetTimelineByIndex(i)
        if t and t.GetName() == name:
            return t
    return None


def versions(project, video):
    """Version numbers already used for this working timeline name, sorted (archive and final names). Versions
    archived before the name carried a format ("Demo v04 …" for "Demo 16x9") count too, so numbers keep going up."""
    legacy = parse(video)["video"] if parse(video)["fmt"] else None
    out = []
    for i in range(1, project.GetTimelineCount() + 1):
        t = project.GetTimelineByIndex(i)
        m = VERSION.match(t.GetName() or "") if t else None
        if m and (m.group("video") == video or (legacy and m.group("video") == legacy)):
            out.append(int(m.group("n")))
    return sorted(out)


def _state(item, video):
    return next((k for k in reversed(keywords(item)) if k in STATES), "") if item else ""


def _versioned(project, video, state, date):
    n = (versions(project, video) or [0])[-1] + 1
    return n, f"{video} v{n:02d} {date or time.strftime('%Y-%m-%d')}" + (f" {state}" if state else "")


def snapshot(resolve, project, name, state=None, to=ARCHIVE, date=None):
    """Before a big edit: a copy of the working timeline `name` is filed in `to` as "<name> vNN YYYY-MM-DD <state>".
    The working timeline keeps its name and place; the current timeline and folder are restored."""
    tl = timeline(project, name)
    if tl is None:
        return {"error": f"no timeline {name!r}"}
    held = held_modifiers(resolve)
    if held:
        return {"error": "a modifier key is held in Resolve's window: filing the copy would duplicate it", "held": held,
                "fix": "one click or mouse move inside the Resolve window, then retry"}
    mp = project.GetMediaPool()
    cur_tl, cur_folder = project.GetCurrentTimeline(), mp.GetCurrentFolder()
    if state is None:
        state = _state(tl.GetMediaPoolItem(), name)
    n, new = _versioned(project, name, state, date)
    try:
        copy = tl.DuplicateTimeline(new)  # becomes the current timeline
        if not copy:
            return {"error": "DuplicateTimeline failed", "name": new}
        item = copy.GetMediaPoolItem()
        r = move(resolve, project, [item], to)
        if "error" in r:
            return {**r, "snapshot": new, "note": "the copy exists and was not filed"}
        tag(item, *timeline_words(name), "timeline", to.rsplit("/", 1)[-1].lower(), state,
            drop=PLACE_WORDS + STATES + FORMAT_WORDS)
    finally:
        if cur_tl:
            project.SetCurrentTimeline(cur_tl)
        if cur_folder:
            mp.SetCurrentFolder(cur_folder)
    return {"snapshot": new, "version": n, "bin": to, "working": name}


def archive(resolve, project, name, state=None, date=None):
    """A rebuild replaces the working timeline: the existing one itself moves to the archive under a versioned name
    (nothing is deleted), which frees the plain name for the new build."""
    tl = timeline(project, name)
    if tl is None:
        return {"error": f"no timeline {name!r}"}
    cur = project.GetCurrentTimeline()  # the human's current timeline stays theirs, whatever the calls below do
    try:
        item = tl.GetMediaPoolItem()
        if state is None:
            state = _state(item, name)
        n, new = _versioned(project, name, state, date)
        r = move(resolve, project, [item], ARCHIVE)
        if "error" in r:
            return r
        tl.SetName(new)
        if tl.GetName() != new:
            return {"error": "moved to the archive, rename failed", "name": new}
        tag(item, *timeline_words(name), "timeline", "archive", state, drop=PLACE_WORDS + STATES + FORMAT_WORDS)
        return {"archived": new, "version": n, "bin": ARCHIVE}
    finally:
        if cur:
            project.SetCurrentTimeline(cur)


def where(project, name):
    """Path of the bin that holds timeline `name` ("01 Timelines/WIP/Latest"), or None."""
    tl = timeline(project, name)
    item = tl.GetMediaPoolItem() if tl else None
    if item is None:
        return None
    uid = item.GetUniqueId()
    return next((path for c, _f, path in walk(project.GetMediaPool().GetRootFolder()) if c.GetUniqueId() == uid), None)


def status(project, name):
    """Status of a working timeline from its bin: "latest", "processing" or "review"; None anywhere else."""
    path = where(project, name)
    return next((k for k, v in WORKING.items() if v == path), None)


def set_status(resolve, project, name, to):
    """Move working timeline `name` to the bin of status `to` (latest / processing / review). Same timeline, same
    name: the record, render jobs and the human's open tab keep pointing at it."""
    if to not in WORKING:
        return {"error": f"status must be one of {', '.join(WORKING)} (got {to!r})"}
    tl = timeline(project, name)
    if tl is None:
        return {"error": f"no timeline {name!r}"}
    if VERSION.match(name):
        return {"error": f"{name!r} is a frozen version: archived and published copies stay where they are"}
    was = where(project, name)
    r = move(resolve, project, [tl.GetMediaPoolItem()], WORKING[to])
    return {**r, "timeline": name, "status": to, "was": was}


def claim(resolve, project, name, state=None, snapshot_first=True, take_over=False):
    """An agent starts changing working timeline `name`: a snapshot into the archive (snapshot_first=False for one
    marker or one cut), then the timeline itself moves to WIP/Processing, which tells the human and the other sessions
    that someone is on it. Already in Processing -> error unless take_over=True (another session holds it, or one
    stopped mid-edit: find out first)."""
    if timeline(project, name) is None:
        return {"error": f"no timeline {name!r}"}
    if status(project, name) == "processing" and not take_over:
        return {"error": f"{name!r} is in Processing: another agent is changing it, or one stopped mid-edit",
                "fix": "check the video's record and the other sessions, ask the human; take_over=True once it is yours"}
    snap = snapshot(resolve, project, name, state) if snapshot_first else None
    if snap and "error" in snap:
        return {"error": "snapshot failed, timeline not claimed", "snapshot": snap}
    return {**set_status(resolve, project, name, "processing"), "snapshot": snap["snapshot"] if snap else None}


def release(resolve, project, name, review=True):
    """The agent is done with `name`: to Waiting for review when the human must sign off (default), else back to
    WIP/Latest. After the human's verdict: approved -> release(review=False) (and a snapshot to PUBLISHED where the
    stage freezes an approval), changes asked -> claim() again."""
    return set_status(resolve, project, name, "review" if review else "latest")


def file_export(resolve, project, path, timeline_name):
    """A final rendered file (checked, approved) goes in 04 Exports/<Video>, tagged with the video, `export`, its
    format and `short`. The item's Description holds the timeline it was rendered from: a re-render of that timeline
    (a new file, since Resolve holds the old one open) replaces the item's file in place, so the bin keeps one item per
    deliverable. A file already in the pool elsewhere is moved there. Returns {"filed": name, "bin": …} or {"error": …}."""
    p = parse(timeline_name)
    if p["version"] or not p["fmt"]:
        return {"error": f"{timeline_name!r}: pass the working timeline's name (video + format, no version)"}
    if not os.path.isfile(path):
        return {"error": f"no file {path!r}"}
    mp = project.GetMediaPool()
    target = EXPORTS + "/" + clean(p["video"])
    cur = mp.GetCurrentFolder()
    norm = os.path.normcase(os.path.abspath(path))
    try:
        b = path_bin(mp, target)
        same_file = next((c for c, _f, _p in walk(mp.GetRootFolder())
                          if os.path.normcase(os.path.abspath(c.GetClipProperty("File Path") or "")) == norm), None)
        prev = next((c for c in b.GetClipList() or [] if c.GetMetadata("Description") == timeline_name), None)
        if same_file is not None:
            item, how = same_file, "already in the pool"
            if same_file.GetUniqueId() not in {c.GetUniqueId() for c in b.GetClipList() or []}:
                r = move(resolve, project, [same_file], target)
                if "error" in r:
                    return r
                how = "moved"
        elif prev is not None:
            if not prev.ReplaceClip(path):
                return {"error": "ReplaceClip failed", "item": prev.GetName(), "file": path}
            item, how = prev, "replaced"
        else:
            mp.SetCurrentFolder(b)
            got = mp.ImportMedia([path]) or []
            if not got:
                return {"error": "ImportMedia returned nothing (Deliver page or a render running?)", "file": path}
            item, how = got[0], "imported"
        item.SetMetadata("Description", timeline_name)  # ReplaceClip resets metadata: set it again every time
        tag(item, *timeline_words(timeline_name), "export")
        return {"filed": item.GetName(), "how": how, "bin": target, "from": timeline_name,
                "description_ok": item.GetMetadata("Description") == timeline_name}
    finally:
        if cur:
            mp.SetCurrentFolder(cur)


def add_format(project, name):
    """A working timeline named before formats were named ("Demo", "Demo - panels") gets the format its resolution
    makes ("Demo 16x9", "Demo 16x9 - panels") and the matching keywords. Archived names stay as they are (history).
    Update the record's `resolve:` after."""
    tl = timeline(project, name)
    if tl is None:
        return {"error": f"no timeline {name!r}"}
    p = parse(name)
    if p["fmt"] or p["version"]:
        return {"name": name, "unchanged": "already named with a format" if p["fmt"] else "archived name"}
    fmt = fmt_of(tl, project)
    if not fmt:
        return {"error": f"{name!r}: resolution matches no format", "formats": FORMATS}
    new = tl_name(p["video"], fmt, variant=p["variant"])
    if timeline(project, new) is not None:
        return {"error": f"a timeline {new!r} exists already"}
    tl.SetName(new)
    if tl.GetName() != new:
        return {"error": "rename failed", "name": name}
    item = tl.GetMediaPoolItem()
    if item:
        tag(item, *timeline_words(new), drop=FORMAT_WORDS)
    return {"renamed": name, "to": new, "fmt": fmt}


def file_name(item):
    p = item.GetClipProperty("File Path") or ""
    return os.path.basename(p.replace("\\", "/"))


def restore_names(items):
    """Clip name = file name again. Returns the names that could not be restored."""
    bad = []
    for it in items:
        nm = file_name(it)
        if nm and it.GetName() != nm:
            it.SetClipProperty("Clip Name", nm)  # returns False and renames anyway: read back
            if it.GetName() != nm:
                bad.append(nm)
    return bad


def multicam(resolve, project, ordered, name, shoot_path, fps, label=""):
    """ordered = [(angle name, MediaPoolItem), …] in angle order; the clip lands in <shoot_path>/Multicam.
    Resolve orders angles by clip NAME and keeps those names inside the multicam clip (timeline items read
    "<multicam> - 2 <label> <angle>" from then on), so the clips carry "N <label> <angle>" only while the multicam
    is made, then get their file names back. Returns (multicam clip or None, names not restored)."""
    mp = project.GetMediaPool()
    cur = mp.GetCurrentFolder()
    target = path_bin(mp, shoot_path + "/Multicam")
    items = [it for _a, it in ordered]
    got = None
    try:
        for k, (angle, it) in enumerate(ordered, 1):
            if not it.GetMetadata("Angle"):  # a camera name someone already wrote stays
                it.SetMetadata("Angle", str(angle))
            it.SetClipProperty("Clip Name", " ".join(x for x in (str(k), str(label), str(angle)) if x))
        mp.SetCurrentFolder(target)
        made = mp.CreateMulticamClip(items, {"name": name, "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_AUDIO,
                                             "angleNameMode": resolve.MULTICAM_ANGLE_NAME_CLIP,
                                             "useFullClipExtents": True, "createBinForSourceClips": False,
                                             "frameRate": float(fps)}) or []
        got = made[0] if made else None
    finally:
        bad = restore_names(items)
        if cur:
            mp.SetCurrentFolder(cur)
    return got, bad


def audit(project, sample=10):
    """What breaks the layout, as counts and samples. `ok` True = nothing to fix."""
    mp = project.GetMediaPool()
    root = mp.GetRootFolder()
    found = {"stray_top_bins": [s.GetName() for s in root.GetSubFolderList() or [] if s.GetName() not in (TIMELINES, FOOTAGE, ASSETS, EXPORTS)],
             "items_at_root": [c.GetName() for c in root.GetClipList() or []],
             "bin_names_with_slash": [], "timelines_outside_01": [], "versioned_name_in_latest": [],
             "archive_name_off_pattern": [], "latest_without_format": [], "format_not_resolution": [],
             "in_processing": [], "old_final_bin": [], "footage_clip_renamed": [], "without_keywords": []}
    working = tuple(WORKING.values())
    tls = {}
    for i in range(1, project.GetTimelineCount() + 1):
        t = project.GetTimelineByIndex(i)
        if t:
            tls[t.GetName()] = t

    def bins(f):
        for s in f.GetSubFolderList() or []:
            if "/" in s.GetName():
                found["bin_names_with_slash"].append(s.GetName())
            bins(s)
    bins(root)
    n = 0
    for c, _f, path in walk(root):
        n += 1
        kind, name = c.GetClipProperty("Type"), c.GetName()
        if kind == "Timeline":
            if path == OLD_FINAL:
                found["old_final_bin"].append(name)  # fix: move(resolve, project, items, PUBLISHED), then delete the bin
            if not path.startswith(TIMELINES):
                found["timelines_outside_01"].append(f"{path}/{name}")
            elif path in working and VERSION.match(name):
                found["versioned_name_in_latest"].append(f"{path}/{name}")
            elif path in (ARCHIVE, PUBLISHED, OLD_FINAL) and not VERSION.match(name):
                found["archive_name_off_pattern"].append(name)
            elif path in working and not parse(name)["fmt"]:
                found["latest_without_format"].append(name)  # fix: add_format(project, name)
            if path == PROCESSING:
                found["in_processing"].append(name)  # yours -> release(); another session's -> leave it, say so
            fmt, tl = parse(name)["fmt"], tls.get(name)
            if fmt and tl is not None and fmt_of(tl, project) not in (None, fmt):
                found["format_not_resolution"].append(f"{name} (resolution makes {fmt_of(tl, project)})")
        elif path.startswith(FOOTAGE) and kind not in ("Subtitle", "Multicam", "Compound"):
            fn = file_name(c)
            if fn and name.lower() not in (fn.lower(), os.path.splitext(fn)[0].lower()):
                found["footage_clip_renamed"].append(f"{name} (file {fn})")
        if kind != "Subtitle" and not c.GetMetadata("Keywords"):
            found["without_keywords"].append(f"{path}/{name}")
    out = {"items": n, "ok": not any(found.values())}
    for k, v in found.items():
        out[k] = {"count": len(v), "sample": v[:sample]} if v else 0
    return out
