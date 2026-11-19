# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""video_ci.py: the automated QA every render passes before a human sees it ("video CI").

Two tiers, one verdict per file:
  tech   (local, free): stream spec, A/V length, loudness, black frames, frozen picture, dead air
  visual (decide.py, paid, ~$0.005-0.02 per short): the profile's decide pack (hook, CTA, caption on a face, framing,
         grade, secrets on screen, text colliding or cut by the frame edge right after each scene change, overlay
         text too small for a phone; stray text and wipes for generated media)
Transitions: the tech pass also finds scene changes (ffmpeg scdet); --events adds known ones (graphic in/out times from
a spec). The pack's `transitions` sample looks at stills 0.1 s and 0.6 s after each one, where regular sampling misses
a broken entrance or a label that never left.
Provenance: --input / --manifest list the files a render was built from (spec, master, LUT, template, project dir);
their size + mtime (folders: a fingerprint of every file) are stored with the CI result, and the videos.py gate refuses
review/done once one of them changed: the final is stale even though the file itself did not move.
Verdict: RED = a blocking check failed (do not show the file, fix it), AMBER = something a human should look at (show
the file WITH the flagged frames and say what was flagged), GREEN = nothing found.

Profiles (default auto: image -> generated, portrait or under 3 min -> short, else master):
  short      finished short, any aspect: finals spec, -14 LUFS, pack short (hook needs --var hook, CTA --var cta)
  clip       vertical clip cut from a long video: same as short
  master     long master: finals spec, -14 LUFS, pack master (grade, framing, secrets), sampling capped
  generated  AI still or take: no audio needed, pack generated (stray text, wipes)
  preview    review render (rough cut, timeline export): spec and loudness as warnings, pack secrets

With --slug and --stage the reports land in the record: checks/ci/<stage>/<file>.json (+ frames) and the stage index
checks/ci/<stage>.json. `videos.py set <slug> <stage> review|done` on master, formats, wide, clips, generate, qc and publish
refuses unless every file in that index is unchanged since its CI run and none is red (override: --skip-ci "<why>").
--forget removes a file from the index (a rejected take, a dropped format).

usage:
  uv run video_ci.py final/x_9x16.mp4 final/x_4x3.mp4 --slug launch-03 --stage formats --var hook="SHOOT WITHOUT A CREW" --var cta="example.com"
  uv run video_ci.py takes/sh010.t*.mp4 --profile generated --slug gv01 --stage generate
  uv run video_ci.py renders/rough_v1.mp4 --profile preview
  uv run video_ci.py takes/sh010.t02.mp4 --slug gv01 --stage generate --forget
  uv run video_ci.py final/x_9x16.mp4 --slug launch-03 --stage formats --var hook="..." --events specs/x.json --input row.json --input formats/9x16
Exit: 0 green, 2 amber, 1 red, 3 could not run.
Reference: ${CLAUDE_PLUGIN_ROOT}/references/ci.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import decide as D  # noqa: E402  (also sets utf-8 stdout)

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
EDGE_S = 0.5            # black / freeze / silence this close to the start or end = intended fade, ignored
VISUAL_CAP = 150        # stills per check at most: long masters get a wider step instead of a bigger bill
SCENE_THRESHOLD = 6.0   # scdet score (0-100) for a scene change; 6 found layout switches, cuts and graphic entrances on
                        # 3-min teaching videos (38 in 167 s) without firing on eased push-ins
MAX_EVENTS = 60         # transitions sampled per file at most (strongest scene changes first; --events always kept)
EVENT_OFFSETS = (0.1, 0.6)  # stills after each event: mid-transition (old and new text both on screen) and landed
MERGE_S = 0.4           # events closer than this are one transition
CI_STAGES = ("master", "formats", "wide", "clips", "generate", "qc", "publish")

