# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Rough cut from text anchors: keep ranges written as (approx second, "phrase") pairs snap to the transcript's words,
get padded, land on frames -> a Cut list (*.cuts.json) + retimed captions (SRT) + every seam printed for reading.

in:  --project edit.json, --spec rough.json:
       {"timeline": "Studio tour",
        "segments": [["A", [27.2, "faire une vidéo de quinze minutes"], [74, "pas avoir etc"], {"face": true}],
                     ["B", [57.5, "Ça me prenait trois heures"], [78.3, "j'arrive et j'enregistre"], {"alt": "short hook"}]],
        "markers": [["A", 27.2, "faire une vidéo", "Green", "Hook", "short candidate"]],
        "end_note": "what was cut and why"}
     Anchor times = the words file's own seconds (what the transcript shows, before words_offset_s). "*" in a phrase =
     any one word (a name the transcript misheard). Options per segment: face (show the face angle, hooks/outros),
     cam (show this angle, a name from the source's angles; "program" = the program file), alt (alternate take, placed
     after a 2 s gap at the end), takes (the same passage in other recordings of the script, as [[src, [s, "phrase"],
     [s, "phrase"]], ...]; the segment's own range = take 1, the default), head / tail (pad seconds, default 0.12 /
     0.25), why.
out: <out>.cuts.json + <out>.srt. --check = try every anchor, print each failure with its context, write nothing.
     Each cut segment carries specs (indexes of the rough.json segments it came from), cam, and takes (every snapped
     take, take 1 first) when the spec had takes: resolve_build.py --mode takes builds the take picker from them.
--picks picks.json (resolve_build.py --mode read_takes: {"picks": [{"specs": [k], "src", "cam"}]}) = the human's
     choices: the picked recording becomes the segment's range (the others stay in takes), cam = the picked camera.
     With --out it writes them into the --spec file (the previous one kept as <spec>.before-picks.json), so a later
     run needs no --picks and an edited rough.json never meets stale picks (picks name segments by index).
usage: python rough.py --project cut/edit.json --spec cut/rough.json --out cut/rough [--check] [--picks picks.json]

Pads (0.12 s before the first word, 0.25 s after the last, clamped to the midpoint with the neighbour word) then
frames: in = floor, out = ceil. Touching segments of one source merge (never one with takes: each is its own choice
in the picker); a source split only to switch the angle stays seamless (<= 8 frames apart -> same frame).
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()


def norm_seg(spec):
    if isinstance(spec, dict):
        return spec["src"], spec["from"], spec["to"], {k: v for k, v in spec.items() if k not in ("src", "from", "to")}
    return spec[0], spec[1], spec[2], (spec[3] if len(spec) > 3 else {})


def norm_marker(m):
    if isinstance(m, dict):
        at = m.get("at") or [m["t"], m["anchor"]]
        return m["src"], at[0], at[1], m.get("color", "Blue"), m["name"], m.get("note", "")
    return tuple(m)


def snap(P, src, fr, to, opts, pad_in, pad_out):
    """One range of one source: anchors -> word ids -> padded edges -> frames, with the seam text around it."""
    (fa, fanc), (ta, tanc) = fr, to
    words, off = P.words(src), P.offset(src)
    i0, _ = E.find_anchor(words, fa + off, fanc)
    _, i1 = E.find_anchor(words, ta + off, tanc)
    if i1 < i0:
        raise ValueError(f"segment ends before it starts: {src} {fanc!r} -> {tanc!r}")
    head, tail = opts.get("head", pad_in), opts.get("tail", pad_out)
    t_in = words[i0]["start"] - head
    if i0 > 0 and head <= pad_in:
        t_in = max(t_in, (words[i0 - 1]["end"] + words[i0]["start"]) / 2)
    t_out = words[i1]["end"] + tail
    if i1 + 1 < len(words) and tail <= pad_out:
        t_out = min(t_out, (words[i1]["end"] + words[i1 + 1]["start"]) / 2)
    return {"src": src, "in_f": max(0, math.floor(t_in * P.fps)), "out_f": math.ceil(t_out * P.fps),
            "first": " ".join(w["text"] for w in words[i0:i0 + 6]),
            "last": " ".join(w["text"] for w in words[max(i0, i1 - 5):i1 + 1]),
            "cut_before": " ".join(w["text"] for w in words[max(0, i0 - 5):i0]),
            "cut_after": " ".join(w["text"] for w in words[i1 + 1:i1 + 6])}


