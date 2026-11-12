# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Video project records for the videos plugin. Stdlib only, works in any folder.

One record per video: <project>/.agents/videos/<slug>/video.md (flat frontmatter, one key per stage, then
## Decisions / ## Notes / ## Log). Published files: <project>/artifacts/videos/<slug>/release.md + index.md.
Spec: ${CLAUDE_PLUGIN_ROOT}/references/records.md
Video CI gate: `set` to review/done on master, formats, clips, generate, qc or publish needs the files tested by
video_ci.py --slug --stage to be unchanged since and not red (references/ci.md); --skip-ci "<why>" passes it, logged.

Project root = $VIDEOS_ROOT, else the nearest folder at or above the current one holding videos.json, else the
current folder. videos.json (optional; videos.local.json beside it overrides it, keep machine paths there):
  {"records": ".agents/videos", "published": "artifacts/videos", "media_root": "/mnt/media", "workspaces": "clients/*"}
"workspaces" = glob of sub-projects that each hold their own records (several clients/brands in one tree).
$VIDEOS_MEDIA_ROOT overrides media_root.

  python videos.py where
  python videos.py init [--media-root PATH] [--workspaces GLOB]
  python videos.py board [--ws W] [--wide] [--all]
  python videos.py show <query>
  python videos.py new <slug> --title T --kind K [--ws W] [--campaign C] [--content IDS] [--style P] [--code P] [--media P] [--talent N] [--next N]
  python videos.py set <slug> <stage|key> "<value>" [--next "<next step>"] [--skip-ci "<why>"]
  python videos.py note <slug> "<text>"
  python videos.py release <slug>
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
STAGES = ["brief", "generate", "ingest", "transcribe", "rough", "tighten", "audio", "grade", "master", "formats", "clips", "qc", "publish"]
KINDS = {
    "talking-head": ["brief", "ingest", "transcribe", "rough", "tighten", "audio", "grade", "master", "formats", "qc", "publish"],
    "clip-batch": ["brief", "transcribe", "rough", "audio", "grade", "clips", "qc", "publish"],
    "clip": ["brief", "transcribe", "rough", "audio", "grade", "formats", "qc", "publish"],
    "motion": ["brief", "audio", "formats", "qc", "publish"],
    "generative": ["brief", "generate", "audio", "formats", "qc", "publish"],
}
HEAD = ["title", "slug", "workspace", "kind", "campaign", "content", "talent", "style", "code", "media", "resolve"]
ABBR = {"brief": "brf", "generate": "gen", "ingest": "ing", "transcribe": "stt", "rough": "rgh", "tighten": "tgt", "audio": "aud", "grade": "grd",
        "master": "mst", "formats": "fmt", "clips": "clp", "qc": "qc", "publish": "pub"}
MARK = {"done": "✓", "skip": "·", "todo": "-", "doing": "▶", "review": "R", "blocked": "✕"}
STATUS = ("todo", "doing", "review", "done", "skip", "blocked")
TODAY = datetime.date.today().isoformat()
DEFAULTS = {"records": ".agents/videos", "published": "artifacts/videos", "media_root": "", "workspaces": ""}


def find_root():
    env = os.environ.get("VIDEOS_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        if (d / "videos.json").is_file():
            return d
    return here


ROOT = find_root()


def load_config():
    cfg = dict(DEFAULTS)
    for name in ("videos.json", "videos.local.json"):
        p = ROOT / name
        if p.is_file():
            try:
                cfg.update(json.loads(p.read_text(encoding="utf-8")))
            except ValueError as e:
                sys.exit(f"{p}: invalid JSON ({e})")
    if os.environ.get("VIDEOS_MEDIA_ROOT"):
        cfg["media_root"] = os.environ["VIDEOS_MEDIA_ROOT"]
    cfg["media_root"] = (cfg.get("media_root") or "").rstrip("/\\")
    return cfg


CFG = load_config()


def workspaces():
    """[(name, dir)]: every workspace when "workspaces" is set, else the project root alone."""
    if not CFG.get("workspaces"):
        return [(ROOT.name, ROOT)]
    dirs = sorted(Path(p) for p in glob.glob(str(ROOT / CFG["workspaces"])) if Path(p).is_dir())
    return [(d.name, d) for d in dirs]


def current_ws():
    here = Path.cwd().resolve()
    for name, d in workspaces():
        if here == d.resolve() or d.resolve() in here.parents:
            return name
    return None


def rel(p):
    try:
        return Path(p).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(p).as_posix()


def records(ws=None):
    out = []
    for name, d in workspaces():
        if ws and name.lower() != ws.lower():
            continue
        for vm in sorted((d / CFG["records"]).glob("*/video.md")):
            out.append((vm, parse(vm)[0], name, d))
    return out


def parse(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "#")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2)


def dump(meta, body):
    keys = [k for k in HEAD if k in meta] + [s for s in STAGES if s in meta]
    keys += [k for k in meta if k not in keys and k not in ("next", "updated")] + [k for k in ("next", "updated") if k in meta]
    return "---\n" + "".join(f"{k}: {meta[k]}\n" for k in keys) + "---\n" + body


def word(v):
    return (v or "todo").split()[0].lower()


def current(meta):
    for s in STAGES:
        if s in meta and word(meta[s]) not in ("done", "skip"):
            return s
    return "publish" if word(meta.get("publish")) == "done" else "-"


def find(query):
    q = query.lower().strip()
    recs = records()
    exact = [r for r in recs if r[1].get("slug", "").lower() == q or r[0].parent.name.lower() == q]
    if exact:
        return exact
    toks = q.replace("-", " ").split()
    return [r for r in recs if all(t in (r[1].get("slug", "") + " " + r[1].get("title", "")).lower() for t in toks)]


def one(query):
    hits = find(query)
    if len(hits) != 1:
        names = ", ".join(h[1].get("slug", h[0].parent.name) for h in hits[:8])
        sys.exit(f"'{query}' matches {len(hits)} records" + (f": {names}" if names else " (videos.py board --all)"))
    return hits[0]


def cmd_where(a):
    print(f"root: {ROOT}")
    print(f"config: {', '.join(n for n in ('videos.json', 'videos.local.json') if (ROOT / n).is_file()) or 'none (defaults)'}")
    print(f"records: <workspace>/{CFG['records']}   published: <workspace>/{CFG['published']}")
    print(f"media_root: {CFG['media_root'] or '(not set: heavy media stays wherever the sources are)'}")
    for name, d in workspaces():
        print(f"- {name}: {rel(d) or '.'} ({len(list((d / CFG['records']).glob('*/video.md')))} record(s))")


def cmd_init(a):
    p = Path.cwd() / "videos.json"
    if p.exists():
        sys.exit(f"exists: {p}")
    cfg = {"records": DEFAULTS["records"], "published": DEFAULTS["published"]}
    if a.workspaces:
        cfg["workspaces"] = a.workspaces
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {p}")
    if a.media_root:
        lp = Path.cwd() / "videos.local.json"
        lp.write_text(json.dumps({"media_root": a.media_root}, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {lp} (machine-specific: keep it out of version control)")


def cmd_board(a):
    recs = records(a.ws)
    if not a.all:
        recs = [r for r in recs if word(r[1].get("publish")) != "done"]
    if not recs:
        print("No video records" + (f" in {a.ws}" if a.ws else "") + f" under {ROOT}. Start one with /videos:new.")
        return
    rows = []
    for vm, m, ws, _ in recs:
        st = current(m)
        stages = " ".join(f"{ABBR[s]}{MARK.get(word(m[s]), '?')}" for s in STAGES if s in m and word(m[s]) != "skip")
        rows.append((m.get("workspace", ws), m.get("campaign", ""), m.get("slug", vm.parent.name), m.get("kind", ""),
                     st, m.get(st, ""), m.get("next", ""), stages))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    last = None
    for ws, camp, slug, kind, st, val, nxt, stages in rows:
        if (ws, camp) != last:
            print(f"\n## {ws}" + (f" · {camp}" if camp else ""))
            last = (ws, camp)
        print(f"- {slug} [{kind}] → {st}: {val or 'todo'}" + (f"\n    next: {nxt}" if nxt else ""))
        if a.wide:
            print(f"    {stages}")
    print(f"\n{len(rows)} video(s). Legend (--wide): ✓ done · R review · ▶ doing · - todo · ✕ blocked")


def cmd_show(a):
    hits = find(a.query)
    if not hits:
        sys.exit(f"no video matches '{a.query}' (videos.py board --all)")
    if len(hits) > 1:
        print("Several matches:")
        for vm, m, ws, _ in hits:
            print(f"- {m.get('slug', vm.parent.name)}  ({m.get('title', '')}, {ws})")
        return
    vm, m, _, _ = hits[0]
    print(f"record: {rel(vm)}")
    print(f"current stage: {current(m)}")
    for stage in CI_GATED:
        idx = vm.parent / "checks" / "ci" / f"{stage}.json"
        if idx.is_file():
            ok, msg = ci_gate(vm.parent, stage)
            print(f"CI {stage}: {msg}")
    print(vm.read_text(encoding="utf-8"))


def cmd_new(a):
    if a.kind not in KINDS:
        sys.exit(f"kind must be one of {', '.join(KINDS)}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.slug):
        sys.exit("slug = lowercase letters, digits and hyphens (it names folders and published files)")
    wss = workspaces()
    ws = a.ws or current_ws() or (wss[0][0] if len(wss) == 1 else None)
    match = [w for w in wss if ws and w[0].lower() == ws.lower()]
    if not match:
        sys.exit("pick a workspace with --ws: " + ", ".join(n for n, _ in wss))
    ws, wsdir = match[0]
    taken = [r for r in records() if r[0].parent.name == a.slug]
    if taken:
        sys.exit(f"a record named {a.slug} already exists: {rel(taken[0][0])} (slugs are unique across workspaces)")
    d = wsdir / CFG["records"] / a.slug
    for sub in ("cut", "checks"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    meta = {"title": a.title, "slug": a.slug, "workspace": ws, "kind": a.kind}
    for k in ("campaign", "content", "talent", "style", "code", "media", "resolve"):
        if getattr(a, k, None):
            meta[k] = getattr(a, k)
    if "media" not in meta and CFG["media_root"]:
        meta["media"] = f"{CFG['media_root']}/{ws}/videos/{a.slug}"
    for s in STAGES:
        meta[s] = "todo" if s in KINDS[a.kind] else "skip"
    meta["brief"] = f"done {TODAY}"
    meta["next"] = a.next or f"/videos:{KINDS[a.kind][1]}"
    meta["updated"] = TODAY
    body = f"\n## Decisions\n\n## Notes\n\n## Log\n- {TODAY} created ({a.kind})\n"
    (d / "video.md").write_text(dump(meta, body), encoding="utf-8")
    print(f"created {rel(d / 'video.md')}" + (f"\nmedia: {meta['media']}" if "media" in meta else ""))


CI_GATED = ("master", "formats", "clips", "generate", "qc", "publish")  # review/done here = a human sees or ships files
CI_FINALS = ("master", "formats", "clips")  # what qc and publish answer for


def ci_gate(rec_dir, stage):
    """Video CI as a required check: files tested by video_ci.py --slug --stage, unchanged since, none red.
    -> (ok, one-line summary)"""
    ci = rec_dir / "checks" / "ci"
    names = [stage] if stage not in ("qc", "publish") else [s for s in (*CI_FINALS, "qc") if (ci / f"{s}.json").is_file()]
    entries = []
    for n in names:
        p = ci / f"{n}.json"
        if p.is_file():
            entries += json.loads(p.read_text(encoding="utf-8")).get("files", [])
    if not entries:
        return False, (f"no CI report for {stage}: run video_ci.py <files> --slug <slug> --stage "
                       f"{stage if stage not in ('qc', 'publish') else '<master|formats|clips>'} first")
    bad, count = [], {"green": 0, "amber": 0, "red": 0}
    for e in entries:
        p = Path(e["file"])
        count[e.get("verdict", "red")] = count.get(e.get("verdict", "red"), 0) + 1
        if not p.is_file():
            bad.append(f"{p.name}: file gone (re-render, or video_ci.py --forget)")
            continue
        st = p.stat()
        if st.st_size != e.get("size") or st.st_mtime_ns != e.get("mtime_ns"):
            bad.append(f"{p.name}: changed after its CI run (run video_ci.py on it again)")
        elif e.get("verdict") == "red":
            bad.append(f"{p.name}: RED: {e.get('why', '')}")
    summary = f"{count['green']} green, {count['amber']} amber, {count['red']} red"
    if bad:
        return False, summary + "\n  " + "\n  ".join(bad)
    return True, summary + (f" (amber: show the flagged frames, {sum(1 for e in entries if e.get('verdict') == 'amber')} file(s))"
                            if count["amber"] else "")


def cmd_set(a):
    vm, _, _, _ = one(a.slug)
    if a.key in STAGES and word(a.value) not in STATUS:
        sys.exit(f"a stage value starts with one of: {', '.join(STATUS)} (got '{a.value}')")
    ci_note = ""
    if a.key in CI_GATED and word(a.value) in ("review", "done"):
        ok, msg = ci_gate(vm.parent, a.key)
        if a.skip_ci:
            ci_note = f" (CI skipped: {a.skip_ci}; {msg.splitlines()[0]})"
        elif not ok:
            sys.exit(f"video CI blocks {a.key} = {word(a.value)}: {msg}\n"
                     f"Fix and re-run video_ci.py, or pass --skip-ci \"<why>\" (logged in the record).")
        else:
            ci_note = f" (CI: {msg})"
    meta, body = parse(vm)
    meta[a.key] = a.value
    if a.next is not None:
        meta["next"] = a.next
    meta["updated"] = TODAY
    line = f"- {TODAY} {a.key}: {a.value}{ci_note}" + (f" → next: {a.next}" if a.next else "")
    body = body.rstrip("\n") + "\n" + line + "\n" if "## Log" in body else body.rstrip("\n") + f"\n\n## Log\n{line}\n"
    vm.write_text(dump(meta, body), encoding="utf-8")
    print(f"{meta.get('slug')}: {a.key} = {a.value}" + (f"; next = {a.next}" if a.next else ""))


def cmd_note(a):
    vm, _, _, _ = one(a.slug)
    meta, body = parse(vm)
    line = f"- {TODAY} {a.text}"
    if "## Notes" in body:
        head, rest = body.split("## Notes", 1)
        nxt = rest.find("\n## ")
        notes, tail = (rest, "") if nxt < 0 else (rest[:nxt], rest[nxt:])
        body = head + "## Notes" + notes.rstrip("\n") + "\n" + line + "\n" + tail
    else:
        body = body.rstrip("\n") + f"\n\n## Notes\n{line}\n"
    meta["updated"] = TODAY
    vm.write_text(dump(meta, body), encoding="utf-8")
    print(f"{meta.get('slug')}: note added")


def cmd_release(a):
    vm, m, ws, wsdir = one(a.slug)
    slug = m.get("slug", vm.parent.name)
    pub = wsdir / CFG["published"]
    out = pub / slug
    out.mkdir(parents=True, exist_ok=True)
    src = vm.relative_to(wsdir).as_posix()
    relmd = out / "release.md"
    if not relmd.exists():
        head = {"title": m.get("title", slug), "slug": slug, "campaign": m.get("campaign", ""), "content": m.get("content", ""),
                "source": src, "formats": "", "platforms": "", "post_date": "", "status": "ready",
                "posted": "", "loudness": "", "qc": ""}
        relmd.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in head.items()) +
                         "---\n\n## Caption\n\n## Hashtags\n\n## Notes\n", encoding="utf-8")
    index = pub / "index.md"
    if not index.exists():
        index.write_text("Published videos. One folder per video (release.md + upload-ready files); how it was made = "
                         "`source:` in release.md.\n\n| Video | Campaign | Formats | Status | Record |\n|---|---|---|---|---|\n",
                         encoding="utf-8")
    text = index.read_text(encoding="utf-8")
    if f"[{slug}]" not in text:
        index.write_text(text.rstrip("\n") + f"\n| [{slug}]({slug}/release.md) | {m.get('campaign', '')} |  | ready | `{src}` |\n",
                         encoding="utf-8")
    print(f"publish folder: {rel(out)}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[1:]))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("where", help="show the project root, config and workspaces")
    i = sub.add_parser("init", help="write videos.json here")
    i.add_argument("--media-root")
    i.add_argument("--workspaces")
    b = sub.add_parser("board", help="every video in progress")
    b.add_argument("--ws")
    b.add_argument("--wide", action="store_true")
    b.add_argument("--all", action="store_true", help="include published videos")
    s = sub.add_parser("show", help="print one record")
    s.add_argument("query")
    n = sub.add_parser("new", help="create a record")
    n.add_argument("slug")
    n.add_argument("--title", required=True)
    n.add_argument("--kind", required=True, choices=list(KINDS))
    for k in ("ws", "campaign", "content", "talent", "style", "code", "media", "resolve", "next"):
        n.add_argument(f"--{k}")
    t = sub.add_parser("set", help="set a stage status or field, append a log line")
    t.add_argument("slug")
    t.add_argument("key")
    t.add_argument("value")
    t.add_argument("--next")
    t.add_argument("--skip-ci", metavar="WHY", help="pass the video CI gate anyway; the reason goes in the log")
    o = sub.add_parser("note", help="add a dated line under ## Notes")
    o.add_argument("slug")
    o.add_argument("text")
    r = sub.add_parser("release", help="create the published folder + index row")
    r.add_argument("slug")
    a = p.parse_args()
    {"where": cmd_where, "init": cmd_init, "board": cmd_board, "show": cmd_show, "new": cmd_new, "set": cmd_set,
     "note": cmd_note, "release": cmd_release}[a.cmd](a)


if __name__ == "__main__":
    main()
