# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Hook x CTA variants of a finished HyperFrames short, for ad A/B tests, without touching the project or its builder.

Per variant: clone the built project (big media hard-linked, small text files copied, .hyperframes/ and renders/
left out), replace the inner HTML of each slot element (`#id` selector) in the clone's index.html, run
`hyperframes check --at-transitions --json` and keep only the findings that are NEW against the same check of the
unchanged project (a longer hook can overflow its box), render the clone, then give it the base's audio:
  --audio-from <base final.mp4>  copy that file's audio (already loudness-solved) onto the variant picture, stream
                                 copy, after checking the two pictures match within one frame
  (otherwise)                    loudness.py fix on the render (-14 LUFS, like any shipped file)
New check errors block that variant's render (--render-anyway overrides). Renders run one at a time (HyperFrames
holds a per-user render lock anyway) and wait for free RAM first (--min-ram-gb).

Variants file:
  {"slots": {"hook": {"selector": "#hook-line"}, "cta": {"selector": "#cta-line"}},
   "variants": [{"id": "h2", "hook": "Inner <span class=\\"tone2\\">HTML</span> or text", "cta": "..."},
                {"id": "h3", "hook": {"html": "...", "text": "plain text for CI"}}]}
A slot a variant leaves out keeps the base content. Ids: letters, digits, - and _ ("base" is reserved). The plain
text of each value (tags stripped, <br> = space) is what video_ci.py --var <slot>=... expects.