PROFILES = {
    # spec: codec/pix/audio requirements; level: "fail" blocks (red), "unsure" flags (amber), None skips the check
    "short": {"codecs": ("h264", "hevc"), "pix": ("yuv420p",), "audio": "aac", "spec": "fail", "lufs": "fail",
              "black": "fail", "freeze": "unsure", "silence": "unsure", "pack": "short", "scenes": True},
    "clip": {"codecs": ("h264", "hevc"), "pix": ("yuv420p",), "audio": "aac", "spec": "fail", "lufs": "fail",
             "black": "fail", "freeze": "unsure", "silence": "unsure", "pack": "short", "scenes": True},
    "master": {"codecs": ("h264", "hevc", "prores", "dnxhd"), "pix": None, "audio": "any", "spec": "fail", "lufs": "fail",
               "black": "unsure", "freeze": "unsure", "silence": "unsure", "pack": "master",
               "scenes": True},
    "generated": {"codecs": None, "pix": None, "audio": None, "spec": "unsure", "lufs": None,
                  "black": "unsure", "freeze": None, "silence": None, "pack": "generated"},
    "preview": {"codecs": None, "pix": None, "audio": "any", "spec": "unsure", "lufs": "unsure",
                "black": "unsure", "freeze": None, "silence": "unsure", "pack": "secrets"},
}
LUFS_TARGET, LUFS_TOL, TP_MAX = -14.0, 0.5, -1.0   # platform delivery: -14 LUFS integrated, true peak at most -1 dBTP


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe(path):
    r = run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if r.returncode != 0:
        raise D.DecideError(f"ffprobe failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout)


def fps_of(s):
    try:
        a, b = s.get("avg_frame_rate", "0/1").split("/")
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def auto_profile(path, info):
    if D.media_kind(path) == "image":
        return "generated"
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    dur = float(info["format"].get("duration", 0) or 0)
    if v and int(v.get("height", 0)) > int(v.get("width", 1)):
        return "short"
    return "short" if dur < 180 else "master"


def check(cid, verdict, detail, tier="tech", at=None, **extra):
    return {"tier": tier, "id": cid, "verdict": verdict, "detail": detail, **({"at": at} if at else {}), **extra}


# ---------------------------------------------------------------- tech tier


def tech_checks(path, info, prof):
    P = PROFILES[prof]
    out = []
    streams = info["streams"]
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    dur = float(info["format"].get("duration", 0) or 0)
    if D.media_kind(path) == "image":
        ok = v is not None and int(v.get("width", 0)) > 0
        return [check("streams", "pass" if ok else "fail", f"image {v.get('width')}x{v.get('height')}" if ok else "unreadable image")], dur
    # spec
    problems = []
    if v is None:
        problems.append("no video stream")
    else:
        if P["codecs"] and v.get("codec_name") not in P["codecs"]:
            problems.append(f"video codec {v.get('codec_name')} (want {'/'.join(P['codecs'])})")
        if P["pix"] and v.get("pix_fmt") not in P["pix"]:
            problems.append(f"pixel format {v.get('pix_fmt')} (want {'/'.join(P['pix'])})")
        if int(v.get("width", 0)) % 2 or int(v.get("height", 0)) % 2:
            problems.append("odd frame size")
    if P["audio"] and a is None:
        problems.append("no audio stream")
    elif P["audio"] not in (None, "any") and a is not None and a.get("codec_name") != P["audio"]:
        problems.append(f"audio codec {a.get('codec_name')} (want {P['audio']})")
    desc = (f"{v.get('codec_name')} {v.get('pix_fmt')} {v.get('width')}x{v.get('height')} {fps_of(v):.3f} fps" if v else "no video")
    desc += f", {a.get('codec_name')} {a.get('sample_rate')} Hz" if a else ", no audio"
    desc += f", {dur:.2f} s"
    out.append(check("streams", P["spec"] if problems else "pass", "; ".join(problems) or desc))
    # A/V length
    if v is not None and a is not None:
        vd, ad = float(v.get("duration", dur) or dur), float(a.get("duration", dur) or dur)
        gap = abs(vd - ad)
        out.append(check("av_length", "unsure" if gap > 0.1 else "pass", f"video {vd:.2f} s, audio {ad:.2f} s"
                         + (" (audio and picture end apart)" if gap > 0.1 else "")))
    # loudness
    if P["lufs"] and a is not None:
        try:
            r = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af",
                     "ebur128=peak=true:framelog=quiet", "-f", "null", "-"])
            summ = r.stderr[r.stderr.rfind("Summary:"):]
            lufs = float(re.search(r"I:[ \t]+(-?[\d.]+) LUFS", summ).group(1))
            tp = float(re.search(r"Peak:[ \t]+(-?[\d.]+|-inf) dBFS", summ).group(1))
            bad = abs(lufs - LUFS_TARGET) > LUFS_TOL or tp > TP_MAX
            out.append(check("loudness", P["lufs"] if bad else "pass",
                             f"{lufs:.1f} LUFS, true peak {tp:.1f} dBTP (want {LUFS_TARGET:.0f} ±{LUFS_TOL}, ≤ {TP_MAX:.0f})",
                             lufs=lufs, true_peak=tp))
        except (AttributeError, ValueError) as e:
            out.append(check("loudness", "unsure", f"could not measure ({e})"))
    # black / freeze / silence in one decode
    wants = [k for k in ("black", "freeze", "silence") if P[k]]
    scenes = []
    if (wants or P.get("scenes")) and v is not None:
        vf = ["scale=320:-2"]
        if P["black"]:
            vf.append("blackdetect=d=0.2:pix_th=0.10")
        if P["freeze"]:
            vf.append("freezedetect=n=0.003:d=1.5")
        if P.get("scenes"):
            vf.append(f"scdet=threshold={SCENE_THRESHOLD}")
        cmd = [FFMPEG, "-hide_banner", "-nostats", "-hwaccel", "auto", "-threads", "0", "-i", str(path), "-vf", ",".join(vf)]
        if P["silence"] and a is not None:
            cmd += ["-af", "silencedetect=n=-50dB:d=1.5"]
        cmd += ["-f", "null", "-"]
        log = run(cmd).stderr

        def interior(spans):
            return [(s, e) for s, e in spans if s > EDGE_S and e < dur - EDGE_S]

        if P.get("scenes"):
            scenes = [(float(t), float(sc)) for sc, t in
                      re.findall(r"lavfi\.scd\.score: ([\d.]+), lavfi\.scd\.time: ([\d.]+)", log)]

        if P["black"]:
            spans = [(float(s), float(e)) for s, e in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
            bad = interior(spans)
            out.append(check("black_frames", P["black"] if bad else "pass",
                             ", ".join(f"{s:.2f}-{e:.2f} s" for s, e in bad) or "none inside the video",
                             at=[round(s, 2) for s, _ in bad]))
        if P["freeze"]:
            st = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
            en = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", log)] + [dur] * len(st)
            bad = interior(list(zip(st, en)))
            out.append(check("frozen_picture", P["freeze"] if bad else "pass",
                             ", ".join(f"{s:.2f}-{e:.2f} s" for s, e in bad) + " (a still screen or slide is fine)" if bad
                             else "none over 1.5 s", at=[round(s, 2) for s, _ in bad]))
        if P["silence"] and a is not None:
            st = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
            en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)] + [dur] * len(st)
            bad = interior(list(zip(st, en)))
            out.append(check("dead_air", P["silence"] if bad else "pass",
                             ", ".join(f"{s:.2f}-{e:.2f} s" for s, e in bad) or "no silence over 1.5 s",
                             at=[round(s, 2) for s, _ in bad]))
    return out, dur, scenes


