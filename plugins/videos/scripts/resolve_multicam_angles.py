# /// script
# requires-python = ">=3.11"
# dependencies = ["zstandard; python_version < '3.14'"]
# ///
"""Switch the ACTIVE ANGLE of multicam clips on a DaVinci Resolve timeline. Resolve's scripting API (21.2) has no
call for it, so this goes through a .drt round trip. House rule it serves: a camera change is the multicam clip's
active angle on V1, never another camera's clip on a track above it.

What switch_angles does (inside Resolve, through Blackmagic's Resolve MCP `run_script_unsafe`):
  duplicate the timeline -> cut V1 multicam clips at the switch points (delete + re-append the same source frames,
  video only) -> drop the converted camera clips -> export .drt -> write "Camera N" into each V1 item -> import the
  .drt -> check it equals the work copy (every item, sub-frame edge, transition, subtitle, marker, track name) and
  every V1 angle reads back as asked -> keep it (new timeline, or replace the original with inplace=True).
The original is never touched before every check passed; on a failure the result is deleted and `error` says why.
Needs zstd for compressed FieldsBlobs: stdlib on Python >= 3.14, else the `zstandard` package in the Python that runs
Resolve's scripts (the inline dependency above only covers `uv run`). Neither there = `error` names it, nothing changes.

    import sys, importlib; sys.path.insert(0, "<plugin>/scripts")
    import resolve_multicam_angles as A; importlib.reload(A)          # reload: Resolve keeps modules between calls
    result = A.switch_angles(resolve, project, "<timeline name>",
                             switches=[("01:00:12:00", "01:00:18:00", "Left"), (300, 420, 2)],
                             from_tracks=False,         # True: camera clips on V2+ (angle sources) -> angles on V1
                             out_name=None,             # default "<name> (angles)"; ignored with inplace=True
                             inplace=False, dry=False)  # dry: plan + checks only, changes nothing
switches: (start, end, camera). start/end = timeline timecode "HH:MM:SS:FF" (non-drop) or int frames from the
timeline start. camera = angle number (1-based) or a word matched against the angle clip names ("Left" matches
"3 A Left"). from_tracks clips are applied lower track first (a higher track wins), then switches in order.
Report: converted, left (clips/switches not applied + why), skipped (V1 clips not cut: linked audio or a transform
set, re-appending would drop it), items_changed / items_split / pieces, angles_set, sync_frames = {old camera clip
frame - multicam angle frame: count} (0 = same picture; +-1 = Resolve's audio sync rounded that angle differently
from the clip's own offset), diff / wrong (round-trip checks), steps (seconds).

Offline: python resolve_multicam_angles.py some.drt   -> V1 multicam items, their angles, the angle list per multicam.

File format (verified on Resolve Studio 21.2): .drt = zip of XML. Timeline and multicam = <Sm2SequenceContainer>,
VideoTrackVec/Element/Sm2TiTrack/Items/Element/Sm2TiVideoClip. A V1 multicam item's <MediaRef> = the media pool
<Sm2MpMulticamClip DbId>, whose <Sm2Sequence DbId> is the multicam's sequence; angle N = that sequence's Nth video
track (Resolve orders angles by clip NAME, not by the list given to CreateMulticamClip). The item's <FieldsBlob> =
hex [u32 2][u32 length of the rest][0x80 raw | 0x81 zstd][protobuf-like message] holding 1a 08 "Camera N"; no field
= angle 1. MediaPool.ImportTimelineFromFile(drt) names the timeline after the FILE (ImportOptions are ignored for
.drt), lands in the current bin, links the existing media (no duplicate clips) and makes it the current timeline.
TimelineItem.GetName() of a multicam item = "<multicam> - <angle clip name>" = the readback.
"""
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

try:
    from compression import zstd as _z  # Python >= 3.14 (also Resolve 21.2's script Python)

    def _unz(b):
        return _z.decompress(b)

    def _z1(b):
        return _z.compress(b, level=3)
except ImportError:  # pragma: no cover
    try:
        import zstandard
    except ImportError:  # neither library: the import must still succeed, resolve_build loads this module mid-build
        zstandard = None

    def _need_zstd():
        if zstandard is None:
            raise RuntimeError(
                "zstd missing: this Python is older than 3.14 and has no `zstandard` package. Install it into the "
                "Python that runs Resolve's scripts (`<that python> -m pip install zstandard`), or keep the camera "
                "clips on V2 / V3 (resolve_build: camera_tracks=True / --camera-tracks)")

    def _unz(b):
        _need_zstd()
        return zstandard.ZstdDecompressor().decompress(b, max_output_size=1 << 20)

    def _z1(b):
        _need_zstd()
        return zstandard.ZstdCompressor(level=3, write_content_size=True).compress(b)