def apply_picks(c, picks):
    """The human's choices in the take picker -> rough spec: the picked recording becomes the segment's own range (the
    others stay in its takes, so the picker can be rebuilt), cam = the picked camera (it replaces face)."""
    segs = [list(norm_seg(s)) for s in c["segments"]]
    for p in picks:
        for k in p["specs"]:
            src, fr, to, opts = segs[k]
            opts = dict(opts)
            if p["src"] != src:
                alts = [list(t) for t in opts.get("takes") or []]
                hit = next((t for t in alts if t[0] == p["src"]), None)
                if hit is None:
                    raise ValueError(f"segment {k}: picked {p['src']!r}, which is not one of its takes")
                opts["takes"] = [[src, fr, to]] + [t for t in alts if t is not hit]
                src, fr, to = hit[0], hit[1], hit[2]
            if p.get("cam"):
                opts["cam"] = p["cam"]
                opts.pop("face", None)
            segs[k] = [src, fr, to, opts]
    return dict(c, segments=segs)


def build(P, c, pad_in, pad_out):
    fps = P.fps
    segs, rec_f, alt_started = [], 0, False
    gap_f = round(float(c.get("alt_gap_s", 2.0)) * fps)
    for k, spec in enumerate(c["segments"]):
        src, fr, to, opts = norm_seg(spec)
        m = snap(P, src, fr, to, opts, pad_in, pad_out)
        in_f, out_f = m["in_f"], m["out_f"]
        takes = None
        if opts.get("takes"):  # the same passage in other recordings: take 1 = this segment's own range
            keep = ("src", "in_f", "out_f", "first", "last")
            takes = [{x: m[x] for x in keep}] + [{x: snap(P, t[0], t[1], t[2], opts, pad_in, pad_out)[x] for x in keep}
                                                 for t in opts["takes"]]
        alt = opts.get("alt")
        if alt and not alt_started:
            alt_started = True
            rec_f += gap_f
        p = segs[-1] if segs else None
        cam = opts.get("cam") or None
        face = bool(opts.get("face")) and not cam
        view = cam or ("face" if face else None)  # what V1 shows: a picked camera, the face angle, or the program
        same = bool(p and p["src"] == src and p["alt"] == alt and not takes and not p.get("takes"))
        if same and p["_view"] != view and abs(in_f - p["out_f"]) <= 8:
            in_f = p["out_f"]  # same source split only to switch the angle: keep it seamless
        seg = {"src": src, "in_f": in_f, "out_f": out_f, "alt": alt, "face": face, "cam": cam, "specs": [k],
               "why": opts.get("why", ""), **{x: m[x] for x in ("first", "last", "cut_before", "cut_after")}}
        if takes:
            seg["takes"] = takes
        if same and p["_view"] == view and 0 <= in_f - p["out_f"] <= 8:
            p.update(out_f=out_f, last=seg["last"], cut_after=seg["cut_after"])
            p["specs"].append(k)
            rec_f += out_f - p["_end"]
            p["_end"] = out_f
            continue
        seg.update(rec_f=rec_f, _end=out_f, _view=view)
        segs.append(seg)
        rec_f += out_f - in_f
    for s in segs:
        s.pop("_end")
        s.pop("_view")
        E.seg_seconds(s, fps)
    markers = []
    for m in c.get("markers", []):
        src, t, anc, color, name, note = norm_marker(m)
        words, off = P.words(src), P.offset(src)
        i0, _ = E.find_anchor(words, t + off, anc)
        ws = words[i0]["start"]
        s = next((s for s in segs if s["src"] == src and s["in_s"] <= ws <= s["out_s"]), None)
        if s is not None:
            frame = s["rec_f"] + max(0, round(ws * fps) - s["in_f"])
        else:  # its passage now plays from another take (picks): same place, scaled into the picked take
            hit = next(((x, t) for x in segs for t in x.get("takes", []) if t["src"] == src
                        and t["in_f"] <= ws * fps <= t["out_f"]), None)
            if hit is None:
                raise ValueError(f"marker outside the kept segments: {name} @ {src} {ws - off:.2f}")
            x, t = hit
            frac = (ws * fps - t["in_f"]) / max(1, t["out_f"] - t["in_f"])
            frame = x["rec_f"] + round(frac * (x["out_f"] - x["in_f"]))
        markers.append({"frame": frame, "color": color, "name": name,
                        "note": f"[{src} {int((ws - off) // 60):02}:{(ws - off) % 60:05.2f}] {note}".strip()})
    main_end = E.main_end_f(segs)
    markers.append({"frame": main_end - 1, "color": "Red", "name": c.get("end_name", "End of rough cut"),
                    "note": c.get("end_note", ""), "kind": "end"})
    for a in dict.fromkeys(s["alt"] for s in segs if s["alt"]):
        s0 = next(s for s in segs if s["alt"] == a)
        markers.append({"frame": s0["rec_f"], "color": "Purple", "name": f"ALT: {a}",
                        "note": c.get("alt_note", "Alternate take kept outside the main cut.")})
    cues = []
    for src in dict.fromkeys(s["src"] for s in segs):
        cues += E.retime_srt(P.captions(src), P.words(src), [s for s in segs if s["src"] == src], fps)
    cues = E.finish_cues(cues, segs, fps)
    out = {"fps": fps, "timeline": c.get("timeline", "rough"), "sources": {k: P.file(k) for k in dict.fromkeys(s["src"] for s in segs)},
           "alt_gap_s": c.get("alt_gap_s", 2.0), "segments": segs, "markers": markers, "main_s": main_end / fps,
           "total_f": E.total_f(segs)}
    return out, cues