def load_events(paths):
    """--events files -> seconds. A file holds a list of seconds, a list of {t|start|time, dur|duration} (graphics: the
    exit at t+dur counts too), or an object whose "events" / "graphics" / "segments" list is such a list."""
    out = []
    for p in paths or []:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = next((data[k] for k in ("events", "graphics", "transitions", "segments") if isinstance(data.get(k), list)), [])
        for e in data:
            if isinstance(e, (int, float)):
                out.append(float(e))
            elif isinstance(e, dict):
                t = next((e[k] for k in ("t", "start", "time", "rec_s") if isinstance(e.get(k), (int, float))), None)
                if t is None:
                    continue
                out.append(float(t))
                d = next((e[k] for k in ("dur", "duration") if isinstance(e.get(k), (int, float))), None)
                if d:
                    out.append(float(t) + float(d))
    return out


def transition_times(scenes, events, dur):
    """scene changes (t, score) + known events -> still times after each (strongest first, capped, merged)."""
    picked = []
    for t in [*sorted(events), *[t for t, _ in sorted(scenes, key=lambda x: -x[1])]]:
        if EDGE_S < t < dur - EDGE_S and all(abs(t - p) >= MERGE_S for p in picked) and len(picked) < MAX_EVENTS:
            picked.append(t)
    return sorted(picked), sorted({round(min(t + o, dur - 0.05), 2) for t in picked for o in EVENT_OFFSETS})