_CAM = re.compile(rb"\x1a\x08Camera \d")
_ITEM = re.compile(r"<Sm2TiVideoClip DbId=\"[^\"]*\">.*?</Sm2TiVideoClip>", re.S)


# ---------------------------------------------------------------------------------------------- FieldsBlob
def _payload(b):
    assert b[:4] == b"\x00\x00\x00\x02" and b[8] in (0x80, 0x81), b[:12].hex()
    return b[9:] if b[8] == 0x80 else _unz(b[9:])


def blob_angle(hx):
    """Active angle stored in a multicam timeline item's FieldsBlob hex (no Camera field = 1)."""
    if not hx:
        return 1
    m = _CAM.search(_payload(bytes.fromhex(hx)))
    return int(m.group(0)[-1:]) if m else 1


def set_angle(hx, n):
    """FieldsBlob hex with the active angle set to n (1..9)."""
    assert 1 <= n <= 9, n
    b = bytes.fromhex(hx)
    raw = _payload(b)
    tag = b"\x1a\x08Camera " + str(n).encode()
    if _CAM.search(raw):
        new = _CAM.sub(tag, raw, count=1)
    else:  # 0a <len> 0a <innerlen> <inner> 20 01 … -> insert before "20 01", outer len += 10 (one-byte varints)
        assert raw[0] == 0x0a and raw[2] == 0x0a and raw[1] < 0x76 and raw[3] < 0x80, raw[:4].hex()
        p = 4 + raw[3]
        assert raw[p:p + 2] == b"\x20\x01", raw[p:p + 4].hex()
        new = bytes([0x0a, raw[1] + 10]) + raw[2:p] + tag + raw[p:]
    body = b"\x80" + new if b[8] == 0x80 else b"\x81" + _z1(new)
    assert _payload(b[:8] + body) == new
    return (b[:4] + len(body).to_bytes(4, "big") + body).hex()


# ---------------------------------------------------------------------------------------------- .drt read / patch
def _int(e, tag, default=0):
    x = e.find(tag)
    return int(float(x.text)) if x is not None and x.text not in (None, "") else default


def _txt(e, tag):
    x = e.find(tag)
    return x.text if x is not None and x.text else ""


def read_drt(path):
    """{"timeline_seq", "mc_seq": {multicam clip DbId: sequence id}, "seqs": {sequence id: {"video": [[item, …] per
    track]}}}; item = {dbid, name, start, dur, in, ref (MediaRef), mst (MediaStartTime s), blob, angle}."""
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n).decode("utf-8") for n in z.namelist()}
    mc_seq, tl_seq = {}, None
    for n, s in files.items():
        if not n.startswith("MediaPool/"):
            continue
        for m in re.finditer(r'<Sm2MpMulticamClip DbId="([^"]+)">(.*?)</Sm2MpMulticamClip>', s, re.S):
            q = re.search(r'<Sm2Sequence DbId="([^"]+)"', m.group(2))
            if q:
                mc_seq[m.group(1)] = q.group(1)
        m = re.search(r'<Sm2MpTimelineClip DbId="[^"]+">.*?<Sm2Sequence DbId="([^"]+)"', s, re.S)
        if m and tl_seq is None:
            tl_seq = m.group(1)
    seqs = {}
    for n, s in files.items():
        if not n.startswith("SeqContainer/"):
            continue
        root = ET.fromstring(s)
        video, sid = [], None
        for tr in root.findall("VideoTrackVec/Element/Sm2TiTrack"):
            sid = sid or _txt(tr, "Sequence")
            items = []
            for el in tr.findall("Items/Element"):
                for it in el:
                    if it.tag != "Sm2TiVideoClip":
                        continue
                    try:
                        mst = float(_txt(it, "MediaStartTime") or 0)
                    except ValueError:
                        mst = 0.0
                    items.append({"dbid": it.attrib.get("DbId"), "name": _txt(it, "Name"), "start": _int(it, "Start"),
                                  "dur": _int(it, "Duration"), "in": _int(it, "In"), "ref": _txt(it, "MediaRef"),
                                  "mst": mst, "blob": _txt(it, "FieldsBlob")})
            video.append(items)
        if sid:
            seqs[sid] = {"file": n, "video": video}
    for sq in seqs.values():
        for tr in sq["video"]:
            for it in tr:
                it["angle"] = blob_angle(it["blob"]) if it["ref"] in mc_seq else None
    return {"timeline_seq": tl_seq, "mc_seq": mc_seq, "seqs": seqs}


