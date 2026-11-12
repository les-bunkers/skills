# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""decide.py: fast yes/no, choice and score checks on a video with the Gemini Decisions API (model gemini-decision-1).

One request = one shared input (a video slice, still frames, an audio slice, reference images, text) + up to 1000
questions, answered in 0.3-1.5 s with calibrated probabilities and no generated text. Price: $0.10 per 1M input tokens,
output free; 1 min of low-resolution video ~ 6k tokens ~ $0.0006. Agents use it as assertions against a render (did the
change land, did anything else break) and as a judge (which of N takes, frames or angles wins).

Commands
  ask    MEDIA + sample flags + questions -> answers table (ad hoc)
  run    CHECKS (JSON file or pack name) -> every check over its sample, pass / fail / unsure report
  pick   N candidates (files, or frames of one file) -> ranked by one score/yes-no question, optional gates
  scan   MEDIA -> one question set per window -> probability timeline + flagged spans
  calibrate  labelled examples x question wordings -> separation per wording + threshold

Environment
  GEMINI_API_KEY (or GOOGLE_API_KEY)  API key; any placeholder when a local proxy injects auth
  GEMINI_BASE_URL                     default https://generativelanguage.googleapis.com (google-genai SDK convention, no version)
  GEMINI_DECISION_MODEL               default gemini-decision-1
  DECIDE_CACHE                        prepared media, answers, answer log (default ~/.cache/videos-decide)
  DECIDE_WORKERS                      parallel requests (default 12)
  FFMPEG / FFPROBE                    binaries (default: on PATH)