# ---------------------------------------------------------------- visual tier


def visual_checks(path, prof, dur, cli_vars, var_files, max_cost, frames_dir, use_cache=True, still_times=None):
    pack = PROFILES[prof]["pack"]
    spec = D.load_checks(pack)
    if "transitions" in spec.get("samples", {}):
        spec["samples"]["transitions"]["at"] = list(still_times or [])  # empty = the checks report "no times" (info)
    for name, smp in spec.get("samples", {}).items():  # long files: widen the step, keep the bill flat
        if smp.get("every") and dur and dur / float(smp["every"]) > VISUAL_CAP:
            smp["every"] = round(dur / VISUAL_CAP, 2)
    path = str(Path(path).resolve())
    skip, out = set(), []
    for c in spec["checks"]:
        names = re.findall(r"\{([A-Za-z_][\w.]*)\}", c.get("ask", ""))
        absent = [n for n in names if cli_vars.get(n) == "-"]
        if absent:  # --var cta=- : this video has none (teaching video without a call to action)
            skip.add(c["id"])
            out.append(check(c["id"], "info", f"skipped: --var {absent[0]}=- (this video has none)", tier="visual"))
            continue
        missing = [n for n in names if n not in cli_vars and not any(n.startswith(f + ".") for f in var_files)]
        if missing:
            skip.add(c["id"])
            if not (prof == "clip" and missing == ["cta"]):  # clips often end without a call to action
                out.append(check(c["id"], "unsure", f"not checked: pass --var {missing[0]}=\"...\"", tier="visual"))
    est = D.run_checks(spec, media=str(path), cli_vars=cli_vars, var_files=var_files, skip=skip or None, dry_run=True)
    cost = est["usage"]["est_tokens"] * D.PRICE_PER_TOKEN
    if cost > max_cost:
        return out + [check("visual", "unsure", f"skipped: estimate ${cost:.3f} is over --max-cost ${max_cost:.2f}",
                            tier="visual")], {"est_cost_usd": round(cost, 4)}
    rep = D.run_checks(spec, media=str(path), cli_vars=cli_vars, var_files=var_files, skip=skip or None, use_cache=use_cache)
    D.save_frames(rep, frames_dir)
    for r in rep["results"]:
        w = r.get("worst") or {}
        val = w.get("value")
        detail = (f"{D.show(next(p['answer'] for p in r['per_input'] if p.get('at') == w.get('at') and p.get('answer')))}"
                  if w and any(p.get("answer") for p in r["per_input"]) else "")
        if r["verdict"] in ("fail", "unsure") and r["failing_at"]:
            detail += f" at {', '.join(str(x) for x in r['failing_at'][:6])}" + (" ..." if len(r["failing_at"]) > 6 else "")
        if r["verdict"] == "error":
            err = next((p.get("error") for p in r["per_input"] if p.get("error")), "")
            detail = f"could not run: {err[:160]}"
        if r.get("note"):
            detail = r["note"]
        frames = sorted({f for p in r["per_input"] if p.get("verdict") in ("fail", "unsure") for f in p.get("frames", [])})
        verdict = r["verdict"]
        soft = next((c.get("soft") for c in spec["checks"] if c["id"] == r["id"]), False)
        if soft and verdict == "fail":  # taste-adjacent checks flag for the human, never block
            verdict, detail = "unsure", f"{detail.strip()} (soft check: shown, not blocking)"
        out.append(check(r["id"], verdict, detail.strip() or verdict, tier="visual", ask=r.get("ask"),
                         expect=r.get("expect"), worst=w, frames=frames[:12]))
    return out, rep["usage"]