Outputs (in --out, which must sit outside the project):
  <name>_<id>.mp4          shipped variant (name = --name, else the --audio-from file's stem, else the project folder)
  renders/<name>_<id>.mp4  raw HyperFrames render (its own audio)
  clones/<project>__<id>/  the clone that was checked and rendered (clones/<project>__base = the unchanged check)
  checks/<id>.json         full check reports;  logs/<id>.{check,render}.log
  variants.json            base + every variant: slot html and text, CI vars, output, new check findings, audio
Reruns skip a variant whose output is newer than its inputs (its index.html, the project files, --audio-from) and
matches what variants.json recorded. Needs hyperframes, ffmpeg and ffprobe on PATH (env HYPERFRAMES, FFMPEG, FFPROBE
override; HYPERFRAMES may be a full command such as "node /path/to/cli.js").

usage:
  uv run variants.py <project dir> variants.json --out <dir> --audio-from <final.mp4>
  uv run variants.py <project dir> --selector "#hook-line" --set h2="Another <b>hook</b>" --set h3="..." --out <dir>
  options: --only h2,h3  --check-only  --no-check  --render-anyway  --fps 24000/1001  --name x_9x16
           --render-arg=--workers=3 (repeatable)  --check-arg=--budget=8m (repeatable)  --min-ram-gb 4
Then: uv run video_ci.py <out>/<name>_<id>.mp4 --var hook="<text from variants.json>" [--slug ... --stage formats]
Exit: 0 every variant done or fresh, 1 a variant blocked or failed, 2 bad input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
HERE = Path(__file__).resolve().parent
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP_TOP = {".hyperframes", "renders"}       # project state and default render folder never go into a clone
TEXT_SUFFIX = {".html", ".htm", ".json", ".js", ".mjs", ".css", ".md", ".txt", ".svg", ".xml"}
LINK_MIN = 1 << 20                           # smaller files are copied: a tool writing one in place can't reach the base
CHECK_SECTIONS = ("lint", "runtime", "layout", "motion", "contrast", "craft", "audio")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class VariantError(Exception):
    pass


# ---------------------------------------------------------------- HTML slots


class _Finder(HTMLParser):
    """Source offsets of the inner HTML of every element whose id is `elem_id` (explicit end tag required)."""

    def __init__(self, src, elem_id):
        super().__init__(convert_charrefs=False)
        self.line_starts = [0] + [i + 1 for i, c in enumerate(src) if c == "\n"]
        self.elem_id, self.stack, self.cur, self.found, self.count = elem_id, None, None, [], 0

    def _off(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        hit = dict(attrs).get("id") == self.elem_id
        if self.stack is not None:
            self.count += hit
            if tag not in VOID:
                self.stack.append(tag)
            return
        if not hit:
            return
        self.count += 1
        if tag in VOID:
            raise VariantError(f"#{self.elem_id} is a <{tag}>: it has no inner HTML")
        start = self._off()
        self.cur = (tag, start + len(self.get_starttag_text()))
        self.stack = [tag]

    def handle_startendtag(self, tag, attrs):
        if dict(attrs).get("id") == self.elem_id:
            raise VariantError(f"#{self.elem_id} is self-closing: it has no inner HTML")

    def handle_endtag(self, tag):
        if self.stack is None or tag in VOID or tag not in self.stack:
            return
        while self.stack.pop() != tag:
            pass
        if not self.stack:
            self.found.append((self.cur[1], self._off()))
            self.stack = None


def find_inner(src, elem_id):
    f = _Finder(src, elem_id)
    f.feed(src)
    f.close()
    if f.count != 1 or len(f.found) != 1:
        raise VariantError(f"#{elem_id}: {f.count} element(s) with that id, {len(f.found)} closed; need exactly one")
    return f.found[0]


class _Fragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text, self.errors = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.text.append(" ")
        elif tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes nothing open" if tag not in self.stack else f"</{tag}> skips <{self.stack[-1]}>")
            if tag in self.stack:
                while self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def fragment(value):
    """(html, plain text) of a slot value; refuses HTML whose tags don't balance (it would break the page)."""
    if isinstance(value, dict):
        html, text = value.get("html"), value.get("text")
    else:
        html, text = value, None
    if not isinstance(html, str):
        raise VariantError(f"slot value must be a string or {{\"html\": ...}}: {value!r}")
    p = _Fragment()
    p.feed(html)
    p.close()
    errs = p.errors + [f"<{t}> never closed" for t in p.stack]
    if errs:
        raise VariantError(f"unbalanced HTML in {html!r}: {'; '.join(errs)}")
    return html, text if isinstance(text, str) else " ".join("".join(p.text).split())


def substitute(src, slots, values):
    """src with each slot's inner HTML replaced (values: slot -> html). Splices from the end so offsets stay valid."""
    spans = []
    for name, sel in slots.items():
        if name in values:
            a, b = find_inner(src, sel)
            spans.append((a, b, values[name]))
    spans.sort(reverse=True)
    for later, earlier in zip(spans, spans[1:], strict=False):
        if earlier[1] > later[0]:
            raise VariantError("two slots overlap (one sits inside the other)")
    out = src
    for a, b, html in spans:
        out = out[:a] + html + out[b:]
    for name, sel in slots.items():  # the result must parse back to exactly what was asked
        if name in values:
            a, b = find_inner(out, sel)
            if out[a:b] != values[name]:
                raise VariantError(f"slot {name}: substitution did not round-trip")
    return out


def element_id(selector):
    m = re.fullmatch(r"#([A-Za-z_][\w:.-]*)", selector.strip())
    if not m:
        raise VariantError(f"selector {selector!r}: only #id selectors are supported")
    return m.group(1)


# ---------------------------------------------------------------- clone


def walk_files(root):
    for dp, dns, fns in os.walk(root):
        rel = Path(dp).relative_to(root)
        if rel == Path("."):
            dns[:] = [d for d in dns if d not in SKIP_TOP]
        for fn in fns:
            yield rel / fn


def newest_mtime(root):
    return max(((root / r).stat().st_mtime for r in walk_files(root)), default=0.0)


def sync_clone(base, dst):
    """Mirror base into dst except index.html (the caller writes it). Big media = hard links (copy if the volume
    refuses), small and text files = copies. Never writes through an existing link."""
    stats = {"linked": 0, "copied": 0, "kept": 0, "removed": 0, "copied_bytes": 0}
    want = set()
    for rel in walk_files(base):
        if rel == Path("index.html"):
            continue
        want.add(rel)
        p, q = base / rel, dst / rel
        sp = p.stat()
        link = sp.st_size >= LINK_MIN and p.suffix.lower() not in TEXT_SUFFIX
        if q.exists():
            sq = q.stat()
            if (link and os.path.samefile(p, q)) or (not link and sq.st_size == sp.st_size and sq.st_mtime_ns == sp.st_mtime_ns
                                                     and not os.path.samefile(p, q)):
                stats["kept"] += 1
                continue
            q.unlink()
        q.parent.mkdir(parents=True, exist_ok=True)
        if link:
            try:
                os.link(p, q)
                stats["linked"] += 1
                continue
            except OSError:
                pass
        shutil.copy2(p, q)
        stats["copied"] += 1
        stats["copied_bytes"] += sp.st_size
    for rel in list(walk_files(dst)):
        if rel != Path("index.html") and rel not in want:
            (dst / rel).unlink()
            stats["removed"] += 1
    return stats


def write_if_changed(path, data: bytes):
    if path.exists() and path.read_bytes() == data:
        return False
    if path.exists():
        path.unlink()  # never write through a hard link
    path.write_bytes(data)
    return True


# ---------------------------------------------------------------- tools


def hf_cmd():
    env = os.environ.get("HYPERFRAMES")
    if env:
        return shlex.split(env, posix=os.name != "nt")
    exe = shutil.which("hyperframes")
    if not exe:
        raise VariantError("hyperframes not found on PATH (npm install -g hyperframes, or set HYPERFRAMES)")
    return [exe]


def run(cmd, log=None, timeout=None):
    t0 = time.time()
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"$ {' '.join(str(c) for c in cmd)}\n# exit {p.returncode}, {time.time() - t0:.1f} s\n"
                       f"--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}", encoding="utf-8")
    return p, time.time() - t0


def probe(path):
    p, _ = run([FFPROBE, "-v", "error", "-count_packets", "-show_entries",
                "stream=codec_type,codec_name,r_frame_rate,nb_read_packets,duration:format=duration", "-of", "json", path])
    if p.returncode:
        raise VariantError(f"ffprobe failed on {path}: {p.stderr.strip()[-200:]}")
    d = json.loads(p.stdout)
    out = {"duration": float(d.get("format", {}).get("duration", 0) or 0)}
    for s in d["streams"]:
        if s["codec_type"] in ("video", "audio") and s["codec_type"] not in out:
            out[s["codec_type"]] = {"codec": s.get("codec_name"), "rate": s.get("r_frame_rate"),
                                    "packets": int(s.get("nb_read_packets", 0) or 0),
                                    "duration": float(s.get("duration", 0) or 0)}
    return out


def rate_value(r):
    a, _, b = str(r).partition("/")
    return float(a) / float(b or 1)


def free_ram_gb():
    """Available physical memory in GB, or None where this can't be read cheaply."""
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in ("tot", "avail", "totpf", "availpf", "totv", "availv", "ext")]
            st = MS()
            st.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.avail / 2 ** 30
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2 ** 20
    except (OSError, AttributeError, ValueError):
        pass
    return None