def angles_of(info, mc_ref):
    """Multicam clip DbId -> [{n, names, refs, items}] (angle n = n-th video track of its sequence)."""
    sq = info["seqs"].get(info["mc_seq"].get(mc_ref))
    if not sq:
        return []
    return [{"n": k + 1, "names": [it["name"] for it in tr], "refs": {it["ref"] for it in tr}, "items": tr}
            for k, tr in enumerate(sq["video"])]


def _rewrite(src, dst, fn):
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    for n in list(files):
        if n.startswith("SeqContainer/"):
            files[n] = _ITEM.sub(fn, files[n].decode("utf-8")).encode("utf-8")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)


def _with_angle(item_xml, n):
    return re.sub(r"<FieldsBlob>([^<]*)</FieldsBlob>", lambda f: f"<FieldsBlob>{set_angle(f.group(1), n)}</FieldsBlob>",
                  item_xml, count=1)


def patch_by_dbid(src, dst, want):
    """want = {Sm2TiVideoClip DbId: angle}. Returns the DbIds not found."""
    left = dict(want)

    def rep(m):
        dbid = re.match(r'<Sm2TiVideoClip DbId="([^"]+)"', m.group(0)).group(1)
        return _with_angle(m.group(0), left.pop(dbid)) if dbid in left else m.group(0)
    _rewrite(src, dst, rep)
    return sorted(left)


def patch(src, dst, want, name_prefix="MC "):
    """want = {record frame incl. timeline start (<Start>): angle} for items whose name starts with name_prefix."""
    done = {}

    def rep(m):
        it = m.group(0)
        st = int(re.search(r"<Start>(\d+)</Start>", it).group(1))
        if st in want and re.search(rf"<Name>{re.escape(name_prefix)}", it):
            done[st] = want[st]
            return _with_angle(it, want[st])
        return it
    _rewrite(src, dst, rep)
    return {"set": len(done), "missing": sorted(set(want) - set(done))}


def mc_offsets(src):
    """{SeqContainer: {angle clip name: start offset vs angle 1, frames}} for multicam sequences whose angle clips
    are named '<n> …' (the naming resolve_build.py gives them)."""
    out = {}
    with zipfile.ZipFile(src) as zin:
        for n in zin.namelist():
            if not n.startswith("SeqContainer/"):
                continue
            s = zin.read(n).decode("utf-8")
            its = [(re.search(r"<Name>([^<]*)</Name>", it).group(1), int(re.search(r"<Start>(\d+)</Start>", it).group(1)))
                   for it in _ITEM.findall(s)]
            if its and all(re.match(r"\d+ ", nm) for nm, _ in its):
                one = next((st for nm, st in its if nm.startswith("1 ")), None)
                if one is not None:
                    out[n] = {nm: st - one for nm, st in its}
    return out


# ---------------------------------------------------------------------------------------------- Resolve side
def _fps(tl):
    f = float(tl.GetSetting("timelineFrameRate") or 24)
    for exact in (24000 / 1001, 30000 / 1001, 60000 / 1001):
        if abs(f - exact) < 0.01:
            return exact
    return f


def _tc_to_frame(tc, fps, start):
    if isinstance(tc, (int, float)):
        return start + int(tc)
    h, m, s, f = (int(x) for x in re.split(r"[:;]", tc))
    nom = round(fps)
    return h * 3600 * nom + m * 60 * nom + s * nom + f


def _find_timeline(project, name):
    for i in range(1, project.GetTimelineCount() + 1):
        t = project.GetTimelineByIndex(i)
        if t and t.GetName() == name:
            return t
    return None


def _bin_of(mp, name):
    def walk(f):
        for c in f.GetClipList() or []:
            if c.GetName() == name and "Timeline" in (c.GetClipProperty("Type") or ""):
                return f
        for s in f.GetSubFolderList() or []:
            r = walk(s)
            if r:
                return r
        return None
    return walk(mp.GetRootFolder())


