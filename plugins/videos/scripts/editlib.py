"""Shared helpers for the videos edit engine (stdlib only): project config, Words loading, text anchors, SRT, cut lists.

Imported by the stage scripts beside it (rough.py, tighten.py, words_dump.py, ...); not run on its own.
Formats: ${CLAUDE_PLUGIN_ROOT}/references/engine.md (project file edit.json, cut lists, overrides, style).
"""
import copy
import json
import math
import os
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# Filler words by language (norm() form). French = Québec speech as measured: « eh » / « ben » are real words there.
FILLERS = {"fr": ["euh", "heu", "hum", "hmm", "euhm"], "en": ["um", "uh", "er", "erm", "hmm", "mm", "uhm"]}
NTSC = {23.976: 24000 / 1001, 29.97: 30000 / 1001, 59.94: 60000 / 1001, 47.952: 48000 / 1001, 119.88: 120000 / 1001}


def setup_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def norm_fps(x):
    """23.976 -> 24000/1001 exactly (also 29.97, 59.94, ...); other rates unchanged."""
    x = float(x)
    for k, v in NTSC.items():
        if abs(x - k) < 0.002:
            return v
    return x


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data, indent=1):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, p)


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


# ---------------------------------------------------------------- text

def norm(s):
    """lowercase, accents stripped, letters/digits only: « Ça, » -> "ca"."""
    s = "".join(ch for ch in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(ch))
    return re.sub(r"[^\w]", "", s)


def anchor_regex(anchor):
    parts = []
    for w in anchor.split():
        parts.append(r"\w{1,24}?" if w == "*" else re.escape(norm(w)))
    return re.compile("".join(parts))


def find_anchor(words, approx, anchor, window=12.0):
    """(first, last) word index of the text anchor nearest `approx` s: accent-stripped characters, must start on a
    word boundary ("on a" never matches inside "qu'on a"); "*" = any one word (a name the transcript misheard)."""
    idx = [i for i, w in enumerate(words) if abs(w["start"] - approx) <= window]
    if not idx:
        raise ValueError(f"no words near {approx}")
    lo, hi = idx[0], idx[-1]
    s, cmap, starts = "", [], set()
    for i in range(lo, hi + 1):
        starts.add(len(s))
        n = norm(words[i]["text"])
        s += n
        cmap += [i] * len(n)
    best = None
    for m in anchor_regex(anchor).finditer(s):
        if m.start() not in starts:
            continue
        a, b = cmap[m.start()], cmap[m.end() - 1]
        d = abs(words[a]["start"] - approx)
        if best is None or d < best[0]:
            best = (d, a, b)
    if best is None:
        ctx = " ".join(w["text"] for w in words[lo:hi + 1])
        raise ValueError(f"anchor not found: {anchor!r} near {approx}\n  ctx: {ctx}")
    return best[1], best[2]


# ---------------------------------------------------------------- SRT

def ts(t):
    h, m, rest = t.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def fmt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def parse_srt(path):
    """[(start s, end s, text one line)] from an SRT file."""
    txt = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues = []
    for block in txt.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        a, b = lines[1].split(" --> ")
        cues.append((ts(a), ts(b), " ".join(lines[2:])))
    return cues