def wait_ram(min_gb, max_wait_s=1800):
    if not min_gb:
        return None
    t0 = time.time()
    while True:
        gb = free_ram_gb()
        if gb is None or gb >= min_gb:
            return gb
        if time.time() - t0 > max_wait_s:
            raise VariantError(f"free RAM stayed under {min_gb} GB for {max_wait_s // 60} min")
        print(f"    free RAM {gb:.1f} GB < {min_gb} GB, waiting", flush=True)
        time.sleep(20)


# ---------------------------------------------------------------- check


def run_check(proj, extra, log):
    cmd = hf_cmd() + ["check", proj, "--json", "--at-transitions", "--max-issues", "100000"] + extra
    p, secs = run(cmd, log, timeout=3600)
    txt = p.stdout
    i = txt.find("{")
    if i < 0:
        raise VariantError(f"check printed no JSON (exit {p.returncode}); see {log}")
    rep, _ = json.JSONDecoder().raw_decode(txt[i:])
    rep["_seconds"] = round(secs, 1)
    return rep


def findings(rep):
    out = []
    for sec in CHECK_SECTIONS:
        for f in (rep.get(sec) or {}).get("findings") or []:
            out.append({"section": sec, "code": f.get("code"), "severity": f.get("severity"),
                        "selector": f.get("selector"), "time": f.get("time"), "text": f.get("text"),
                        "message": (f.get("message") or "")[:300]})
    return out


