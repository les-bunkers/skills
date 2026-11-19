"""Where things go in a DaVinci Resolve project and what they are called: one layout for every stage, agent session
and human. Runs inside Resolve through Blackmagic's Resolve MCP (`run_script_unsafe`, `timeout` 60). Stdlib only.

    import sys, importlib; sys.path.insert(0, r"<plugin>/scripts")
    import resolve_bins as B; importlib.reload(B)              # reload: Resolve keeps modules between calls
    result = B.snapshot(resolve, project, "Teleprompter demo", "rough")    # before a big edit
    result = B.audit(project)                                              # what breaks the layout

Layout (paths from the media-pool root; the numbers keep the order in every sort mode):
  01 Timelines/WIP/Latest    working timelines, name = the video + its format ("Teleprompter demo 9x16",
                             "Teleprompter demo 16x9"); a short cut from the long vertical = "<Video> 9x16 S01 <topic>";
                             a variant waiting for a choice = "<Video> 9x16 - <variant>". tl_name() builds these.
  01 Timelines/WIP/Archive   history: "<working name> vNN YYYY-MM-DD <state>" ("Teleprompter demo 9x16 v02 2026-03-04
                             rough"). NN counts up per working timeline and is never reused.
  01 Timelines/Final         approved cuts, same name pattern with state "approved" (made on the first approval).
  01 Timelines/Scratch       tests and throwaways.
  02 Footage/<YYYY-MM-DD place or subject>/[<concept>/]<take or source>/    camera and audio files as shot
  02 Footage/<shoot>/Multicam                                               that shoot's multicam clips
  03 Assets/Images · Videos/<Video>[/<set>] · Music · SFX · Voice/<Video> · Subtitles/<Video>
                             made or bought material; Voice = cleaned, mastered or generated voice tracks.

Rules:
  - Camera and audio clips keep their FILE name as clip name. The camera goes in the clip's `Angle` metadata.
  - Before a big edit: snapshot(). The copy goes to the archive under a versioned name; the working timeline keeps its
    name and its place, so every pointer to it (records, scripts, the human's habit) stays right.
  - Keywords on every item (Resolve shows one smart bin per keyword): the video name, the shoot, the kind (timeline /
    latest / archive / rough / tight / camera / audio track / multicam / graphic / voice / subtitle …); timelines also
    carry their format (9x16 / 16x9 / 4x3) and shorts "short".
  - The format in a timeline's name matches its resolution (audit checks it). A video is cut as the long 9x16 first;
    the 16x9 and the shorts are made from the approved 9x16 (references/resolve.md § Organization).
  - Nothing at the pool root, no other top-level bin, no "/" in a bin name.
  - Moves go through move(): `MoveClips` / `MoveFolders` COPY when Resolve's window believes Alt is down.
"""
import os
import re
import time

TIMELINES, FOOTAGE, ASSETS = "01 Timelines", "02 Footage", "03 Assets"
LATEST, ARCHIVE = TIMELINES + "/WIP/Latest", TIMELINES + "/WIP/Archive"
FINAL, SCRATCH = TIMELINES + "/Final", TIMELINES + "/Scratch"
ASSET_KINDS = ("Images", "Videos", "Music", "SFX", "Voice", "Subtitles")
VERSION = re.compile(r"^(?P<video>.+) v(?P<n>\d{2,3}) (?P<date>\d{4}-\d{2}-\d{2})(?: (?P<state>.+))?$")
PLACE_WORDS = ("timeline", "latest", "archive", "final", "scratch")
STATES = ("rough", "tight", "graded", "mastered", "approved")  # read back from a timeline's keywords; others: pass state
# Timeline size per format. 9x16 = the long vertical every video is cut as first, and its shorts; 16x9 is made from the
# approved 9x16 (a 16x9 project's own UHD or HD size is kept); 4x3 only when a platform asks for it.
FORMATS = {"9x16": (1080, 1920), "16x9": (3840, 2160), "4x3": (1440, 1080)}
FORMAT_WORDS = tuple(FORMATS)
# "<video> <fmt>[ S<nn>[ <label>]][ - <variant>]" (the version suffix is stripped first, VERSION)
NAME = re.compile(r"^(?P<video>.+?) (?P<fmt>9x16|16x9|4x3)(?: S(?P<short>\d{2,3})(?: (?P<label>.+?))?)?(?: - (?P<variant>.+))?$")


def clean(name):
    """A bin or timeline name without path separators (a "/" in a bin name breaks every path lookup)."""
    return re.sub(r"\s*[\\/:]+\s*", " - ", str(name)).strip()


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
    found = {"stray_top_bins": [s.GetName() for s in root.GetSubFolderList() or [] if s.GetName() not in (TIMELINES, FOOTAGE, ASSETS)],
             "items_at_root": [c.GetName() for c in root.GetClipList() or []],
             "bin_names_with_slash": [], "timelines_outside_01": [], "versioned_name_in_latest": [],
             "archive_name_off_pattern": [], "latest_without_format": [], "format_not_resolution": [],
             "footage_clip_renamed": [], "without_keywords": []}
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
            if not path.startswith(TIMELINES):
                found["timelines_outside_01"].append(f"{path}/{name}")
            elif path == LATEST and VERSION.match(name):
                found["versioned_name_in_latest"].append(name)
            elif path in (ARCHIVE, FINAL) and not VERSION.match(name):
                found["archive_name_off_pattern"].append(name)
            elif path == LATEST and not parse(name)["fmt"]:
                found["latest_without_format"].append(name)  # fix: add_format(project, name)
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