def check(P, c):
    bad = 0
    items = []
    for spec in c["segments"]:
        src, fr, to, opts = norm_seg(spec)
        items += [(src, fr), (src, to)]
        for t in opts.get("takes") or []:
            items += [(t[0], t[1]), (t[0], t[2])]
    items += [(m[0], m[1:3]) for m in (norm_marker(x) for x in c.get("markers", []))]
    for src, (t, anc) in items:
        try:
            E.find_anchor(P.words(src), t + P.offset(src), anc)
        except (ValueError, KeyError) as e:
            bad += 1
            print(f"{src} {t}: {e}\n")
    print("anchor failures:", bad)
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--spec", required=True, help="rough.json (anchors)")
    ap.add_argument("--out", help="output prefix (<out>.cuts.json, <out>.srt)")
    ap.add_argument("--check", action="store_true", help="only try every anchor and list failures")
    ap.add_argument("--pad-in", type=float, default=0.12)
    ap.add_argument("--pad-out", type=float, default=0.25)
    ap.add_argument("--picks", help="picks.json from resolve_build.py --mode read_takes: the human's takes and cameras")
    a = ap.parse_args()
    P = E.Project(a.project)
    c = E.read_json(a.spec)
    if a.picks:
        c = apply_picks(c, E.read_json(a.picks)["picks"])
    if a.check:
        sys.exit(1 if check(P, c) else 0)
    if not a.out:
        ap.error("--out is required (or --check)")
    if a.picks:  # the picks are the human's decision: they go into rough.json, the previous one is kept beside it
        sp = Path(a.spec)
        E.write_json(sp.with_name(sp.stem + ".before-picks.json"), E.read_json(a.spec))
        E.write_json(a.spec, c)
        print(f"picks written into {sp.name} (previous: {sp.stem}.before-picks.json); later runs need no --picks")
    r, cues = build(P, c, a.pad_in, a.pad_out)
    out = Path(a.out)
    r["srt"] = str(out.resolve().with_name(out.name + ".srt"))
    E.write_srt(r["srt"], cues)
    E.write_json(out.with_name(out.name + ".cuts.json"), r)
    print(f"{r['timeline']}: main {r['main_s'] / 60:.2f} min, total {r['total_f'] / P.fps / 60:.2f} min, {len(r['segments'])} segments, "
          f"{len(cues)} cues -> {out}.cuts.json / .srt\nSeams (…cut before || kept … || cut after…): read every one.")
    for s in r["segments"]:
        tag = (f" [ALT {s['alt']}]" if s["alt"] else "") + (f" [cam {s['cam']}]" if s.get("cam") else "") + \
              (f" [{len(s['takes'])} takes]" if s.get("takes") else "")
        print(f"  {s['src']:>4} [{s['in_s']:7.2f}-{s['out_s']:7.2f}]{tag} …{s['cut_before'][-35:]} || {s['first']} … "
              f"{s['last']} || {s['cut_after'][:35]}…")


if __name__ == "__main__":
    main()