def counts(fs):
    c = Counter(f["severity"] for f in fs)
    return {"error": c.get("error", 0), "warning": c.get("warning", 0), "info": c.get("info", 0)}


def new_findings(base_fs, var_fs):
    """Findings in the variant beyond what the base has, matched on (section, code, selector, severity) with counts."""
    left = Counter((f["section"], f["code"], f["selector"], f["severity"]) for f in base_fs)
    new = []
    for f in var_fs:
        k = (f["section"], f["code"], f["selector"], f["severity"])
        if left[k] > 0:
            left[k] -= 1
        else:
            new.append(f)
    return new


def check_summary(rep, fs):
    return {"ok": rep.get("ok"), "browser_skipped": rep.get("browserSkipped"), "seconds": rep.get("_seconds"),
            "counts": counts(fs)}


# ---------------------------------------------------------------- render + audio


def render(clone, raw, fps, extra, log):
    raw.parent.mkdir(parents=True, exist_ok=True)
    tmp = raw.with_name(raw.stem + ".part" + raw.suffix)
    tmp.unlink(missing_ok=True)
    cmd = hf_cmd() + ["render", clone, "--output", tmp, "--quiet"] + (["--fps", fps] if fps else []) + extra
    p, secs = run(cmd, log)
    if p.returncode or not tmp.exists():
        raise VariantError(f"render failed (exit {p.returncode}); see {log}")
    os.replace(tmp, raw)
    waited = "Waiting for another render" in (p.stdout + p.stderr)
    return round(secs, 1), waited


def audio_copy(raw, base_final, out):
    pr, pb = probe(raw), probe(base_final)
    if "video" not in pr or "video" not in pb or "audio" not in pb:
        raise VariantError("audio copy needs a video stream in both files and audio in the base final")
    frame = 1 / rate_value(pb["video"]["rate"])
    dv = abs(pr["video"]["duration"] - pb["video"]["duration"])
    rec = {"mode": "copy", "from": str(base_final), "frame_s": round(frame, 5),
           "video_s": [pr["video"]["duration"], pb["video"]["duration"]],
           "video_packets": [pr["video"]["packets"], pb["video"]["packets"]]}
    if dv > frame + 1e-3 or pr["video"]["rate"] != pb["video"]["rate"]:
        raise VariantError(f"variant picture {pr['video']['duration']:.3f} s @ {pr['video']['rate']} vs base "
                           f"{pb['video']['duration']:.3f} s @ {pb['video']['rate']}: more than one frame apart (pass --fps?)")
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    p, _ = run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", raw, "-i", base_final, "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy", "-map_metadata", "0", "-movflags", "+faststart", tmp])
    if p.returncode:
        raise VariantError(f"mux failed: {p.stderr.strip()[-300:]}")
    po = probe(tmp)
    rec["out_video_packets"] = po["video"]["packets"]
    rec["out_audio_s"] = [po["audio"]["duration"], pb["audio"]["duration"]]
    if po["video"]["packets"] != pr["video"]["packets"] or abs(po["audio"]["duration"] - pb["audio"]["duration"]) > 1e-3:
        tmp.unlink(missing_ok=True)
        raise VariantError(f"mux check failed: {rec}")
    os.replace(tmp, out)
    rec["ok"] = True
    return rec