usage:
  uv run decide.py ask render.mp4 --at 0.4,1.2 --res high "The on-screen headline reads «SHOOT WITHOUT A CREW»."
  uv run decide.py ask render.mp4 --from -3 --choice "What closes the video?" endcard="a logo end card" talk="the speaker talking"
  uv run decide.py run short --media final/x_9x16.mp4 --var hook="SHOOT WITHOUT A CREW" --var cta="example.com" --json checks/decide.json
  uv run decide.py pick takes/*.mp4 --with ref.png:"Reference sheet" --score "The take matches the reference" bad ok good
  uv run decide.py scan master.mp4 --every 1 --res high "A password, email address or phone number is readable on screen." --flag ">=0.4"
  uv run decide.py calibrate calib.json     (wordings x labelled frames -> margins, AUC, thresholds)
Exit: 0 pass, 1 a check failed, 2 unsure answers only (route to a human), 3 usage / media / API error.
Reference: ${CLAUDE_PLUGIN_ROOT}/references/decide.md
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
PACKS = HERE / "decide_packs"
MODEL = os.environ.get("GEMINI_DECISION_MODEL", "gemini-decision-1")
BASE = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
PRICE_PER_TOKEN = 0.10 / 1e6
WORKERS = int(os.environ.get("DECIDE_WORKERS", "12"))
MAX_BODY = int(float(os.environ.get("DECIDE_MAX_BODY_MB", "95")) * 1024 * 1024)  # API inline cap = 100 MB
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESOLUTIONS = ("low", "medium", "high", "ultra_high")
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
             ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aif", ".aiff"}
# prepared media: short side of video proxies, long side of stills (the API downsamples anyway; smaller = faster upload)
VIDEO_SHORT_SIDE = {"low": 360, "medium": 360, "high": 720, "ultra_high": 720}
STILL_LONG_SIDE = {"low": 512, "medium": 768, "high": 1536, "ultra_high": 2048}
# rough token estimates for --dry-run (docs: video ~100 tok/s low, ~300 high; audio 32 tok/s; images up to 1120 at high)
EST_VIDEO_TPS = {"low": 100, "medium": 100, "high": 300, "ultra_high": 300}
EST_IMAGE = {"low": 260, "medium": 560, "high": 1120, "ultra_high": 2240}


class DecideError(Exception):
    pass


def cache_dir():
    d = Path(os.environ.get("DECIDE_CACHE") or Path.home() / ".cache" / "videos-decide")
    (d / "media").mkdir(parents=True, exist_ok=True)
    (d / "answers").mkdir(parents=True, exist_ok=True)
    return d


def tool(name):
    exe = os.environ.get(name.upper()) or shutil.which(name)
    if not exe:
        raise DecideError(f"{name} not found on PATH (or set {name.upper()})")
    return exe


# ---------------------------------------------------------------- media

_durations = {}


def duration(path):
    path = str(path)
    if path not in _durations:
        out = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True)
        try:
            _durations[path] = float(out.stdout.strip())
        except ValueError:
            _durations[path] = 0.0
    return _durations[path]


def media_kind(path):
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return "video"


def _prep_key(src, *parts):
    st = Path(src).stat()
    raw = "|".join([str(Path(src).resolve()), str(st.st_size), str(st.st_mtime_ns), *map(str, parts), "v1"])
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _ffmpeg(args, out):
    out = Path(out)
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_name(out.stem + f".{os.getpid()}.{threading.get_ident()}.tmp{out.suffix}")
    r = subprocess.run([tool("ffmpeg"), "-v", "error", "-y", *args, str(tmp)], capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise DecideError(f"ffmpeg failed for {out.name}: {r.stderr.strip()[-400:]}")
    os.replace(tmp, out)
    return out


def prep_still(src, t, res):
    side = STILL_LONG_SIDE[res]
    out = cache_dir() / "media" / f"{_prep_key(src, 'still', round(t, 3), side)}.jpg"
    vf = f"scale='if(gt(iw,ih),min({side},iw),-2)':'if(gt(iw,ih),-2,min({side},ih))'"
    err = None
    for back in (0.0, 0.1, 0.3, 0.7, 1.5):  # the container can outlast the last video frame (audio tail): step back
        try:
            return _ffmpeg(["-ss", f"{max(0.0, t - back):.3f}", "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "3"], out)
        except DecideError as e:
            err = e
    raise err


def prep_image(src, res):
    side = STILL_LONG_SIDE[res]
    out = cache_dir() / "media" / f"{_prep_key(src, 'image', side)}.jpg"
    vf = f"scale='if(gt(iw,ih),min({side},iw),-2)':'if(gt(iw,ih),-2,min({side},ih))'"
    return _ffmpeg(["-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "3"], out)


def prep_clip(src, t0, t1, res):
    side = VIDEO_SHORT_SIDE[res]
    out = cache_dir() / "media" / f"{_prep_key(src, 'clip', round(t0, 3), round(t1, 3), side)}.mp4"
    vf = f"scale='if(gt(iw,ih),-2,{side})':'if(gt(iw,ih),{side},-2)',fps=10,format=yuv420p"
    return _ffmpeg(["-ss", f"{t0:.3f}", "-t", f"{max(0.05, t1 - t0):.3f}", "-i", str(src), "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                    "-movflags", "+faststart"], out)


def prep_audio(src, t0, t1):
    out = cache_dir() / "media" / f"{_prep_key(src, 'audio', round(t0, 3), round(t1, 3))}.mp3"
    return _ffmpeg(["-ss", f"{t0:.3f}", "-t", f"{max(0.05, t1 - t0):.3f}", "-i", str(src), "-vn", "-ac", "1",
                    "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k"], out)


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


# ---------------------------------------------------------------- samples -> inputs
# A sample says which part of the media a check looks at. Keys (all optional):
#   at / frames: seconds or list -> still frames     every: seconds -> stills every N s in [from, to)
#   from / to: seconds (negative = from the end)     audio: true -> audio only     window (+ hop): seconds -> clips
#   each: true -> one request per still/window (per-frame verdicts)     res: low | medium | high | ultra_high
#   label: text before the media    context: text (or list) after it    with: reference files before it
#   media: another file than the run's media     compare: true -> "Before:" baseline + "After:" media in one input


def _num_list(v):
    if v is None:
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, str):
        return [float(x) for x in v.split(",") if x.strip()]
    return [float(x) for x in v]


def _abs_t(t, dur):
    return max(0.0, dur + t) if t < 0 else t


def _span(sample, dur):
    a = _abs_t(float(sample.get("from", 0) or 0), dur)
    b = sample.get("to")
    b = dur if b is None else _abs_t(float(b), dur)
    if dur:
        b = min(b, dur)
    if b <= a:
        raise DecideError(f"empty span from {a:.2f} to {b:.2f} s (media is {dur:.2f} s)")
    return a, b


def _ref_parts(refs):
    parts = []
    for r in refs or []:
        if isinstance(r, str):
            path, label = r, ""
            if not Path(r).exists() and ":" in r:
                path, _, label = r.rpartition(":")
            r = {"file": path, "label": label}
        if not Path(r["file"]).exists():
            raise DecideError(f"reference file not found: {r['file']}")
        if r.get("label"):
            parts.append(("text", r["label"].rstrip(":") + ":"))
        f = r["file"]
        if media_kind(f) == "image":
            parts.append(("imagefile", f, r.get("res", "high")))
        elif media_kind(f) == "audio":
            parts.append(("audio", f, 0.0, duration(f)))
        elif "at" in r:
            parts.append(("image", f, _abs_t(float(r["at"]), duration(f)), r.get("res", "high")))
        else:
            parts.append(("video", f, 0.0, duration(f), r.get("res", "low")))
    return parts


def plan_inputs(sample, media):
    """-> list of {"at": display, "t": float | [a, b] | None, "parts": [...]}; one entry = one request input."""
    media = sample.get("media") or media
    if not media:
        raise DecideError("no media: pass --media or set media in the check file / sample")
    if not Path(media).exists():
        raise DecideError(f"media not found: {media}")
    kind = media_kind(media)
    dur = 0.0 if kind == "image" else duration(media)
    res = sample.get("res")
    if res and res not in RESOLUTIONS:
        raise DecideError(f"res must be one of {RESOLUTIONS}")
    inputs = []
    if kind == "image":
        inputs.append({"at": "image", "t": None, "parts": [("imagefile", media, res or "high")]})
    elif sample.get("audio") or kind == "audio":
        a, b = _span(sample, dur)
        for wa, wb in _windows(a, b, sample):
            inputs.append({"at": f"{wa:.2f}-{wb:.2f}", "t": [wa, wb], "parts": [("audio", media, wa, wb)]})
    elif any(k in sample for k in ("at", "frames", "every")):
        times = [_abs_t(t, dur) for t in _num_list(sample.get("at", sample.get("frames")))]
        if sample.get("every"):
            a, b = _span(sample, dur)
            step = float(sample["every"])
            t = a + float(sample.get("offset", step / 2))
            while t < b:
                times.append(t)
                t += step
        times = [min(t, max(0.0, dur - 0.05)) for t in times]
        r = res or "high"
        if sample.get("each") or len(times) == 1:
            for t in times:
                inputs.append({"at": f"{t:.2f}", "t": t, "parts": [("image", media, t, r)]})
        else:
            parts = []
            for t in times:
                parts += [("text", f"Frame at {t:.2f} s:"), ("image", media, t, r)]
            inputs.append({"at": ",".join(f"{t:.1f}" for t in times), "t": times, "parts": parts})
    else:
        a, b = _span(sample, dur)
        for wa, wb in _windows(a, b, sample):
            inputs.append({"at": f"{wa:.2f}-{wb:.2f}", "t": [wa, wb], "parts": [("video", media, wa, wb, res or "low")]})
    pre = _ref_parts(sample.get("with"))
    if sample.get("label"):
        pre.append(("text", sample["label"]))
    ctx = sample.get("context")
    post = [("text", c) for c in ([ctx] if isinstance(ctx, str) else (ctx or [])) if c]
    for inp in inputs:
        inp["pre"], inp["media_parts"], inp["post"] = pre, inp["parts"], post
        inp["parts"] = pre + inp["parts"] + post
    return inputs


def _windows(a, b, sample):
    w = sample.get("window")
    if not w:
        return [(a, b)]
    w = float(w)
    hop = float(sample.get("hop", w))
    out, t = [], a
    while t < b - 0.05:
        out.append((t, min(b, t + w)))
        t += hop
    return out


def paired_inputs(sample, media, baseline):
    """compare: true -> each input = "Before:" baseline part + "After:" media part at the same times."""
    after = plan_inputs(sample, media)
    if not sample.get("compare"):
        return after
    base = sample.get("baseline") or baseline
    if not base:
        raise DecideError("compare: true needs --baseline (or baseline in the sample)")
    before = plan_inputs({**sample, "media": base, "with": None, "label": None, "context": None}, base)
    if len(before) != len(after):
        raise DecideError("baseline and media give a different number of inputs (durations differ?)")
    for b, a in zip(before, after):
        a["parts"] = (a["pre"] + [("text", "Before (previous version):")] + b["media_parts"]
                      + [("text", "After (new version):")] + a["media_parts"] + a["post"])
    return after


def part_block(part):
    kind = part[0]
    if kind == "text":
        return {"type": "text", "text": part[1]}, None
    if kind == "imagefile":
        _, src, res = part
        if Path(src).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and Path(src).stat().st_size < 4_000_000:
            p, mime = Path(src), IMAGE_EXT[Path(src).suffix.lower()]
        else:
            p, mime = prep_image(src, res), "image/jpeg"
        return {"type": "image", "data": b64(p), "mime_type": mime, "resolution": res}, p
    if kind == "image":
        _, src, t, res = part
        p = prep_still(src, t, res)
        return {"type": "image", "data": b64(p), "mime_type": "image/jpeg", "resolution": res}, p
    if kind == "video":
        _, src, a, b, res = part
        p = prep_clip(src, a, b, res)
        return {"type": "video", "data": b64(p), "mime_type": "video/mp4",
                "resolution": "high" if res in ("high", "ultra_high") else res}, p
    if kind == "audio":
        _, src, a, b = part
        p = prep_audio(src, a, b)
        return {"type": "audio", "data": b64(p), "mime_type": "audio/mp3"}, p
    raise DecideError(f"unknown part {kind}")


def estimate_tokens(parts, questions):
    n = 0
    for p in parts:
        if p[0] == "text":
            n += len(p[1]) // 4 + 1
        elif p[0] in ("image", "imagefile"):
            n += EST_IMAGE[p[-1]]
        elif p[0] == "video":
            n += int((p[3] - p[2]) * EST_VIDEO_TPS[p[4]])
        elif p[0] == "audio":
            n += int((p[3] - p[2]) * 32)
    return n + sum(len(json.dumps(q, ensure_ascii=False)) // 4 for q in questions.values())


# ---------------------------------------------------------------- questions + expectations

_VAR = re.compile(r"\{([A-Za-z_][\w.]*)\}")


def fill(value, variables):
    if isinstance(value, str):
        return _VAR.sub(lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0), value)
    if isinstance(value, list):
        return [fill(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, variables) for k, v in value.items()}
    return value


def flatten(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = " ".join(v) if isinstance(v, list) and all(isinstance(x, str) for x in v) else v
    return out


def build_question(c):
    """check dict -> API question. Shorthand: ask (+ yes/no descriptions) | options {id: desc} | levels [low..high]."""
    instr = c.get("ask") or c.get("instructions")
    if not instr:
        raise DecideError(f"check {c.get('id')}: needs 'ask'")
    qtype = c.get("type") or ("choice" if "options" in c else "score" if "levels" in c else "noul")
    q = {"type": qtype, "instructions": instr}
    if qtype == "choice":
        opts = c.get("options") or c.get("criteria")
        q["criteria"] = {str(o): "" for o in opts} if isinstance(opts, list) else {str(k): str(v) for k, v in opts.items()}
        if not 2 <= len(q["criteria"]) <= 255:
            raise DecideError(f"check {c.get('id')}: choice needs 2-255 options")
    elif qtype == "score":
        q["criteria"] = [str(x) for x in (c.get("levels") or c.get("criteria"))]
        if not 2 <= len(q["criteria"]) <= 255:
            raise DecideError(f"check {c.get('id')}: score needs 2-255 levels")
    elif qtype == "noul":
        crit = {}
        if c.get("yes"):
            crit["true_description"] = c["yes"]
        if c.get("no"):
            crit["false_description"] = c["no"]
        if crit:
            q["criteria"] = crit
    else:
        raise DecideError(f"check {c.get('id')}: type must be noul, choice or score")
    return q


_CMP = re.compile(r"^\s*(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)\s*$")


def _cmp(value, rule):
    m = _CMP.match(str(rule))
    if not m:
        raise DecideError(f"bad expectation {rule!r} (use >=0.8, <0.3, ...)")
    op, x = m.group(1), float(m.group(2))
    return {">=": value >= x, "<=": value <= x, ">": value > x, "<": value < x, "==": abs(value - x) < 1e-9,
            "!=": abs(value - x) >= 1e-9}[op]


def value_of(ans):
    t = ans.get("type")
    return ans.get("noul") if t == "noul" else ans.get("choice") if t == "choice" else ans.get("score")


def _threshold(rule):
    m = _CMP.match(str(rule))
    return float(m.group(2)) if m else None


def judge(ans, expect, min_conf=0.6, band=None):
    """-> pass | fail | unsure | info.
    noul: yes | no | >=0.8 | <0.27 ...  unsure when P is within `band` of the threshold (default 0.08; set the threshold
          from `calibrate`, whose margins are measured in the same units).
    choice: id | "a|b" | "!id" | [ids]  unsure when confidence < min_conf (default 0.6).
    score: >=2 ...  unsure when the score is within `band` of the threshold (default 0.25 of a level)."""
    if expect is None:
        return "info"
    t, v, conf = ans.get("type"), value_of(ans), ans.get("confidence", 0.0)
    if t == "noul":
        rule = {"yes": ">=0.5", "no": "<0.5", True: ">=0.5", False: "<0.5"}.get(expect, expect)
        ok = _cmp(v, rule)
        thr = _threshold(rule)
        if thr is not None and abs(v - thr) < (0.08 if band is None else band):
            return "unsure"
    elif t == "choice":
        if isinstance(expect, list):
            ok = v in expect
        elif str(expect).startswith("!"):
            ok = v not in str(expect)[1:].split("|")
        else:
            ok = v in str(expect).split("|")
        if min_conf and conf < min_conf:
            return "unsure"
    else:
        ok = _cmp(v, expect)
        thr = _threshold(expect)
        if thr is not None and abs(v - thr) < (0.25 if band is None else band):
            return "unsure"
    return "pass" if ok else "fail"


def show(ans):
    t = ans.get("type")
    if t == "noul":
        return f"P(yes)={ans['noul']:.2f}"
    if t == "choice":
        probs = ans.get("probabilities") or {}
        top = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
        return f"{ans['choice']} (" + ", ".join(f"{k} {p:.2f}" for k, p in top) + ")"
    if t == "score":
        return f"{ans['score']:.2f}"
    return json.dumps(ans)


# ---------------------------------------------------------------- API


def api_key():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise DecideError("set GEMINI_API_KEY (any placeholder when GEMINI_BASE_URL points at a key-injecting proxy)")
    return key


def call_api(body, use_cache=True):
    """POST /v1beta/decisions with retries; answers cached by sha256 of the exact body. -> (response, cached)"""
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(raw) > MAX_BODY:
        raise DecideError(f"request is {len(raw) / 1e6:.0f} MB (cap {MAX_BODY / 1e6:.0f} MB): shorten the span, use res low or stills")
    digest = hashlib.sha256(raw).hexdigest()
    cpath = cache_dir() / "answers" / f"{digest[:32]}.json"
    if use_cache and cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8")), True
    req = urllib.request.Request(f"{BASE}/v1beta/decisions", data=raw, method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": api_key()})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            last = DecideError(f"HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3 and "SECRET_UNAVAILABLE" not in detail:
                time.sleep(1.5 * 2 ** attempt)
                continue
            raise last
        except urllib.error.URLError as e:
            last = DecideError(f"cannot reach {BASE}: {e.reason}")
            if attempt < 3:
                time.sleep(1.5 * 2 ** attempt)
                continue
            raise last
    else:
        raise last
    tmp = cpath.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(resp, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cpath)
    return resp, False


_log_lock = threading.Lock()


def log_answers(run_id, media, at, answers, checks):
    lines = []
    for key, ans in answers.items():
        lines.append(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run_id, "model": MODEL,
                                 "check": key, "media": str(media), "at": at, "type": ans.get("type"),
                                 "value": value_of(ans), "confidence": ans.get("confidence"),
                                 "ask": checks.get(key, {}).get("ask")}, ensure_ascii=False))
    with _log_lock, open(cache_dir() / "log.jsonl", "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- engine


def execute(jobs, dry_run=False, use_cache=True, run_id=None, media=None):
    """jobs: [{"input": {...}, "questions": {key: q}, "checks": {key: check}}] -> same list with answers / error / usage."""
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")

    def one(job):
        t0 = time.time()
        qs = job["questions"]
        job["est_tokens"] = estimate_tokens(job["input"]["parts"], qs)
        try:
            blocks, files = [], []
            for p in job["input"]["parts"]:
                blk, f = part_block(p)
                blocks.append(blk)
                if f is not None and p[0] == "image":
                    files.append(str(f))
            job["frames"] = files
            if dry_run:
                job["bytes"] = sum(len(b.get("data", "")) for b in blocks)
                return job
            resp, cached = call_api({"model": MODEL, "input": blocks, "questions": qs}, use_cache)
            job["answers"] = resp.get("answers", {})
            job["usage"] = resp.get("usage", {})
            job["cached"] = cached
            if not cached:
                log_answers(run_id, job["input"].get("media", media), job["input"]["at"], job["answers"], job["checks"])
        except DecideError as e:
            job["error"] = str(e)
        job["ms"] = int((time.time() - t0) * 1000)
        return job

    with cf.ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        return list(ex.map(one, jobs))


def make_jobs(groups, media, baseline):
    """groups: [(sample, {key: check})] -> request jobs (<= 1000 questions each, one per input)."""
    jobs = []
    for sample, checks in groups:
        questions = {k: build_question(c) for k, c in checks.items()}
        for inp in paired_inputs(sample, media, baseline):
            inp["media"] = sample.get("media") or media
            keys = list(questions)
            for i in range(0, len(keys), 1000):
                part = keys[i:i + 1000]
                jobs.append({"input": inp, "questions": {k: questions[k] for k in part},
                             "checks": {k: checks[k] for k in part}})
    return jobs


def summarize_usage(jobs):
    toks = sum((j.get("usage") or {}).get("total_input_tokens", 0) for j in jobs if not j.get("cached"))
    return {"requests": len(jobs), "cached": sum(1 for j in jobs if j.get("cached")),
            "errors": sum(1 for j in jobs if j.get("error")), "input_tokens": toks,
            "cost_usd": round(toks * PRICE_PER_TOKEN, 6),
            "est_tokens": sum(j.get("est_tokens", 0) for j in jobs)}


# ---------------------------------------------------------------- run (check files)


def load_checks(ref, seen=()):
    """check file path or pack name (scripts/decide_packs/<name>.json); 'extends' merges a base pack by check id."""
    p = Path(ref)
    if not p.exists():
        p = PACKS / f"{ref.removesuffix('.json')}.json"
    if not p.exists():
        known = ", ".join(sorted(x.stem for x in PACKS.glob("*.json"))) if PACKS.exists() else "none"
        raise DecideError(f"check file or pack not found: {ref} (packs: {known})")
    spec = json.loads(p.read_text(encoding="utf-8"))
    spec["_path"] = str(p)
    base = spec.get("extends")
    if base and base not in seen:
        parent = load_checks(base, (*seen, ref))
        by_id = {c["id"]: c for c in parent.get("checks", [])}
        for c in spec.get("checks", []):
            by_id[c["id"]] = {**by_id.get(c["id"], {}), **c}
        merged = {**parent, **{k: v for k, v in spec.items() if k != "checks"}}
        merged["samples"] = {**parent.get("samples", {}), **spec.get("samples", {})}
        merged["vars"] = {**parent.get("vars", {}), **spec.get("vars", {})}
        merged["checks"] = list(by_id.values())
        spec = merged
    return spec


def resolve_vars(spec, cli_vars, var_files, base_dir):
    variables = dict(spec.get("vars") or {})
    for name, f in {**(spec.get("var_files") or {}), **var_files}.items():
        fp = Path(f) if Path(f).is_absolute() else base_dir / f
        if not fp.exists():
            fp = Path(f)
        variables.update(flatten(json.loads(fp.read_text(encoding="utf-8")), f"{name}."))
    variables.update(cli_vars)
    return variables


def run_checks(spec, media=None, baseline=None, cli_vars=None, var_files=None, only=None, skip=None,
               dry_run=False, use_cache=True):
    """Run a loaded check spec. -> report dict."""
    base_dir = Path(spec.get("_path", ".")).parent
    media = media or spec.get("media")
    if media and not Path(media).is_absolute() and not Path(media).exists():
        media = str(base_dir / media)
    variables = resolve_vars(spec, cli_vars or {}, var_files or {}, base_dir)
    if media:
        variables.setdefault("media_name", Path(media).stem)
    defaults = {"min_confidence": spec.get("min_confidence", 0.6), **({"band": spec["band"]} if "band" in spec else {})}
    samples = spec.get("samples", {})
    checks = [c for c in spec.get("checks", []) if (not only or c["id"] in only) and not (skip and c["id"] in skip)
              and not c.get("disabled")]
    if not checks:
        raise DecideError("no checks selected")
    groups = {}
    for c in checks:
        if not KEY_RE.match(c["id"]):
            raise DecideError(f"check id {c['id']!r}: letters, digits, _ and - only, max 64")
        c = fill(c, variables)
        unfilled = _VAR.findall(json.dumps({k: c.get(k) for k in ("ask", "options", "levels", "yes", "no")}, ensure_ascii=False))
        if unfilled:
            raise DecideError(f"check {c['id']}: missing var(s) {sorted(set(unfilled))} (pass --var name=value)")
        s = c.get("sample", {})
        sample = fill(dict(samples[s]) if isinstance(s, str) else dict(s), variables)
        if isinstance(s, str) and s not in samples:
            raise DecideError(f"check {c['id']}: unknown sample {s!r}")
        gkey = json.dumps(sample, sort_keys=True, ensure_ascii=False)
        groups.setdefault(gkey, (sample, {}))[1][c["id"]] = {**defaults, **c}
    t0 = time.time()
    jobs = make_jobs(list(groups.values()), media, baseline)
    jobs = execute(jobs, dry_run=dry_run, use_cache=use_cache, media=media)
    results = []
    for c in checks:
        cid = c["id"]
        cj = [j for j in jobs if cid in j["questions"]]
        chk = next(j["checks"][cid] for j in cj) if cj else c
        per = []
        for j in cj:
            if j.get("error"):
                per.append({"at": j["input"]["at"], "verdict": "error", "error": j["error"]})
                continue
            if dry_run:
                per.append({"at": j["input"]["at"], "verdict": "dry-run"})
                continue
            ans = j["answers"].get(cid)
            if ans is None:
                per.append({"at": j["input"]["at"], "verdict": "error", "error": "no answer returned"})
                continue
            v = judge(ans, chk.get("expect"), chk.get("min_confidence", 0.6), chk.get("band"))
            per.append({"at": j["input"]["at"], "t": j["input"]["t"], "verdict": v, "value": value_of(ans),
                        "confidence": round(ans.get("confidence", 0), 3), "answer": ans, "frames": j.get("frames", [])})
        results.append(aggregate(chk, per))
    report = {"media": media, "baseline": baseline, "checks_file": spec.get("_path"), "model": MODEL,
              "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 2),
              "usage": summarize_usage(jobs), "results": results}
    report["summary"] = {v: sum(1 for r in results if r["verdict"] == v) for v in ("pass", "fail", "unsure", "info", "error")}
    report["status"] = ("error" if report["summary"]["error"] else "fail" if report["summary"]["fail"]
                        else "unsure" if report["summary"]["unsure"] else "pass")
    return report


def aggregate(chk, per):
    agg = chk.get("agg", "all")
    verdicts = [p["verdict"] for p in per]
    if not per:
        v = "error"
    elif "error" in verdicts:
        v = "error"
    elif "dry-run" in verdicts:
        v = "dry-run"
    elif agg == "any":
        v = "pass" if "pass" in verdicts else "unsure" if "unsure" in verdicts else verdicts[0] if len(set(verdicts)) == 1 else "fail"
    elif agg == "majority":
        n_pass, n_fail = verdicts.count("pass"), verdicts.count("fail")
        v = "pass" if n_pass > len(verdicts) / 2 else "fail" if n_fail > len(verdicts) / 2 else "unsure"
    else:
        v = "fail" if "fail" in verdicts else "unsure" if "unsure" in verdicts else "info" if set(verdicts) == {"info"} else "pass"
    worst = None
    scored = [p for p in per if "value" in p]
    if scored:
        bad = [p for p in scored if p["verdict"] in ("fail", "unsure")] or scored
        worst = bad[0]
        if all(isinstance(p["value"], (int, float)) for p in bad):
            exp = str(chk.get("expect", ""))
            low_is_bad = exp.startswith(">") or exp == "yes"
            worst = (min if low_is_bad else max)(bad, key=lambda p: p["value"])
    return {"id": chk["id"], "ask": chk.get("ask"), "type": chk.get("type") or ("choice" if "options" in chk else "score" if "levels" in chk else "noul"),
            "expect": chk.get("expect"), "agg": agg, "verdict": v, "n": len(per),
            "failing_at": [p["at"] for p in per if p["verdict"] in ("fail", "unsure", "error")],
            "worst": {k: worst[k] for k in ("at", "value", "confidence", "verdict") if k in worst} if worst else None,
            "per_input": per}


def save_frames(report, out_dir):
    """copy the stills behind failing / unsure answers next to the report (a fail alone tells the human nothing)."""
    out_dir = Path(out_dir)
    for r in report["results"]:
        for p in r["per_input"]:
            if p.get("verdict") in ("fail", "unsure") and p.get("frames"):
                out_dir.mkdir(parents=True, exist_ok=True)
                saved = []
                for i, f in enumerate(p["frames"]):
                    dst = out_dir / f"{r['id']}_{str(p['at']).replace(',', '_')[:40]}_{i}.jpg"
                    shutil.copyfile(f, dst)
                    saved.append(str(dst))
                p["frames"] = saved


def print_report(report):
    rows = [("check", "verdict", "at", "answer", "conf", "expect")]
    for r in report["results"]:
        w = r["worst"] or {}
        per = next((p for p in r["per_input"] if p.get("at") == w.get("at")), r["per_input"][0] if r["per_input"] else {})
        ans = show(per["answer"]) if per.get("answer") else (per.get("error") or "")[:70]
        at = w.get("at", per.get("at", ""))
        if r["n"] > 1:
            at = f"{at} (worst of {r['n']})"
        rows.append((r["id"], r["verdict"].upper(), str(at), ans, f"{w.get('confidence', '')}", str(r["expect"])))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("| " + " | ".join(str(c).ljust(widths[k]) for k, c in enumerate(row)) + " |")
        if i == 0:
            print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    u, s = report["usage"], report["summary"]
    print(f"\n{report['status'].upper()}: {s['pass']} pass, {s['fail']} fail, {s['unsure']} unsure, {s['info']} info, "
          f"{s['error']} error | {u['requests']} requests ({u['cached']} cached), {u['input_tokens']:,} tokens, "
          f"${u['cost_usd']:.4f}, {report['seconds']} s")
    for r in report["results"]:
        for p in r["per_input"]:
            if p.get("error"):
                print(f"  {r['id']} @ {p['at']}: {p['error']}")
                break


def exit_code(report):
    return {"pass": 0, "fail": 1, "unsure": 2, "error": 3}.get(report["status"], 0)


# ---------------------------------------------------------------- ask / pick / scan


def sample_from_args(a):
    s = {}
    if a.at:
        s["at"] = a.at
    if a.every:
        s["every"] = a.every
    if a.from_ is not None:
        s["from"] = a.from_
    if a.to is not None:
        s["to"] = a.to
    if a.audio:
        s["audio"] = True
    if a.res:
        s["res"] = a.res
    if getattr(a, "each", False):
        s["each"] = True
    if getattr(a, "window", None):
        s["window"] = a.window
    if getattr(a, "hop", None):
        s["hop"] = a.hop
    if a.context:
        s["context"] = a.context
    if a.label:
        s["label"] = a.label
    if a.with_:
        s["with"] = a.with_
    if getattr(a, "baseline", None):
        s["compare"] = True
    return s


def questions_from_args(a):
    """positional = yes/no questions; --choice Q id=desc ...; --score Q level ... (low to high)."""
    checks = {}
    for i, q in enumerate(a.questions or [], 1):
        checks[f"q{i}"] = {"id": f"q{i}", "ask": q}
    for i, spec in enumerate(a.choice or [], 1):
        q, opts = spec[0], {}
        for o in spec[1:]:
            k, _, d = o.partition("=")
            opts[k.strip()] = d.strip()
        checks[f"c{i}"] = {"id": f"c{i}", "ask": q, "options": opts}
    for i, spec in enumerate(a.score or [], 1):
        checks[f"s{i}"] = {"id": f"s{i}", "ask": spec[0], "levels": spec[1:]}
    if not checks:
        raise DecideError("no question: pass a yes/no question, --choice or --score")
    if getattr(a, "expect", None):
        for c in checks.values():
            c["expect"] = a.expect
    return checks


def cmd_ask(a):
    checks = questions_from_args(a)
    spec = {"_path": str(Path.cwd() / "ask.json"), "min_confidence": a.min_confidence,
            "checks": [{**c, "sample": sample_from_args(a)} for c in checks.values()]}
    report = run_checks(spec, media=a.media, baseline=a.baseline, dry_run=a.dry_run, use_cache=not a.no_cache)
    if a.dry_run:
        return dry_print(report)
    for r in report["results"]:
        print(f"## {r['id']}: {r['ask']}")
        for p in r["per_input"]:
            if p.get("answer"):
                print(f"  @{p['at']:>12}  {show(p['answer'])}  conf {p['confidence']:.2f}"
                      + (f"  -> {p['verdict'].upper()}" if p["verdict"] != "info" else ""))
            else:
                print(f"  @{p['at']:>12}  ERROR {p.get('error')}")
    u = report["usage"]
    print(f"\n{u['requests']} requests ({u['cached']} cached), {u['input_tokens']:,} tokens, ${u['cost_usd']:.4f}, {report['seconds']} s")
    write_json(a, report)
    return exit_code(report)


def dry_print(report):
    u = report["usage"]
    print(f"dry run: {u['requests']} requests, ~{u['est_tokens']:,} tokens est., ~${u['est_tokens'] * PRICE_PER_TOKEN:.4f}")
    for r in report["results"]:
        print(f"  {r['id']}: {r['n']} input(s) at {', '.join(str(p['at']) for p in r['per_input'][:6])}"
              + (" ..." if r["n"] > 6 else ""))
    return 0


def cmd_run(a):
    spec = load_checks(a.checks)
    cli_vars = dict(v.split("=", 1) for v in a.var or [])
    var_files = dict(v.split("=", 1) for v in a.vars or [])
    only = set(a.only.split(",")) if a.only else None
    skip = set(a.skip.split(",")) if a.skip else None
    report = run_checks(spec, media=a.media, baseline=a.baseline, cli_vars=cli_vars, var_files=var_files,
                        only=only, skip=skip, dry_run=a.dry_run, use_cache=not a.no_cache)
    if a.dry_run:
        return dry_print(report)
    if a.json:
        save_frames(report, Path(a.json).with_suffix("").as_posix() + "_frames")
    print_report(report)
    write_json(a, report)
    return exit_code(report)


def write_json(a, report):
    if getattr(a, "json", None):
        p = Path(a.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"report: {p}")


def pick(candidates, question, sample=None, gates=None, refs=None, dry_run=False, use_cache=True):
    """Rank candidates. candidates: [(label, media, sample)]; question: check dict (score or noul; choice = joint mode).
    gates: [check dicts, noul, expect] that exclude a candidate. -> list sorted best first."""
    checks = {"rank": {**question, "id": "rank"}}
    for i, g in enumerate(gates or [], 1):
        checks[f"gate{i}"] = {**g, "id": f"gate{i}"}
    groups_jobs = []
    for label, media, s in candidates:
        smp = {**(sample or {}), **(s or {})}
        if refs:
            smp["with"] = refs
        jobs = make_jobs([(smp, {k: dict(v) for k, v in checks.items()})], media, None)
        for j in jobs:
            j["label"] = label
        groups_jobs += jobs
    jobs = execute(groups_jobs, dry_run=dry_run, use_cache=use_cache)
    out = []
    for j in jobs:
        row = {"label": j["label"], "media": j["input"]["media"], "at": j["input"]["at"], "error": j.get("error")}
        if j.get("answers"):
            ra = j["answers"]["rank"]
            row["value"] = value_of(ra)
            row["confidence"] = ra.get("confidence")
            row["answer"] = ra
            row["gates"] = {}
            for k, g in checks.items():
                if k.startswith("gate"):
                    ga = j["answers"].get(k)
                    row["gates"][g["ask"]] = {"p_yes": ga.get("noul"), "verdict": judge(ga, g.get("expect", "no"), g.get("min_confidence", 0.6))}
            row["kept"] = all(v["verdict"] != "fail" for v in row["gates"].values())  # unsure gates keep the candidate, flagged
        row["frames"] = j.get("frames")
        out.append(row)
    out.sort(key=lambda r: (not r.get("kept", False), -(r.get("value") or -1) if isinstance(r.get("value"), (int, float)) else 0))
    return out, summarize_usage(jobs)


def cmd_pick(a):
    if a.score:
        question = {"ask": a.score[0][0], "levels": a.score[0][1:]}
    elif a.questions:
        question = {"ask": a.questions[0]}
    else:
        raise DecideError("pick needs --score Q levels... or one yes/no question")
    sample = sample_from_args(a)
    sample.pop("at", None)
    if len(a.media) == 1 and a.at and "," in a.at:
        cands = [(f"{t:.2f}s", a.media[0], {"at": t}) for t in _num_list(a.at)]
    else:
        per = {"at": a.at} if a.at else {}
        cands = [(Path(m).stem, m, per) for m in a.media]
    gates = [{"ask": g, "expect": f"<{a.gate_max}"} for g in a.gate or []]
    t0 = time.time()
    rows, usage = pick(cands, question, sample, gates, None, a.dry_run, not a.no_cache)
    if a.dry_run:
        print(f"dry run: {usage['requests']} requests, ~{usage['est_tokens']:,} tokens")
        return 0
    for i, r in enumerate(rows, 1):
        if r.get("error"):
            print(f"{i:>2}. {r['label']:<32} ERROR {r['error']}")
            continue
        bad = [f"{k} ({v['verdict']}, P {v['p_yes']:.2f})" for k, v in (r.get("gates") or {}).items() if v["verdict"] != "pass"]
        g = ("  DROPPED by gate: " if not r.get("kept", True) else "  gate unsure: ") + "; ".join(bad) if bad else ""
        print(f"{i:>2}. {r['label']:<32} {r['value']:.2f}  conf {r['confidence']:.2f}{g}")
    print(f"\n{usage['requests']} requests ({usage['cached']} cached), {usage['input_tokens']:,} tokens, ${usage['cost_usd']:.4f}, {time.time() - t0:.1f} s")
    write_json(a, {"question": question, "gates": gates, "ranking": rows, "usage": usage})
    return 0 if rows and not rows[0].get("error") else 3


def spans(points, rule, merge_gap):
    """points [(t0, t1, value)] -> merged [t0, t1, max] where value matches rule."""
    out = []
    for a, b, v in sorted(points):
        if v is None or not _cmp(v, rule):
            continue
        if out and a - out[-1][1] <= merge_gap:
            out[-1][1] = max(out[-1][1], b)
            out[-1][2] = max(out[-1][2], v)
        else:
            out.append([a, b, v])
    return out


def cmd_scan(a):
    checks = questions_from_args(a)
    sample = sample_from_args(a)
    if not (a.every or a.window):
        raise DecideError("scan needs --every N (stills) or --window N (clips)")
    sample["each"] = True
    spec = {"_path": str(Path.cwd() / "scan.json"), "min_confidence": 0,
            "checks": [{**c, "sample": sample} for c in checks.values()]}
    report = run_checks(spec, media=a.media, dry_run=a.dry_run, use_cache=not a.no_cache)
    if a.dry_run:
        return dry_print(report)
    step = float(a.every or a.hop or a.window)
    flagged_any = False
    for r in report["results"]:
        print(f"## {r['id']}: {r['ask']}")
        pts = []
        for p in r["per_input"]:
            t = p.get("t")
            a0, a1 = (t, t) if isinstance(t, (int, float)) else (t or [0, 0])
            if isinstance(t, (int, float)):
                a0, a1 = t - step / 2, t + step / 2
            pts.append((max(0.0, a0), a1, p.get("value") if isinstance(p.get("value"), (int, float)) else None))
            bar = "#" * int(round((p.get("value") or 0) * 20)) if isinstance(p.get("value"), (int, float)) and r["type"] == "noul" else ""
            if a.timeline:
                print(f"  {p['at']:>12}  {show(p['answer']) if p.get('answer') else p.get('error')}  {bar}")
        if a.flag:
            sp = spans(pts, a.flag, step * 0.51)
            r["spans"] = [{"from": round(x, 2), "to": round(y, 2), "max": round(v, 3)} for x, y, v in sp]
            flagged_any |= bool(sp)
            print(f"  flagged ({a.flag}): " + (", ".join(f"{x:.1f}-{y:.1f}s (max {v:.2f})" for x, y, v in sp) or "none"))
    u = report["usage"]
    print(f"\n{u['requests']} requests ({u['cached']} cached), {u['input_tokens']:,} tokens, ${u['cost_usd']:.4f}, {report['seconds']} s")
    write_json(a, report)
    return 1 if flagged_any else 0


# ---------------------------------------------------------------- calibrate (pick the wording + threshold on labelled examples)


def calibrate(spec, dry_run=False, use_cache=True):
    """spec: {"sample": {...defaults}, "items": [{"media", "at"|"from"/"to"|..., "label"}], "variants": [check dicts]}.
    Every variant is asked on every item in ONE request per item (up to 1000 questions share the input).
    label: true/false for yes/no variants, option id for choice, level index for score.
    -> per variant: separation stats + best threshold, ranked best first."""
    base_dir = Path(spec.get("_path", ".")).parent
    variants = {}
    for i, v in enumerate(spec["variants"], 1):
        vid = v.get("id") or f"v{i}"
        variants[vid] = {**v, "id": vid}
    groups, labels = [], []
    for item in spec["items"]:
        media = item["media"] if Path(item["media"]).is_absolute() else str(base_dir / item["media"])
        smp = {**spec.get("sample", {}), **{k: v for k, v in item.items() if k not in ("media", "label")}, "media": media}
        for t in (_num_list(smp.pop("at")) if "at" in smp else [None]):
            s = dict(smp)
            if t is not None:
                s["at"] = t
            groups.append((s, {k: dict(v) for k, v in variants.items()}))
            labels.append(item["label"])
    jobs = execute(make_jobs(groups, None, None), dry_run=dry_run, use_cache=use_cache)
    if dry_run:
        return {"usage": summarize_usage(jobs), "items": len(labels), "variants": list(variants)}
    rows = []
    for vid, v in variants.items():
        vals = []
        for job, lab in zip(jobs, labels):
            ans = (job.get("answers") or {}).get(vid)
            if ans:
                val = value_of(ans)
                if v.get("invert") and ans.get("type") == "noul":
                    val = 1 - val  # wording asks the opposite of the label ("face fully visible" vs label "text on face")
                vals.append((lab, val, ans.get("confidence"), job["input"]["media"], job["input"]["at"]))
        qtype = build_question(v)["type"]
        row = {"id": vid, "ask": v.get("ask"), "type": qtype, "n": len(vals)}
        if qtype == "noul":
            pos = [x[1] for x in vals if x[0] in (True, "yes", 1)]
            neg = [x[1] for x in vals if x[0] not in (True, "yes", 1)]
            row.update(_separation(pos, neg))
        elif qtype == "choice":
            row["accuracy"] = round(sum(1 for lab, val, *_ in vals if val == lab) / max(1, len(vals)), 3)
            row["misses"] = [{"at": at, "media": Path(m).name, "label": lab, "got": val} for lab, val, _, m, at in vals if val != lab]
        else:
            row["mae"] = round(sum(abs(float(lab) - val) for lab, val, *_ in vals) / max(1, len(vals)), 3)
        row["values"] = [{"label": lab, "value": round(val, 3) if isinstance(val, float) else val, "at": at,
                          "media": Path(m).name} for lab, val, _, m, at in vals]
        rows.append(row)
    rows.sort(key=lambda r: (-(r.get("margin", -9)), -(r.get("auc", 0)), -(r.get("accuracy", 0)), r.get("mae", 0)))
    return {"usage": summarize_usage(jobs), "items": len(labels), "ranking": rows}


def _separation(pos, neg):
    if not pos or not neg:
        return {"note": "need positive and negative items"}
    auc = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))
    margin = min(pos) - max(neg)
    cands = sorted(set([0.5] + [round(x, 3) for x in pos + neg]))
    best = max(cands, key=lambda th: (sum(p >= th for p in pos) + sum(q < th for q in neg), -abs(th - 0.5)))
    if margin > 0:
        best = round((min(pos) + max(neg)) / 2, 3)
    acc = (sum(p >= best for p in pos) + sum(q < best for q in neg)) / (len(pos) + len(neg))
    return {"margin": round(margin, 3), "auc": round(auc, 3), "pos_mean": round(sum(pos) / len(pos), 3),
            "neg_mean": round(sum(neg) / len(neg), 3), "pos_min": round(min(pos), 3), "neg_max": round(max(neg), 3),
            "threshold": best, "accuracy": round(acc, 3)}


def cmd_calibrate(a):
    p = Path(a.spec)
    spec = json.loads(p.read_text(encoding="utf-8"))
    spec["_path"] = str(p.resolve())
    t0 = time.time()
    res = calibrate(spec, a.dry_run, not a.no_cache)
    u = res["usage"]
    if a.dry_run:
        print(f"dry run: {res['items']} labelled inputs x {len(res['variants'])} variants -> {u['requests']} requests, ~{u['est_tokens']:,} tokens")
        return 0
    for r in res["ranking"]:
        if r["type"] == "noul" and "margin" in r:
            print(f"{r['id']:<6} margin {r['margin']:+.2f}  auc {r['auc']:.2f}  yes~{r['pos_mean']:.2f} (min {r['pos_min']:.2f})  "
                  f"no~{r['neg_mean']:.2f} (max {r['neg_max']:.2f})  threshold {r['threshold']:.2f} acc {r['accuracy']:.2f}  | {r['ask']}")
        elif r["type"] == "choice":
            print(f"{r['id']:<6} accuracy {r['accuracy']:.2f}  misses {len(r['misses'])}  | {r['ask']}")
        else:
            print(f"{r['id']:<6} {json.dumps({k: r.get(k) for k in ('mae', 'note') if k in r})}  | {r['ask']}")
    print(f"\n{res['items']} labelled inputs, {u['requests']} requests ({u['cached']} cached), {u['input_tokens']:,} tokens, ${u['cost_usd']:.4f}, {time.time() - t0:.1f} s")
    write_json(a, res)
    return 0


# ---------------------------------------------------------------- CLI


def add_sample_flags(p, media_nargs=None):
    p.add_argument("--at", help="still frame(s), seconds, comma list; negative = from the end")
    p.add_argument("--every", type=float, help="stills every N seconds over [--from, --to)")
    p.add_argument("--from", dest="from_", type=float, help="span start, seconds (negative = from the end)")
    p.add_argument("--to", type=float, help="span end, seconds (default: end)")
    p.add_argument("--audio", action="store_true", help="send the audio of the span only")
    p.add_argument("--res", choices=RESOLUTIONS, help="low (default for video), high (default for stills; small text)")
    p.add_argument("--context", action="append", help="text after the media (transcript, expected text, rules)")
    p.add_argument("--label", help="text before the media")
    p.add_argument("--with", dest="with_", action="append", help="reference file before the media, FILE or FILE:label")
    p.add_argument("--dry-run", action="store_true", help="prepare media, estimate tokens, no API call")
    p.add_argument("--no-cache", action="store_true", help="ask again even when the same request is cached")
    p.add_argument("--json", help="write the full report here")


def add_question_flags(p):
    p.add_argument("questions", nargs="*", help="yes/no questions (statements work best: 'The caption covers a face.')")
    p.add_argument("--choice", nargs="+", action="append", metavar="Q", help="--choice QUESTION id=description ...")
    p.add_argument("--score", nargs="+", action="append", metavar="Q", help="--score QUESTION level0 level1 ... (low to high)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[2:]))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ask", help="ad-hoc questions on one sample")
    p.add_argument("media")
    add_question_flags(p)
    add_sample_flags(p)
    p.add_argument("--each", action="store_true", help="one request per still")
    p.add_argument("--window", type=float, help="split the span into clips of N s (one request each)")
    p.add_argument("--hop", type=float)
    p.add_argument("--expect", help="apply to every question: yes | no | >=0.8 | option id")
    p.add_argument("--min-confidence", type=float, default=0.6)
    p.add_argument("--baseline", help="previous version: input becomes 'Before:' baseline + 'After:' media at the same times")

    p = sub.add_parser("run", help="run a check file or pack")
    p.add_argument("checks", help="check JSON file or pack name (" + ", ".join(sorted(x.stem for x in PACKS.glob('*.json'))) + ")" if PACKS.exists() else "check JSON file")
    p.add_argument("--media", help="file under test (overrides the check file)")
    p.add_argument("--baseline", help="previous version for compare samples")
    p.add_argument("--var", action="append", help="name=value used as {name} in checks")
    p.add_argument("--vars", action="append", help="name=file.json: its keys become {name.key}")
    p.add_argument("--only", help="comma list of check ids")
    p.add_argument("--skip", help="comma list of check ids")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", help="write the report here; failing frames go to <name>_frames/")

    p = sub.add_parser("pick", help="rank candidates (takes, covers, angles)")
    p.add_argument("media", nargs="+", help="candidate files; or one file + --at t1,t2,... = frames as candidates")
    add_question_flags(p)
    add_sample_flags(p)
    p.add_argument("--gate", action="append", help="yes/no question that drops a candidate when yes (e.g. 'Visible text')")
    p.add_argument("--gate-max", type=float, default=0.5, help="drop when a gate's P(yes) reaches this (calibrate it; default 0.5)")

    p = sub.add_parser("scan", help="question set per window/frame over the whole file -> timeline + flagged spans")
    p.add_argument("media")
    add_question_flags(p)
    add_sample_flags(p)
    p.add_argument("--window", type=float, help="clip length, seconds (video windows instead of stills)")
    p.add_argument("--hop", type=float)
    p.add_argument("--flag", help="value rule that flags a window, e.g. '>=0.5'")
    p.add_argument("--timeline", action="store_true", help="print every window")

    p = sub.add_parser("calibrate", help="compare question wordings on labelled examples, get thresholds")
    p.add_argument("spec", help="JSON: sample defaults, items [{media, at, label}], variants [checks]")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", help="write the full ranking here")

    a = ap.parse_args(argv)
    try:
        return {"ask": cmd_ask, "run": cmd_run, "pick": cmd_pick, "scan": cmd_scan, "calibrate": cmd_calibrate}[a.cmd](a)
    except DecideError as e:
        print(f"decide: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