# ---------------------------------------------------------------- record index


def record_dir(slug):
    import videos as V  # same folder; resolves the project from the current directory or $VIDEOS_ROOT
    vm = V.one(slug)[0]
    return vm.parent


def update_index(rec, stage, entry=None, forget=None):
    idx_path = rec / "checks" / "ci" / f"{stage}.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"stage": stage, "files": []}
    files = [f for f in idx["files"] if f["file"] != (entry or {}).get("file") and f["file"] != forget]
    if entry:
        files.append(entry)
    idx["files"] = sorted(files, key=lambda f: f["file"])
    idx["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    return idx_path


# ---------------------------------------------------------------- main


def ci_file(path, a, cli_vars, var_files, out_dir):
    t0 = time.time()
    info = probe(path)
    prof = a.profile if a.profile != "auto" else auto_profile(path, info)
    checks, dur, scenes = tech_checks(path, info, prof)
    events = load_events(a.events)
    picked, still_times = transition_times(scenes, events, dur) if (scenes or events) else ([], [])
    if PROFILES[prof].get("scenes") or events:
        checks.append(check("transitions", "info", f"{len(scenes)} scene changes found, {len(events)} given; "
                            f"{len(picked)} sampled (0.1 s and 0.6 s after each)", at=[round(t, 2) for t in picked]))
    usage = {}
    stem = Path(path).stem
    frames_dir = out_dir / f"{stem}_frames"
    if a.no_visual:
        checks.append(check("visual", "unsure", f"skipped by the agent: {a.no_visual}", tier="visual"))
    else:
        try:
            vchecks, usage = visual_checks(path, prof, dur, cli_vars, var_files, a.max_cost, frames_dir, not a.no_cache,
                                           still_times)
            checks += vchecks
        except D.DecideError as e:
            checks.append(check("visual", "fail", f"visual tier could not run: {e} (fix it, or --no-visual \"<why>\")", tier="visual"))
    inputs = input_prints(a)
    verdicts = [c["verdict"] for c in checks]
    verdict = "red" if ("fail" in verdicts or "error" in verdicts) else "amber" if "unsure" in verdicts else "green"
    st = Path(path).stat()
    report = {"file": str(Path(path).resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns, "profile": prof,
              "verdict": verdict, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
              "usage": usage, "vars": cli_vars, "inputs": inputs, "checks": checks,
              "frames_dir": str(frames_dir) if frames_dir.exists() else None}
    out_dir.mkdir(parents=True, exist_ok=True)
    rpath = out_dir / f"{stem}.json"
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    report["report"] = str(rpath)
    return report


def input_prints(a):
    """--input paths + --manifest inputs -> [{path, size, mtime_ns} | {path, files, fingerprint}] (videos.fingerprint)."""
    import videos as V
    paths = list(a.input or [])
    for m in a.manifest or []:
        data = json.loads(Path(m).read_text(encoding="utf-8"))
        base = Path(m).resolve().parent
        for p in (data.get("inputs", []) if isinstance(data, dict) else data):
            p = p.get("path") if isinstance(p, dict) else p
            paths.append(str(p if Path(p).is_absolute() else base / p))
    out = []
    for p in paths:
        fp = V.fingerprint(p)
        if fp is None:
            raise D.DecideError(f"--input not found: {p}")
        out.append(fp)
    return out


def print_report(r):
    cost = r["usage"].get("cost_usd")
    print(f"\n{Path(r['file']).name}  {r['verdict'].upper()}  ({r['profile']}, {r['seconds']} s"
          + (f", ${cost:.4f}" if cost is not None else "") + ")")
    for c in r["checks"]:
        mark = {"pass": "pass  ", "fail": "FAIL  ", "unsure": "LOOK  ", "error": "ERROR ", "info": "info  "}.get(c["verdict"], c["verdict"])
        print(f"  {mark}{c['tier']:<6} {c['id']:<17} {c['detail']}")
    if r.get("inputs"):
        print(f"  inputs: {len(r['inputs'])} recorded (the gate refuses review/done once one changes)")
    if r.get("frames_dir"):
        print(f"  frames: {r['frames_dir']}")
    print(f"  report: {r['report']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("files", nargs="+")
    ap.add_argument("--profile", default="auto", choices=["auto", *PROFILES])
    ap.add_argument("--slug", help="video record: reports go to its checks/ci/ and feed the videos.py gate")
    ap.add_argument("--stage", choices=CI_STAGES, help="stage these files belong to (with --slug)")
    ap.add_argument("--var", action="append", help="name=value for the pack ({hook}, {cta}); name=- = the video "
                    "has none (skips that check as info)")
    ap.add_argument("--vars", action="append", help="name=file.json: its keys become {name.key}")
    ap.add_argument("--out", help="report folder without --slug (default: <file folder>/ci)")
    ap.add_argument("--max-cost", type=float, default=0.50, help="skip the visual tier (amber) above this estimate per file")
    ap.add_argument("--no-visual", metavar="WHY", help="run the tech tier only; the reason is recorded (verdict at best amber)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--forget", action="store_true", help="remove the files from the stage index (rejected take, dropped format)")
    ap.add_argument("--events", action="append", help="JSON of known transition times: seconds, or {t|start, dur} items "
                    "(graphics specs, cut lists); sampled with the scene changes")
    ap.add_argument("--input", action="append", help="a file or folder this render was built from (repeat); the gate "
                    "refuses review/done once it changes")
    ap.add_argument("--manifest", action="append", help="JSON listing inputs: {\"inputs\": [paths]} or [paths], relative "
                    "to the manifest")
    a = ap.parse_args(argv)
    if a.slug and not a.stage:
        ap.error("--slug needs --stage")
    cli_vars = dict(v.split("=", 1) for v in a.var or [])
    var_files = dict(v.split("=", 1) for v in a.vars or [])
    rec = None
    try:
        rec = record_dir(a.slug) if a.slug else None
    except SystemExit as e:
        print(f"video_ci: {e}", file=sys.stderr)
        return 3
    if a.forget:
        if not rec:
            ap.error("--forget needs --slug and --stage")
        for f in a.files:
            update_index(rec, a.stage, forget=str(Path(f).resolve()))
            print(f"forgotten from {a.stage}: {f}")
        return 0
    seen, tally = set(), {}
    for f in a.files:
        if not Path(f).exists():
            print(f"video_ci: not found: {f}", file=sys.stderr)
            seen.add("error")
            continue
        out_dir = (rec / "checks" / "ci" / a.stage) if rec else Path(a.out or Path(f).resolve().parent / "ci")
        try:
            r = ci_file(f, a, cli_vars, var_files, out_dir)
        except D.DecideError as e:
            print(f"video_ci: {f}: {e}", file=sys.stderr)
            seen.add("error")
            continue
        print_report(r)
        if rec:
            why = "; ".join(f"{c['id']}: {c['detail']}" for c in r["checks"] if c["verdict"] in ("fail", "error"))[:300]
            look = "; ".join(f"{c['id']}" for c in r["checks"] if c["verdict"] == "unsure")
            idx = update_index(rec, a.stage, {"file": r["file"], "size": r["size"], "mtime_ns": r["mtime_ns"],
                                               "verdict": r["verdict"], "profile": r["profile"], "ran_at": r["ran_at"],
                                               "report": r["report"], "why": why, "look": look, "inputs": r["inputs"]})
            print(f"  index: {idx}")
        seen.add(r["verdict"])
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    if len(a.files) > 1:
        print("\nCI: " + ", ".join(f"{tally.get(v, 0)} {v}" for v in ("green", "amber", "red")))
    return 1 if "red" in seen else 3 if "error" in seen else 2 if "amber" in seen else 0


if __name__ == "__main__":
    sys.exit(main())