def audio_loudness(raw, out, report):
    script = HERE / "loudness.py"
    cmd = (["uv", "run", "--quiet", script] if shutil.which("uv") else [sys.executable, script])
    p, secs = run(cmd + ["fix", raw, out, "--json", report])
    rep = json.loads(report.read_text(encoding="utf-8")).get(out.name, {}) if report.exists() else {}
    if p.returncode or not rep.get("ok"):
        raise VariantError(f"loudness.py fix failed (exit {p.returncode}): {(p.stdout + p.stderr).strip()[-300:]}")
    return {"mode": "loudness", "report": str(report), "output_audio": rep.get("output_audio"), "ok": True,
            "seconds": round(secs, 1)}


# ---------------------------------------------------------------- main


def load_spec(a):
    if a.variants:
        spec = json.loads(Path(a.variants).read_text(encoding="utf-8"))
        slots = {k: v["selector"] if isinstance(v, dict) else v for k, v in spec.get("slots", {}).items()}
        variants = spec.get("variants", [])
    else:
        if not a.selector or not a.set:
            raise VariantError("give a variants file, or --selector with at least one --set id=value")
        slots = {a.slot: a.selector}
        variants = []
        for s in a.set:
            vid, eq, val = s.partition("=")
            if not eq:
                raise VariantError(f"--set {s!r}: want id=value")
            variants.append({"id": vid.strip(), a.slot: val})
    if not slots:
        raise VariantError("no slots")
    ids = [str(v.get("id", "")) for v in variants]
    for vid in ids:
        if not ID_RE.match(vid) or vid == "base":
            raise VariantError(f"variant id {vid!r}: letters, digits, - and _ only, not 'base'")
    if len(set(ids)) != len(ids):
        raise VariantError("duplicate variant ids")
    for v in variants:
        extra = set(v) - {"id"} - set(slots)
        if extra:
            raise VariantError(f"variant {v['id']}: unknown slot(s) {sorted(extra)} (declare them under slots)")
    return slots, variants


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[1:]))
    ap.add_argument("project", help="built HyperFrames project folder (holds index.html)")
    ap.add_argument("variants", nargs="?", help="variants JSON file")
    ap.add_argument("--out", required=True, help="output folder, outside the project")
    ap.add_argument("--selector", help="single-slot mode: #id of the element whose inner HTML changes")
    ap.add_argument("--slot", default="hook", help="single-slot mode: slot name (default hook)")
    ap.add_argument("--set", action="append", metavar="ID=VALUE", help="single-slot mode: one variant (repeatable)")
    ap.add_argument("--audio-from", help="base final: its audio is copied onto every variant (no loudness pass)")
    ap.add_argument("--name", help="output name stem (default: --audio-from stem, else the project folder name)")
    ap.add_argument("--fps", help="render frame rate (default: the --audio-from file's rate, else the composition's)")
    ap.add_argument("--only", help="comma-separated variant ids")
    ap.add_argument("--check-only", action="store_true", help="clone + check, no render")
    ap.add_argument("--no-check", action="store_true", help="skip hyperframes check")
    ap.add_argument("--render-anyway", action="store_true", help="render even when the check finds new errors")
    ap.add_argument("--render-arg", action="append", default=[], help="extra render flag, e.g. --render-arg=--workers=3")
    ap.add_argument("--check-arg", action="append", default=[], help="extra check flag, e.g. --check-arg=--budget=8m")
    ap.add_argument("--min-ram-gb", type=float, default=4.0, help="wait for this much free RAM before a render (0 = off)")
    a = ap.parse_args(argv)
    try:
        return run_all(a)
    except VariantError as e:
        print(f"variants: {e}", file=sys.stderr)
        return 2