def write_srt(path, cues):
    """cues = [(start, end, [line, ...])]; CRLF line ends (Resolve and most players)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        for n, (a, b, lines) in enumerate(cues, 1):
            f.write(f"{n}\n{fmt(a)} --> {fmt(b)}\n" + "\n".join(lines) + "\n\n")


def wrap(text, width=42):
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def words_to_cues(words, max_words=14):
    """Caption cues straight from words (no source SRT): a cue ends on . ? ! or after max_words words."""
    cues, cur = [], []
    for w in words:
        if w.get("type") == "event":
            continue
        cur.append(w)
        if w["text"].rstrip().endswith((".", "?", "!")) or len(cur) >= max_words:
            cues.append((cur[0]["start"], cur[-1]["end"], " ".join(x["text"] for x in cur)))
            cur = []
    if cur:
        cues.append((cur[0]["start"], cur[-1]["end"], " ".join(x["text"] for x in cur)))
    return cues


def retime_srt(cues, words, segs, fps):
    """Source cues -> cues on the cut. segs: dicts with in_s, out_s, rec_s (seconds). Cue text is difflib-aligned to the
    words so hand fixes in the source SRT survive; a cue spanning a cut splits; long runs become 2-line chunks."""
    out = []
    for c0, c1, text in cues:
        ws = [w for w in words if c0 - 0.05 <= (w["start"] + w["end"]) / 2 <= c1 + 0.05]
        toks = text.split()
        tn = [norm(t) for t in toks]
        wn = [norm(w["text"]) for w in ws]
        times = [None] * len(toks)
        for blk in SequenceMatcher(None, tn, wn, autojunk=False).get_matching_blocks():
            for k in range(blk.size):
                w = ws[blk.b + k]
                times[blk.a + k] = (w["start"], w["end"])
        for i in range(len(times)):  # unmatched tokens (hand-fixed names) take a neighbour's time
            if times[i] is None:
                prev = next((times[j] for j in range(i - 1, -1, -1) if times[j]), None)
                nxt = next((times[j] for j in range(i + 1, len(times)) if times[j]), None)
                ref = prev or nxt or (c0, c1)
                times[i] = (ref[1], ref[1]) if prev else (ref[0], ref[0])
        runs = []
        for tok, (a, b) in zip(toks, times):
            mid = (a + b) / 2
            si = next((k for k, s in enumerate(segs) if s["in_s"] <= mid <= s["out_s"]), None)
            if runs and runs[-1][0] == si:
                runs[-1][1].append(tok)
                runs[-1][3] = b
            else:
                runs.append([si, [tok], a, b])
        for ri, (si, rtoks, a, b) in enumerate(runs):
            if si is None:
                continue
            s = segs[si]
            if ri == 0:
                a = c0
            if ri == len(runs) - 1:
                b = c1
            a = max(a, s["in_s"])
            b = min(max(b, a + 0.4), s["out_s"])
            ta, tb = s["rec_s"] + (a - s["in_s"]), s["rec_s"] + (b - s["in_s"])
            if tb - ta < 0.25:
                continue
            lines = wrap(" ".join(rtoks))
            if len(lines) <= 2:
                out.append([ta, tb, lines])
            else:
                chunks = [lines[i:i + 2] for i in range(0, len(lines), 2)]
                total = sum(len(" ".join(ch)) for ch in chunks)
                t = ta
                for ch in chunks:
                    d = (tb - ta) * len(" ".join(ch)) / total
                    out.append([t, t + d, ch])
                    t += d
    out.sort(key=lambda c: c[0])
    for i in range(len(out) - 1):  # no overlaps, 1 frame gap
        if out[i][1] > out[i + 1][0] - 1 / fps:
            out[i][1] = max(out[i][0] + 0.2, out[i + 1][0] - 1 / fps)
    return out


def finish_cues(cues, segs, fps):
    """Sort, 1-frame gap, clamp each cue to the end of the part it starts in, drop cues < 0.2 s."""
    cues.sort(key=lambda q: q[0])
    for i in range(len(cues) - 1):
        if cues[i][1] > cues[i + 1][0] - 1 / fps:
            cues[i][1] = max(cues[i][0] + 0.2, cues[i + 1][0] - 1 / fps)
    ends = [(s["rec_s"], s["rec_s"] + s["out_s"] - s["in_s"]) for s in segs]
    for q in cues:
        e = next((b for a, b in ends if a - 0.05 <= q[0] < b), None)
        if e is not None:
            q[1] = min(q[1], e)
    return [q for q in cues if q[1] - q[0] >= 0.2]


# ---------------------------------------------------------------- Words

def read_words(path):
    """Any transcript this engine knows -> Words dict {"source", "language", "model", "time_base", "words": [...]}.
    Accepts: Words (*.words.json); studio transcript (schema 1: segments[].words[{w, start_ms, end_ms}]); raw Grok STT
    response (words[{text, start, end}]); raw ElevenLabs Scribe (type word/spacing/audio_event); OpenAI verbose_json
    (words[{word, start, end}]). Empty tokens and Scribe spacing are dropped; no type is guessed here (see
    words_with_types for fillers)."""
    d = read_json(path)
    if isinstance(d, list):
        d = {"words": d}
    out = {"source": d.get("source") or d.get("audio") or "", "language": d.get("language") or d.get("language_code") or "",
           "model": d.get("model") or "", "time_base": d.get("time_base", "source"), "words": []}
    if out["language"] == "fra":
        out["language"] = "fr"
    if out["language"] == "eng":
        out["language"] = "en"
    ws = []
    studio = any("start_ms" in w for s in d.get("segments") or [] for w in (s.get("words") or [])[:1])
    if studio:
        for s in d["segments"]:
            for w in s.get("words", []):
                if not w.get("w", "").strip():
                    continue
                x = {"text": w["w"], "start": w["start_ms"] / 1000, "end": w["end_ms"] / 1000}
                if "speaker" in w:
                    x["speaker"] = w["speaker"]
                ws.append(x)
    else:
        for w in d.get("words", []):
            t = w.get("text", w.get("word", ""))
            typ = w.get("type", "word")
            if typ == "spacing" or not str(t).strip():
                continue
            x = {"text": t if "text" in w else str(t).strip(), "start": float(w["start"]), "end": float(w["end"])}
            if typ == "audio_event":
                typ = "event"
            x["type"] = typ if typ in ("word", "filler", "event") else "word"
            for k in ("speaker", "speaker_id", "logprob", "conf"):
                if k in w and k != "speaker_id":
                    x[k] = w[k]
            if "speaker_id" in w and "speaker" not in x:
                x["speaker"] = w["speaker_id"]
            ws.append(x)
    out["words"] = ws
    return out


def words_with_types(words, fillers):
    """Mark type "filler" on words whose normalised text is in `fillers` (a set of norm() strings); keeps others."""
    for w in words:
        if w.get("type", "word") == "word" and norm(w["text"]) in fillers:
            w["type"] = "filler"
        w.setdefault("type", "word")
    return words


# ---------------------------------------------------------------- project (edit.json)

class Project:
    """edit.json (+ edit.local.json beside it, deep-merged over it). Relative paths resolve against its folder.
    sources.<id> = one recording the cut is made from: file (program/Output file: sound + picture), words (+ words_offset_s
    to land on file time), captions (source SRT on the words time base), ears, fillers, scan, angles, face, multicam."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.dir = self.path.parent
        cfg = read_json(self.path)
        local = self.path.with_name(self.path.stem + ".local.json")
        if local.is_file():
            cfg = deep_merge(cfg, read_json(local))
        self.cfg = cfg
        self.fps = norm_fps(cfg.get("fps", 24000 / 1001))
        self.sources = cfg.get("sources", {})
        self._words = {}

    def p(self, rel):
        """Absolute path for a config value (None stays None)."""
        if not rel:
            return None
        q = Path(os.path.expandvars(os.path.expanduser(str(rel))))
        return str(q if q.is_absolute() else (self.dir / q).resolve())

    def src(self, sid):
        if sid not in self.sources:
            raise KeyError(f"source {sid!r} not in {self.path.name} (has {', '.join(self.sources)})")
        return self.sources[sid]

    def file(self, sid):
        return self.p(self.src(sid)["file"])

    def offset(self, sid):
        """Seconds added to word (and caption) times to land on the source file's time."""
        return float(self.src(sid).get("words_offset_s", 0.0))

    def words(self, sid):
        """Words of one source on the source file's time (offset applied), empty tokens dropped. Word id = index here
        = index in the Words file (import_words.py drops empty tokens too)."""
        if sid not in self._words:
            off = self.offset(sid)
            d = read_words(self.p(self.src(sid)["words"]))
            ws = []
            for w in d["words"]:
                if not w["text"].strip():
                    continue
                x = dict(w)
                x["start"], x["end"] = w["start"] + off, w["end"] + off
                ws.append(x)
            self._words[sid] = (ws, d.get("language") or self.cfg.get("language", ""))
        return self._words[sid][0]

    def language(self, sid=None):
        if sid is not None:
            self.words(sid)
            return self._words[sid][1] or self.cfg.get("language", "")
        return self.cfg.get("language", "")

    def captions(self, sid):
        """Source cues on the source file's time: the source SRT when given (keeps hand fixes), else built from words."""
        off = self.offset(sid)
        srt = self.p(self.src(sid).get("captions"))
        if srt and os.path.isfile(srt):
            return [(a + off, b + off, t) for a, b, t in parse_srt(srt)]
        return words_to_cues(self.words(sid))

    def angles(self, sid):
        """{angle name: {"file": abs path, "offset_s": s}}; offset + = the same sound comes later in the program file,
        so angle frame = program frame - round(offset_s * fps)."""
        out = {}
        for name, a in (self.src(sid).get("angles") or {}).items():
            a = a if isinstance(a, dict) else {"file": a}
            out[name] = {"file": self.p(a["file"]), "offset_s": a.get("offset_s")}
        return out

    def face(self, sid):
        """(file, delay_f) of the angle shown on face parts (V2), or (None, 0)."""
        s = self.src(sid)
        name = s.get("face")
        if not name:
            return None, 0
        a = self.angles(sid).get(name)
        if not a:
            return None, 0
        return a["file"], round((a["offset_s"] or 0.0) * self.fps)


