"""Where things go in a DaVinci Resolve project and what they are called: one layout for every stage, agent session
and human. Runs inside Resolve through Blackmagic's Resolve MCP (`run_script_unsafe`, `timeout` 60). Stdlib only.

    import sys, importlib; sys.path.insert(0, r"<plugin>/scripts")
    import resolve_bins as B; importlib.reload(B)              # reload: Resolve keeps modules between calls
    result = B.snapshot(resolve, project, "Teleprompter demo", "rough")    # before a big edit
    result = B.audit(project)                                              # what breaks the layout

Layout (paths from the media-pool root; the numbers keep the order in every sort mode):
  01 Timelines/WIP/Latest    working timelines, plain name = the video ("Teleprompter demo"). A variant waiting for a
                             choice = "<Video> - <variant>" ("Teleprompter demo - split panels").
  01 Timelines/WIP/Archive   history: "<Video> vNN YYYY-MM-DD <state>" ("Teleprompter demo v02 2026-03-04 rough").
                             NN counts up per video and is never reused.
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
    latest / archive / rough / tight / camera / audio track / multicam / graphic / voice / subtitle …).
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


def clean(name):
    """A bin or timeline name without path separators (a "/" in a bin name breaks every path lookup)."""
    return re.sub(r"\s*[\\/:]+\s*", " - ", str(name)).strip()


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
    """Version numbers already used for this video, sorted (archive and final names)."""
    out = []
    for i in range(1, project.GetTimelineCount() + 1):
        t = project.GetTimelineByIndex(i)
        m = VERSION.match(t.GetName() or "") if t else None
        if m and m.group("video") == video:
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
        tag(item, name, "timeline", to.rsplit("/", 1)[-1].lower(), state, drop=PLACE_WORDS + STATES)
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
    tag(item, name, "timeline", "archive", state, drop=PLACE_WORDS + STATES)
    return {"archived": new, "version": n, "bin": ARCHIVE}


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
             "archive_name_off_pattern": [], "footage_clip_renamed": [], "without_keywords": []}

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