def run_all(a):
    proj = Path(a.project).resolve()
    out = Path(a.out).resolve()
    if not (proj / "index.html").is_file():
        raise VariantError(f"{proj}: no index.html")
    if out == proj or proj in out.parents:
        raise VariantError("--out must sit outside the project (HyperFrames refuses renders inside it, and clones would nest)")
    slots_sel, variants = load_spec(a)
    slots = {k: element_id(v) for k, v in slots_sel.items()}
    if a.only:
        keep = {x.strip() for x in a.only.split(",")}
        variants = [v for v in variants if v["id"] in keep]
    audio_from = Path(a.audio_from).resolve() if a.audio_from else None
    if audio_from and not audio_from.is_file():
        raise VariantError(f"--audio-from {audio_from}: not found")
    fps = a.fps or (probe(audio_from).get("video", {}).get("rate") if audio_from else None)
    name = a.name or (audio_from.stem if audio_from else proj.name)
    base_raw = (proj / "index.html").read_bytes()
    base_src = base_raw.decode("utf-8")
    base_vals = {}
    for s, eid in slots.items():
        i, j = find_inner(base_src, eid)
        base_vals[s] = fragment(base_src[i:j])
    parsed, sources = {}, {}
    for v in variants:  # every value is validated before any check or render runs
        try:
            parsed[v["id"]] = {s: fragment(v[s]) for s in slots if s in v}
            sources[v["id"]] = substitute(base_src, slots, {s: h for s, (h, _) in parsed[v["id"]].items()})
        except VariantError as e:
            raise VariantError(f"variant {v['id']}: {e}") from None
    out.mkdir(parents=True, exist_ok=True)
    vj = out / "variants.json"
    prev = json.loads(vj.read_text(encoding="utf-8")) if vj.exists() else {}
    prev_rows = {r["id"]: r for r in prev.get("variants", [])}
    base_newest = newest_mtime(proj)
    print(f"project {proj}\nslots   " + ", ".join(f"{s} = #{e}" for s, e in slots.items()))
    print(f"name {name}, fps {fps or 'composition default (pass --fps to match the base)'}, audio "
          f"{'copied from ' + audio_from.name if audio_from else 'loudness.py fix'}")

    base_check = {}

    def get_base_check():
        if base_check:
            return base_check
        clone = out / "clones" / f"{proj.name}__base"
        clone.mkdir(parents=True, exist_ok=True)
        sync_clone(proj, clone)
        write_if_changed(clone / "index.html", base_raw)
        cache = out / "checks" / "base.json"
        sha = hashlib.sha256(base_raw).hexdigest()
        flags = a.check_arg
        if cache.exists():
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("_index_sha256") == sha and c.get("_flags") == flags:
                print(f"  base check: cached ({c.get('_seconds')} s when run)")
                base_check.update(c)
                return base_check
        print("  base check (unchanged project, same flags) ...", flush=True)
        rep = run_check(clone, flags, out / "logs" / "base.check.log")
        rep["_index_sha256"], rep["_flags"] = sha, flags
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        fs = findings(rep)
        print(f"  base check: ok={rep.get('ok')} {counts(fs)} in {rep['_seconds']} s", flush=True)
        base_check.update(rep)
        return base_check

    rows, bad = [], 0
    for v in variants:
        vid = v["id"]
        vals = parsed[vid]
        row = {"id": vid, "slots": {s: {"html": (vals.get(s) or base_vals[s])[0], "text": (vals.get(s) or base_vals[s])[1],
                                        "changed": s in vals} for s in slots}}
        row["ci_vars"] = [f"{s}={row['slots'][s]['text']}" for s in slots]
        final = out / f"{name}_{vid}.mp4"
        raw = out / "renders" / f"{name}_{vid}.mp4"
        clone = out / "clones" / f"{proj.name}__{vid}"
        row.update({"clone": str(clone), "render": str(raw), "output": str(final)})
        print(f"\n[{vid}] " + " | ".join(f"{s}: {row['slots'][s]['text']}" for s in slots), flush=True)
        try:
            new_src = sources[vid]
            clone.mkdir(parents=True, exist_ok=True)
            st = sync_clone(proj, clone)
            changed = write_if_changed(clone / "index.html", new_src.encode("utf-8"))
            print(f"  clone: {st['linked']} linked, {st['copied']} copied ({st['copied_bytes'] / 2 ** 20:.1f} MB), "
                  f"{st['kept']} kept, index.html {'written' if changed else 'unchanged'}")
            inputs = max(base_newest, (clone / "index.html").stat().st_mtime, audio_from.stat().st_mtime if audio_from else 0)
            p = prev_rows.get(vid, {})
            if (not a.check_only and final.exists() and p.get("status") == "done" and final.stat().st_mtime > inputs
                    and p.get("output_size") == final.stat().st_size and p.get("output_mtime_ns") == final.stat().st_mtime_ns):
                for k in ("check", "render_seconds", "render_waited_for_lock", "audio", "output_size", "output_mtime_ns"):
                    if k in p:
                        row[k] = p[k]
                row["status"] = "done"
                print(f"  fresh: {final.name} is newer than its inputs, skipped")
                rows.append(row)
                continue
            if not a.no_check:
                bc = get_base_check()
                print("  check ...", flush=True)
                rep = run_check(clone, a.check_arg, out / "logs" / f"{vid}.check.log")
                (out / "checks").mkdir(parents=True, exist_ok=True)
                (out / "checks" / f"{vid}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
                bfs, vfs = findings(bc), findings(rep)
                new = new_findings(bfs, vfs)
                row["check"] = {**check_summary(rep, vfs), "base": check_summary(bc, bfs), "new": new,
                                "new_counts": counts(new), "report": str(out / "checks" / f"{vid}.json")}
                nc = counts(new)
                print(f"  check: ok={rep.get('ok')} in {rep['_seconds']} s, new vs base: {nc['error']} error(s), "
                      f"{nc['warning']} warning(s), {nc['info']} info")
                for f in new[:12]:
                    print(f"    NEW {f['severity']:<7} {f['section']}/{f['code']} {f['selector']} @ {f['time']}: "
                          f"{f['message'][:140]}")
                if (nc["error"] or rep.get("browserSkipped")) and not a.render_anyway:
                    row["status"] = "blocked"
                    print("  blocked: new check errors or no browser pass (fix the text, or --render-anyway)")
                    rows.append(row)
                    bad += 1
                    continue
            if a.check_only:
                row["status"] = "checked"
                rows.append(row)
                continue
            gb = wait_ram(a.min_ram_gb)
            print(f"  render{f' (free RAM {gb:.1f} GB)' if gb is not None else ''} ...", flush=True)
            secs, waited = render(clone, raw, fps, a.render_arg, out / "logs" / f"{vid}.render.log")
            row["render_seconds"], row["render_waited_for_lock"] = secs, waited
            print(f"  rendered in {secs} s{' (part of it waiting for another render)' if waited else ''}", flush=True)
            if audio_from:
                row["audio"] = audio_copy(raw, audio_from, final)
            else:
                row["audio"] = audio_loudness(raw, final, out / "loudness.json")
            sf = final.stat()
            row.update({"status": "done", "output_size": sf.st_size, "output_mtime_ns": sf.st_mtime_ns})
            print(f"  -> {final}")
        except (VariantError, subprocess.TimeoutExpired, OSError) as e:
            row["status"], row["error"] = "failed", str(e)
            print(f"  FAILED: {e}")
            bad += 1
        rows.append(row)

    done_ids = {r["id"] for r in rows}
    rows += [r for r in prev.get("variants", []) if r["id"] not in done_ids]  # --only keeps the others' rows
    report = {"tool": "variants.py", "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "project": str(proj), "name": name,
              "fps": fps, "slots": {s: {"selector": slots_sel[s]} for s in slots},
              "base": {"id": "base", "output": str(audio_from) if audio_from else None,
                       "slots": {s: {"html": base_vals[s][0], "text": base_vals[s][1]} for s in slots},
                       "ci_vars": [f"{s}={base_vals[s][1]}" for s in slots],
                       "check": (check_summary(base_check, findings(base_check)) if base_check
                                 else prev.get("base", {}).get("check"))},
              "variants": sorted(rows, key=lambda r: r["id"])}
    vj.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{vj}\n" + "\n".join(f"  {r['id']:<8} {r['status']:<8} {Path(r['output']).name}" for r in report["variants"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