def _snap(t):
    """Every item on every track (V1 without names: the angle is in the name), track names, markers."""
    def r(x):
        return None if x is None else round(x, 4)
    s0, res = t.GetStartFrame(), {}
    for kind in ("video", "audio", "subtitle"):
        for n in range(1, t.GetTrackCount(kind) + 1):
            res[f"{kind}{n}"] = [(r(i.GetStart(True) - s0), r(i.GetEnd(True) - s0),
                                  None if kind == "subtitle" else r(i.GetLeftOffset(True)),
                                  None if (kind, n) == ("video", 1) else (i.GetName() or ""))
                                 for i in t.GetItemListInTrack(kind, n) or []]
            res[f"{kind}{n}_name"] = t.GetTrackName(kind, n)
    res["markers"] = sorted((f, m["color"], m["name"]) for f, m in (t.GetMarkers() or {}).items())
    return res


_PLAIN = {"Pan": 0.0, "Tilt": 0.0, "ZoomX": 1.0, "ZoomY": 1.0, "RotationAngle": 0.0, "Opacity": 100.0, "CropLeft": 0.0,
          "CropRight": 0.0, "CropTop": 0.0, "CropBottom": 0.0, "FlipX": False, "FlipY": False}


def switch_angles(resolve, project, timeline, switches=(), from_tracks=False, out_name=None, inplace=False, dry=False,
                  workdir=None):
    t0 = time.time()
    rep = {"timeline": timeline, "steps": {}}

    def step(k):
        rep["steps"][k] = round(time.time() - t0, 1)
    mp = project.GetMediaPool()
    src = _find_timeline(project, timeline)
    if not src:
        return {"error": f"timeline not found: {timeline}"}
    keep_tl, keep_folder, keep_page = project.GetCurrentTimeline(), mp.GetCurrentFolder(), resolve.GetCurrentPage()
    keep_tl_name = keep_tl.GetName() if keep_tl else None
    bin_ = _bin_of(mp, timeline) or keep_folder
    out_name = timeline if inplace else (out_name or f"{timeline} (angles)")
    if not inplace and _find_timeline(project, out_name):
        return {"error": f"a timeline named {out_name!r} exists already"}
    wd = workdir or os.path.join(tempfile.gettempdir(), "resolve_multicam_angles")
    os.makedirs(wd, exist_ok=True)
    tag = re.sub(r"[^\w.-]+", "_", timeline)[:60] + time.strftime("_%H%M%S")
    p_read, p_built, p_set = (os.path.join(wd, f"{tag}_{x}.drt") for x in ("read", "built", "angles"))
    work = None

    def restore(current=None):
        mp.SetCurrentFolder(keep_folder)
        t = current or (_find_timeline(project, keep_tl_name) if keep_tl_name else None)
        if t:
            project.SetCurrentTimeline(t)
        resolve.OpenPage(keep_page)

    try:
        # 0. work copy, read it
        mp.SetCurrentFolder(bin_)
        work = src.DuplicateTimeline(f"{timeline} (angles work {time.strftime('%H%M%S')})")
        if not work:
            return {"error": "DuplicateTimeline failed"}
        project.SetCurrentTimeline(work)
        fps, st = _fps(work), work.GetStartFrame()
        if not work.Export(p_read, resolve.EXPORT_DRT, resolve.EXPORT_NONE):
            raise RuntimeError("drt export failed")
        info = read_drt(p_read)
        step("read")
        tracks = info["seqs"][info["timeline_seq"]]["video"]
        v1 = [it for it in tracks[0] if it["ref"] in info["mc_seq"]]
        by_start = {v["start"]: v for v in v1}
        api_v1 = {int(round(i.GetStart())): i for i in work.GetItemListInTrack("video", 1) or []}
        plan = {v["start"]: [[v["start"], v["start"] + v["dur"], v["angle"]]] for v in v1}
        rep["v1_multicam_items"] = len(v1)

        def resolve_angle(v, cam):
            angs = angles_of(info, v["ref"])
            if isinstance(cam, int):
                return cam if 1 <= cam <= len(angs) else None
            if isinstance(cam, tuple):  # ("ref", source clip DbId)
                return next((a["n"] for a in angs if cam[1] in a["refs"]), None)
            c = str(cam).lower()
            hit = [a["n"] for a in angs if any(re.search(rf"(^|\W){re.escape(c)}(\W|$)", nm.lower()) for nm in a["names"])]
            return hit[0] if len(hit) == 1 else None

        def paint(ps, pe, cam):
            hits = [v for v in v1 if v["start"] < pe and ps < v["start"] + v["dur"]]
            covered, bad = 0, []
            for v in hits:
                n = resolve_angle(v, cam)
                if n is None:
                    bad.append(v["start"] - st)
                    continue
                a, b = max(ps, v["start"]), min(pe, v["start"] + v["dur"])
                covered += b - a
                segs = []
                for s0, s1, k in plan[v["start"]]:
                    if s1 <= a or s0 >= b:
                        segs.append([s0, s1, k])
                        continue
                    if s0 < a:
                        segs.append([s0, a, k])
                    if s1 > b:
                        segs.append([b, s1, k])
                segs.append([a, b, n])
                segs.sort()
                merged = []
                for sg in segs:
                    if merged and merged[-1][2] == sg[2] and merged[-1][1] == sg[0]:
                        merged[-1][1] = sg[1]
                    else:
                        merged.append(sg)
                plan[v["start"]] = merged
            return covered, bad, [v["start"] for v in hits]

        # 1. camera clips on V2+ -> angles (lower tracks first, so a higher track wins)
        converted, sync, left = [], [], []
        if from_tracks:
            for n in range(2, len(tracks) + 1):
                api_n = {int(round(i.GetStart())): i for i in work.GetItemListInTrack("video", n) or []}
                on = work.GetIsTrackEnabled("video", n)
                for it in tracks[n - 1]:
                    a = api_n.get(it["start"])
                    tc = it["start"] - st
                    if a is None:
                        left.append((n, tc, "not found through the API"))
                        continue
                    if not on or not a.GetClipEnabled():
                        left.append((n, tc, "disabled clip or track: left in place"))
                        continue
                    covered, bad, under = paint(it["start"], it["start"] + it["dur"], ("ref", it["ref"]))
                    if bad or covered < it["dur"]:
                        left.append((n, tc, "no multicam angle for this source under the whole clip: left in place"))
                        continue
                    converted.append((a, under, (n, tc)))
                    # picture check: this clip's source frame vs the angle frame the multicam shows at the same spot
                    v = next(v for v in v1 if v["start"] <= it["start"] < v["start"] + v["dur"])
                    ang = next((x for x in angles_of(info, v["ref"]) if it["ref"] in x["refs"]), None)
                    ai = next((x for x in ang["items"] if x["ref"] == it["ref"]), None) if ang else None
                    if ai:
                        m_abs = round(v["mst"] * fps) + v["in"] + (it["start"] - v["start"])
                        sync.append(it["in"] - (m_abs - ai["start"] + ai["in"]))
        # 2. explicit switches
        for sw in switches:
            ps, pe, cam = _tc_to_frame(sw[0], fps, st), _tc_to_frame(sw[1], fps, st), sw[2]
            covered, bad, _ = paint(ps, pe, cam)
            if bad or covered < pe - ps:
                left.append(("switch", sw, f"camera {cam!r} is not an angle of the multicam (or no multicam) under part of it"))
        changed = {k: segs for k, segs in plan.items()
                   if segs != [[k, k + by_start[k]["dur"], by_start[k]["angle"]]]}
        split = {k: segs for k, segs in changed.items() if len(segs) > 1}
        skipped = []
        for k in list(split):
            a = api_v1.get(k)
            props = (a.GetProperty() or {}) if a else {}
            if a is None or (a.GetLinkedItems() or []) or any(props.get(p, d) != d for p, d in _PLAIN.items()):
                skipped.append((k - st, "linked audio / transform set / not found: not cut"))
                split.pop(k)
                changed.pop(k)
        # a camera clip goes only when every V1 clip under it took its angle; else it stays on top (picture unchanged)
        kept, not_cut = [], {s + st for s, _ in skipped}
        for a, under, where in converted:
            if not not_cut.intersection(under):
                kept.append(a)
            else:
                left.append((where[0], where[1], "a V1 clip under it was not cut: left in place"))
        converted = kept
        rep.update(converted=len(converted), left=left, skipped=skipped, items_changed=len(changed), items_split=len(split),
                   pieces=sum(len(s) for s in split.values()),
                   sync_frames={str(d): sync.count(d) for d in sorted(set(sync))})
        step("plan")
        if dry or not changed:
            if dry:
                rep["plan"] = {k - st: [(a - st, b - st, n) for a, b, n in segs] for k, segs in list(changed.items())[:40]}
            else:
                rep["note"] = "nothing to change"
            mp.DeleteTimelines([work])
            work = None
            restore()
            return rep

        # 3. cut V1 + drop converted clips on the work copy
        appends = []
        for k, segs in split.items():
            v = by_start[k]
            mpi = api_v1[k].GetMediaPoolItem()
            for a, b, n in segs:
                appends.append({"mediaPoolItem": mpi, "startFrame": v["in"] + (a - k), "endFrame": v["in"] + (b - k),
                                "recordFrame": a, "trackIndex": 1, "mediaType": 1})
        dels = [api_v1[k] for k in split] + converted
        if dels and not work.DeleteClips(dels, False):
            raise RuntimeError("DeleteClips failed")
        if appends:
            got = mp.AppendToTimeline(appends) or []
            if len(got) != len(appends):
                raise RuntimeError(f"AppendToTimeline placed {len(got)} of {len(appends)} pieces")
        if from_tracks:  # drop the tracks the conversion emptied, top down
            for n in range(work.GetTrackCount("video"), 1, -1):
                if n <= len(tracks) and tracks[n - 1] and not (work.GetItemListInTrack("video", n) or []):
                    work.DeleteTrack("video", n)
        step("cut")

        # 4. angles through the .drt
        if not work.Export(p_built, resolve.EXPORT_DRT, resolve.EXPORT_NONE):
            raise RuntimeError("drt export (built) failed")
        info2 = read_drt(p_built)
        v1b = [it for it in info2["seqs"][info2["timeline_seq"]]["video"][0] if it["ref"] in info2["mc_seq"]]
        want_at = {a: n for segs in changed.values() for a, b, n in segs}
        want = {it["dbid"]: want_at[it["start"]] for it in v1b if it["start"] in want_at and it["angle"] != want_at[it["start"]]}
        missing = patch_by_dbid(p_built, p_set, want)
        if missing:
            raise RuntimeError(f"{len(missing)} V1 items not found in the drt")
        step("patch")
        mp.SetCurrentFolder(bin_)
        nt = mp.ImportTimelineFromFile(p_set)
        if not nt:
            raise RuntimeError("drt import failed")
        step("import")

        # 5. checks: equal to the work copy, angles as asked
        sa, sb = _snap(work), _snap(nt)
        diff = sorted(k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k))
        names = {}
        for v in v1b:
            for x in angles_of(info2, v["ref"]):
                names[(v["ref"], x["n"])] = x["names"][0] if x["names"] else ""
        refs = {v["start"]: v["ref"] for v in v1b}
        final = {**{v["start"]: v["angle"] for v in v1b}, **want_at}
        wrong = []
        for i in nt.GetItemListInTrack("video", 1) or []:
            k = int(round(i.GetStart()))
            if k in refs:
                exp = names.get((refs[k], final.get(k, 1)), "")
                if exp and not (i.GetName() or "").endswith(" - " + exp):
                    wrong.append((k - st, final.get(k), (i.GetName() or "")[-40:]))
        rep.update(diff=diff, wrong=wrong[:10], n_wrong=len(wrong), angles_set=len(want))
        if diff or wrong:
            mp.DeleteTimelines([nt])
            raise RuntimeError("round trip check failed (result deleted, original untouched)")
        mp.DeleteTimelines([work])
        work = None
        if inplace:
            mp.DeleteTimelines([src])
        nt.SetName(out_name)
        rep.update(ok=True, result=out_name, length=nt.GetEndFrame() - nt.GetStartFrame())
        step("done")
        restore(nt if inplace and keep_tl_name == timeline else None)
        return rep
    except Exception as e:  # keep the original; drop the work copy
        rep["error"] = f"{type(e).__name__}: {e}"
        if work:
            try:
                mp.DeleteTimelines([work])
            except Exception:
                pass
        restore()
        return rep


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        sys.exit("usage: python resolve_multicam_angles.py <timeline.drt>")
    info = read_drt(sys.argv[1])
    seen = set()
    v1 = [it for it in info["seqs"][info["timeline_seq"]]["video"][0] if it["ref"] in info["mc_seq"]]
    print(f"{len(v1)} V1 multicam items; angles used: "
          + ", ".join(f"{n} x{sum(1 for it in v1 if it['angle'] == n)}" for n in sorted({it["angle"] for it in v1})))
    for it in v1:
        if it["ref"] not in seen:
            seen.add(it["ref"])
            print(f"  {it['name']}: " + " | ".join(f"{a['n']} {a['names'][0] if a['names'] else '?'}" for a in angles_of(info, it["ref"])))