# ---------------------------------------------------------------- cut lists

def load_cuts(path):
    """Cut list (*.cuts.json) with frame fields filled: in_f/out_f from in/out when missing, rec_f laid end to end
    (plus alt_gap_s before the first alternate) when missing, in_s/out_s/rec_s."""
    d = read_json(path)
    fps = d["fps"] = norm_fps(d["fps"])
    rec, alt_started = 0, False
    gap = round(float(d.get("alt_gap_s", 2.0)) * fps)
    for s in d["segments"]:
        if "in_f" not in s:
            s["in_f"] = round(s["in"] * fps)
        if "out_f" not in s:
            s["out_f"] = round(s["out"] * fps)
        s.setdefault("alt", None)
        s.setdefault("face", False)
        if s["alt"] and not alt_started:
            alt_started = True
            rec += gap
        if "rec_f" not in s:
            s["rec_f"] = rec
        rec = s["rec_f"] + s["out_f"] - s["in_f"]
        s["in_s"], s["out_s"], s["rec_s"] = s["in_f"] / fps, s["out_f"] / fps, s["rec_f"] / fps
    d.setdefault("markers", [])
    return d


def seg_seconds(s, fps):
    s["in_s"], s["out_s"], s["rec_s"] = s["in_f"] / fps, s["out_f"] / fps, s["rec_f"] / fps
    s["in"], s["out"] = round(s["in_f"] / fps, 6), round(s["out_f"] / fps, 6)
    return s


def main_end_f(segs):
    main = [s["rec_f"] + s["out_f"] - s["in_f"] for s in segs if not s.get("alt")]
    return max(main) if main else 0


def total_f(segs):
    return max((s["rec_f"] + s["out_f"] - s["in_f"] for s in segs), default=0)


def tc(sec, start_h=1):
    t = start_h * 3600 + sec
    return f"{int(t // 3600):02}:{int(t % 3600 // 60):02}:{t % 60:05.2f}"


def need_ffmpeg(name="ffmpeg"):
    return os.environ.get(name.upper(), name)


def frames_floor(t, fps):
    return max(0, math.floor(t * fps))
